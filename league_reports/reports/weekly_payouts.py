"""Weekly payout rules (DESIGN.md decision #16): who wins a week's side bet,
given that week's rule and box scores. Pure - no ESPN or S3 calls;
weekly_summary_v2.compute_weekly_payout_week fetches the box scores and
hands them here.

Three layers, each built from the one before:

1. Matchup facts - build_team_weeks() turns box_score_cache's trimmed
   matchups into one "team-week" dict per team: its starters, score,
   opponent score, margin and W/L/T result.
2. Player selection - top_n(), total_points(), players_for_slot().
3. Payout types - one function per type, (team_week, rule) -> candidate or
   None, registered in PAYOUT_TYPES. A candidate is
   {"points", "players", "text"}; None means "this team doesn't qualify".

pick_payout_winner() ties them together: filter team-weeks by the rule's
optional "result" modifier, collect each remaining team's candidate, pay the
best one.

Adding a payout type = one candidate function + one PAYOUT_TYPES entry.

Rule schema (one entry of weekly_payouts_config.json's week -> rule map):

  "type":   required, a PAYOUT_TYPES key
  "slots":  required for top_slot / top_slot_combo, e.g. {"QB": 1, "WR": 1}.
            Keys are player positions, or a SLOT_ALIASES key ("FLEX") to mean
            the lineup slot the player was started in
  "result": optional, "winner" or "loser" - only teams that won/lost their
            matchup are eligible. Works with every type
  "label":  optional display text override (decision #15e)
"""
from collections import namedtuple

# "slots" keys that name a lineup slot rather than a player position. A
# player started at flex still has position RB/WR/TE, so matching "FLEX"
# against position never finds anyone - the value is ESPN's slot_position
# string for that slot (espn_api's POSITION_MAP).
SLOT_ALIASES = {"FLEX": "RB/WR/TE"}

# rule["result"] -> the team-week "result" it keeps.
RESULT_FILTERS = {"winner": "W", "loser": "L"}


# --- 1. Matchup facts -----------------------------------------------------

def starters(lineup):
    """Everyone not on the bench who has a score."""
    return [p for p in lineup if p['slot_position'] != "BE" and p['points'] is not None]


def matchup_result(score, opponent_score):
    if score > opponent_score:
        return "W"
    if score < opponent_score:
        return "L"
    return "T"


def owner_initials(team):
    """Same format as weekly_summary_v2's efficiency rows - the front end
    joins payouts, efficiency and survivor on this string."""
    return f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}" if team.owners else "N/A"


def build_team_weeks(box_scores, team_lookup):
    """box_scores: box_score_cache's trimmed matchups; team_lookup:
    {team_id: espn_api Team}. Returns one dict per team per matchup, home
    before away, in box order. `score` is ESPN's official matchup score, so
    `result` always agrees with ESPN's standings."""
    team_weeks = []
    for box in box_scores:
        sides = [
            (box['home_team_id'], box['home_lineup'], box['home_score'], box['away_score']),
            (box['away_team_id'], box['away_lineup'], box['away_score'], box['home_score']),
        ]
        for team_id, lineup, score, opponent_score in sides:
            team = team_lookup.get(team_id)
            if team is None:
                continue  # defensive - box_score_cache already drops bye weeks
            team_weeks.append({
                "team": team.team_name,
                "owner": owner_initials(team),
                "starters": starters(lineup),
                "score": score,
                "opponent_score": opponent_score,
                "margin": score - opponent_score,
                "result": matchup_result(score, opponent_score),
            })
    return team_weeks


# --- 2. Player selection --------------------------------------------------

def top_n(players, n):
    return sorted(players, key=lambda p: p['points'] or 0, reverse=True)[:n]


def total_points(players):
    return sum(p['points'] or 0 for p in players)


def players_for_slot(players, slot):
    if slot in SLOT_ALIASES:
        return [p for p in players if p['slot_position'] == SLOT_ALIASES[slot]]
    return [p for p in players if p['position'] == slot]


def slots_label(slot_counts):
    return ', '.join(f'{count}×{slot}' for slot, count in slot_counts.items())


# --- 3. Payout types ------------------------------------------------------

def _candidate(points, players, text):
    return {"points": points, "players": [p['name'] for p in players], "text": text}


def _result_text(rule, text, winner=None, loser=None):
    """Default display text for a rule: `text` as-is with no "result"
    modifier, otherwise the modifier-specific wording if the type has one,
    else `text` with the modifier appended."""
    result = rule.get("result")
    if result == "winner":
        return winner or f"{text} (Winning Team)"
    if result == "loser":
        return loser or f"{text} (Losing Team)"
    return text


