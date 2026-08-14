"""Current-season weekly efficiency awards, survivor pool, and weekly
payout winners — v2. Same output shapes as
league_reports.reports.weekly_summary (build_weekly_efficiency ->
DataFrame, build_survivor_results -> dict, build_weekly_payouts -> list) -
see .claude/DESIGN-incremental-espn-pipeline.md decision #13, part C. The
scoring/optimal-lineup/payout-rule math is unchanged from v1, line for
line - only where per-week box-score data comes from is different, and
only object attribute access (`p.points`) became dict access (`p['points']`)
since league_reports.box_score_cache hands back trimmed plain dicts, not
espn_api BoxPlayer objects.

v1's build_weekly_efficiency and build_weekly_payouts each call
league.box_scores(week) separately, and so does advanced_history.py - 3x
redundant ESPN calls for the same (year, week) during a normal run. This
version reads through league_reports.box_score_cache.get_box_scores()
instead, shared with advanced_history_v2.py.

No per-year cache layer here (unlike head_to_head_v2/advanced_history_v2):
this report already only ever processes one season per call (it's "this
season's weekly awards", not a year-range loop), so there's no
across-many-years redundancy to eliminate - only the per-week box-score
cache applies, same as advanced_history_v2's "current year, already-played
weeks" layer. A `current_year` param still distinguishes "this whole
season is a past/backfilled one" (decision #11's year override) from "this
is the live season, only its already-played weeks are closed" - same
reasoning as advanced_history_v2.py, needed because a plain
`week < league.current_week` check under-counts a fully-finished season's
own last week by one.

build_survivor_results_v2 is untouched from v1 in every way but name - it
only post-processes an already-built efficiency DataFrame, no ESPN calls
of its own to cache.

Part D of decision #13 (the front-end-facing weekly/<year>/week_<n>.json +
manifest.json partitioned publish format) is intentionally NOT in this
file - tracked separately once parts A-C are validated, not bundled in
here (see DESIGN-incremental-espn-pipeline.md's rollout order, step 10).
"""
from collections import defaultdict

import pandas as pd

from league_reports.box_score_cache import get_box_scores
from league_reports.espn_client import get_league

DEFAULT_AWARDS = {
    "top_score": "🔥 The Regression Incoming Plaque",
    "bottom_score": "🧱 The Razz Memorial Crawlspace Trophy",
    "least_efficient": "🧠 Staniel's Should’ve Played My Bench Golden Clipboard",
}


def get_optimal_lineup(players, lineup_config):
    """players: box_score_cache's trimmed lineup (list of player dicts)."""
    used_ids = set()
    optimal_lineup = []

    position_groups = defaultdict(list)
    for p in players:
        if p['points'] is not None and p['points'] >= 0:
            position_groups[p['position']].append(p)

    def select_best(pos_list, count):
        nonlocal used_ids
        eligible = [p for p in pos_list if p.get('player_id') is not None and p['player_id'] not in used_ids]
        best = sorted(eligible, key=lambda p: p['points'] or 0, reverse=True)[:count]
        used_ids.update(p['player_id'] for p in best)
        return best

    for pos in ["QB", "RB", "WR", "TE", "K", "D/ST"]:
        count = lineup_config.get(pos, 0)
        optimal_lineup.extend(select_best(position_groups[pos], count))

    flex_count = lineup_config.get("FLEX", 0)
    flex_pool = []
    for pos in ["RB", "WR", "TE"]:
        flex_pool.extend([p for p in position_groups[pos] if p['player_id'] not in used_ids])
    optimal_lineup.extend(select_best(flex_pool, flex_count))

    return optimal_lineup


def calculate_team_efficiency(team, lineup, config, week):
    """team: an espn_api Team object (from league.teams - for team_name/
    owners); lineup: box_score_cache's trimmed player dicts."""
    actual = sum(p['points'] for p in lineup if p['slot_position'] not in ['BE', 'IR'] and p['points'] is not None)
    optimal = sum(p['points'] for p in get_optimal_lineup(lineup, config) if p['points'] is not None)
    efficiency = (actual / optimal) * 100 if optimal > 0 else 0.0

    return {
        "Week": week,
        "Team Name": team.team_name,
        "Owner": f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}" if team.owners else "N/A",
        "Actual Points": round(actual, 2),
        "Optimal Points": round(optimal, 2),
        "Efficiency %": round(efficiency, 2),
        "Award": ""
    }