def _highest_total_points(team_week, rule):
    players = team_week["starters"]
    text = _result_text(
        rule, "Highest Scoring Team",
        winner="Highest Scoring Winning Team", loser="Highest Scoring Losing Team",
    )
    return _candidate(sum(p['points'] for p in players), players, text)


def _top_player_overall(team_week, rule):
    if not team_week["starters"]:
        return None
    top = max(team_week["starters"], key=lambda p: p['points'])
    return _candidate(top['points'], [top], _result_text(rule, "Top Individual Player Score"))


def _top_slot(team_week, rule):
    """Best single slot group: with several slots listed, each is scored on
    its own and the team's highest one counts (top_slot_combo adds them)."""
    slot_counts = rule["slots"]
    best_players = None
    best_points = -1
    for slot, count in slot_counts.items():
        eligible = players_for_slot(team_week["starters"], slot)
        if len(eligible) < count:
            continue
        players = top_n(eligible, count)
        points = total_points(players)
        if points > best_points:
            best_points = points
            best_players = players
    if not best_players:
        return None
    return _candidate(best_points, best_players, _result_text(rule, f"Top {slots_label(slot_counts)} Score"))


def _top_slot_combo(team_week, rule):
    """Every listed slot filled, summed. A player counts toward one slot only."""
    slot_counts = rule["slots"]
    selected = []
    used_ids = set()
    for slot, count in slot_counts.items():
        eligible = [p for p in players_for_slot(team_week["starters"], slot) if p['player_id'] not in used_ids]
        players = top_n(eligible, count)
        used_ids.update(p['player_id'] for p in players)
        selected.extend(players)
    if len(selected) != sum(slot_counts.values()):
        return None
    return _candidate(total_points(selected), selected, _result_text(rule, f"Top Combo: {slots_label(slot_counts)}"))


def _margin_of_victory(text):
    """Candidate function paying a matchup's winner its margin. A tied
    matchup has no winner, so neither side qualifies."""
    def candidate(team_week, rule):
        if team_week["result"] != "W":
            return None
        return _candidate(team_week["margin"], [], text)
    return candidate


PayoutType = namedtuple("PayoutType", ["candidate", "lowest_wins"], defaults=[False])

PAYOUT_TYPES = {
    "highest_total_points": PayoutType(_highest_total_points),
    "top_player_overall": PayoutType(_top_player_overall),
    "top_slot": PayoutType(_top_slot),
    "top_slot_combo": PayoutType(_top_slot_combo),
    "biggest_blowout": PayoutType(_margin_of_victory("Biggest Blow Out")),
    "smallest_margin_of_victory": PayoutType(_margin_of_victory("Smallest Margin of Victory"), lowest_wins=True),
}


# --- Entry point ----------------------------------------------------------

def eligible_team_weeks(team_weeks, rule):
    """Applies the rule's optional "result" modifier. Teams in a tied
    matchup are neither winner nor loser."""
    result = rule.get("result")
    if result is None:
        return team_weeks
    return [tw for tw in team_weeks if tw["result"] == RESULT_FILTERS[result]]


def pick_payout_winner(week, rule, team_weeks):
    """This week's payout winner (weekly_payout_winners.json's row shape),
    or None if nobody qualified. Equal points: first in team_weeks order
    wins.

    A rule with an unknown "type" or "result" pays nobody and prints a
    warning rather than raising - payouts share a Lambda step with the
    efficiency and survivor reports, and a config typo shouldn't take those
    down with it. tests/test_weekly_payouts.py checks every league's
    committed config for exactly this."""
    payout_type = PAYOUT_TYPES.get(rule["type"])
    if payout_type is None:
        print(f"⚠️ Week {week}: unknown payout type {rule['type']!r} - no payout awarded")
        return None
    if rule.get("result") is not None and rule["result"] not in RESULT_FILTERS:
        print(f"⚠️ Week {week}: unknown payout result filter {rule['result']!r} - no payout awarded")
        return None

    candidates = []
    for team_week in eligible_team_weeks(team_weeks, rule):
        candidate = payout_type.candidate(team_week, rule)
        if candidate is not None:
            candidates.append({**candidate, "team": team_week["team"], "owner": team_week["owner"]})

    if not candidates:
        return None

    pick = min if payout_type.lowest_wins else max
    top = pick(candidates, key=lambda c: c["points"])
    return {
        "week": week,
        "payout_text": rule.get("label") or top["text"],
        "team": top["team"],
        "owner": top["owner"],
        "points": round(top["points"], 2),
        "players": top["players"]
    }