def assign_weekly_awards(week_data, awards=DEFAULT_AWARDS):
    if not week_data:
        return

    max_actual = max(week_data, key=lambda x: x["Actual Points"])
    min_actual = min(week_data, key=lambda x: x["Actual Points"])
    min_eff = min(week_data, key=lambda x: x["Efficiency %"])

    for entry in week_data:
        if entry == max_actual:
            entry["Award"] = awards.get("top_score", DEFAULT_AWARDS["top_score"])
        elif entry == min_actual:
            entry["Award"] = awards.get("bottom_score", DEFAULT_AWARDS["bottom_score"])
        elif entry == min_eff:
            entry["Award"] = awards.get("least_efficient", DEFAULT_AWARDS["least_efficient"])


def _week_is_closed(year, week, current_year, league):
    return (current_year is not None and year < current_year) or week < league.current_week


def compute_weekly_efficiency_week(league, league_id, year, week, lineup_config, awards, bucket, is_closed):
    """One week's efficiency rows (one per team) with that week's award
    assigned, sourced via league_reports.box_score_cache instead of calling
    league.box_scores(week) directly."""
    box_scores = get_box_scores(league, league_id, year, week, bucket, is_closed=is_closed)
    team_lookup = {team.team_id: team for team in league.teams}

    week_data = []
    for box in box_scores:
        home_team = team_lookup.get(box['home_team_id'])
        away_team = team_lookup.get(box['away_team_id'])
        if home_team is None or away_team is None:
            continue  # defensive - box_score_cache already drops bye weeks
        week_data.append(calculate_team_efficiency(home_team, box['home_lineup'], lineup_config, week))
        week_data.append(calculate_team_efficiency(away_team, box['away_lineup'], lineup_config, week))

    assign_weekly_awards(week_data, awards)
    return week_data


def _weekly_efficiency_rows_to_df(all_data):
    df = pd.DataFrame(all_data)
    if not df.empty:
        df = df.sort_values(by=["Week", "Team Name"])
    return df


def build_weekly_efficiency_v2(league_id, year, swid, espn_s2, lineup_config, awards=DEFAULT_AWARDS):
    """Convenience wrapper: every week fetched live, no cache - what
    build_weekly_efficiency() does. Kept for direct comparison against v1
    (scripts/compare_v2.py) and for any caller without a bucket."""
    league = get_league(league_id, year, swid, espn_s2)
    all_data = []

    for week in range(1, min(league.currentMatchupPeriod, league.settings.reg_season_count) + 1):
        try:
            week_data = compute_weekly_efficiency_week(
                league, league_id, year, week, lineup_config, awards, bucket=None, is_closed=False,
            )
        except Exception as e:
            print(f"⚠️ Failed to load week {week}: {e}")
            continue
        all_data.extend(week_data)

    return _weekly_efficiency_rows_to_df(all_data)


def build_weekly_efficiency_v2_cached(league_id, year, current_year, swid, espn_s2, lineup_config, bucket, awards=DEFAULT_AWARDS):
    """The production path: already-played weeks of this season are read
    from/written to league_reports.box_score_cache; the just-completed/
    in-progress week is always live."""
    league = get_league(league_id, year, swid, espn_s2)
    all_data = []

    for week in range(1, min(league.currentMatchupPeriod, league.settings.reg_season_count) + 1):
        is_closed = _week_is_closed(year, week, current_year, league)
        try:
            week_data = compute_weekly_efficiency_week(
                league, league_id, year, week, lineup_config, awards, bucket=bucket, is_closed=is_closed,
            )
        except Exception as e:
            print(f"⚠️ Failed to load week {week}: {e}")
            continue
        all_data.extend(week_data)

    return _weekly_efficiency_rows_to_df(all_data)


def build_survivor_results_v2(efficiency_df, last_elimination_week=12):
    """Unchanged from v1's build_survivor_results in every way but name -
    this only post-processes an already-built efficiency DataFrame, no
    ESPN calls of its own to cache."""
    df = efficiency_df.sort_values(by=["Week", "Actual Points"])

    eliminated = {}
    remaining = set(df["Owner"].unique())

    for week in sorted(df["Week"].unique()):
        if week >= last_elimination_week:
            continue
        week_df = df[df["Week"] == week]
        week_df = week_df[week_df["Owner"].isin(remaining)]

        if not week_df.empty:
            lowest = week_df.iloc[0]
            eliminated_player = lowest["Owner"]
            eliminated[eliminated_player] = int(week)
            remaining.remove(eliminated_player)

    return {
        "eliminated": eliminated,
        "remaining": list(sorted(remaining))
    }


def compute_weekly_payout_week(league, league_id, year, week, payout_config, bucket, is_closed):
    """This week's payout winner, or None if there's no payout rule for
    this week or nobody qualified. Sourced via box_score_cache."""
    if str(week) not in payout_config["weekly_payouts"]:
        return None  # skip weeks with no payout rule

    rule = payout_config["weekly_payouts"][str(week)]
    payout_type = rule["type"]
    box_scores = get_box_scores(league, league_id, year, week, bucket, is_closed=is_closed)
    team_lookup = {team.team_id: team for team in league.teams}

    def best(players, count):
        return sorted(players, key=lambda x: x['points'] or 0, reverse=True)[:count]

    def add_points(players):
        return sum(p['points'] or 0 for p in players)

    winners = []

    for box in box_scores:
        for team_id, lineup in [(box['home_team_id'], box['home_lineup']), (box['away_team_id'], box['away_lineup'])]:
            team = team_lookup.get(team_id)
            if team is None:
                continue  # defensive - box_score_cache already drops bye weeks

            owner = f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}"
            name = team.team_name
            filtered = [p for p in lineup if p['slot_position'] != "BE" and p['points'] is not None]

            if payout_type == "highest_total_points":
                total = sum(p['points'] for p in filtered)
                winners.append({
                    "team": name,
                    "owner": owner,
                    "points": total,
                    "players": [p['name'] for p in filtered],
                    "text": "Highest Scoring Team"
                })

            elif payout_type == "top_player_overall":
                top = max(filtered, key=lambda p: p['points'])
                winners.append({
                    "team": name,
                    "owner": owner,
                    "points": top['points'],
                    "players": [top['name']],
                    "text": "Top Individual Player Score"
                })

            elif payout_type == "top_slot":
                max_combo = None
                max_points = -1
                slot_counts = rule["slots"]
                for pos, count in slot_counts.items():
                    eligible = [p for p in filtered if p['position'] == pos]
                    if len(eligible) >= count:
                        top_players = best(eligible, count)
                        points = add_points(top_players)
                        if points > max_points:
                            max_points = points
                            max_combo = top_players
                if max_combo:
                    winners.append({
                        "team": name,
                        "owner": owner,
                        "points": max_points,
                        "players": [p['name'] for p in max_combo],
                        "text": f"Top {', '.join([f'{v}×{k}' for k, v in slot_counts.items()])} Score"
                    })

            elif payout_type == "top_slot_combo":
                slot_counts = rule["slots"]
                selected = []
                used_ids = set()

                for pos, count in slot_counts.items():
                    eligible = [p for p in filtered if p['position'] == pos and p['player_id'] not in used_ids]
                    top_players = best(eligible, count)
                    used_ids.update(p['player_id'] for p in top_players)
                    selected.extend(top_players)

                if len(selected) == sum(slot_counts.values()):
                    total = add_points(selected)
                    winners.append({
                        "team": name,
                        "owner": owner,
                        "points": total,
                        "players": [p['name'] for p in selected],
                        "text": f"Top Combo: {', '.join([f'{v}×{k}' for k, v in slot_counts.items()])}"
                    })

    if not winners:
        return None

    top = max(winners, key=lambda w: w["points"])
    return {
        "week": week,
        "payout_text": top["text"],
        "team": top["team"],
        "owner": top["owner"],
        "points": round(top["points"], 2),
        "players": top["players"]
    }


def build_weekly_payouts_v2(league_id, year, swid, espn_s2, payout_config):
    """Convenience wrapper: every week fetched live, no cache - what
    build_weekly_payouts() does. No try/except around the per-week body,
    matching v1 exactly (v1 has none here, unlike build_weekly_efficiency)."""
    league = get_league(league_id, year, swid, espn_s2)
    all_winners = []

    for week in range(1, league.current_week):
        winner = compute_weekly_payout_week(league, league_id, year, week, payout_config, bucket=None, is_closed=False)
        if winner:
            all_winners.append(winner)

    return all_winners


def build_weekly_payouts_v2_cached(league_id, year, current_year, swid, espn_s2, payout_config, bucket):
    """The production path. Note: this loop only ever reaches
    range(1, league.current_week), i.e. it never touches the in-progress
    week at all (payouts are only awarded for fully-completed weeks) - so
    every week this function processes is already closed by construction,
    same `_week_is_closed` expression as the other two builders anyway for
    consistency rather than hardcoding True."""
    league = get_league(league_id, year, swid, espn_s2)
    all_winners = []

    for week in range(1, league.current_week):
        is_closed = _week_is_closed(year, week, current_year, league)
        winner = compute_weekly_payout_week(league, league_id, year, week, payout_config, bucket=bucket, is_closed=is_closed)
        if winner:
            all_winners.append(winner)

    return all_winners
