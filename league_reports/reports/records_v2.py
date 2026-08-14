"""All-time single-game/single-player records — v2. Same output shape as
league_reports.reports.records.build_records (see .claude/DESIGN.md
decision #10, .claude/TODO-backend.md's "records.py per-year cache" item).
The record-finding logic is unchanged from v1 - only the box-score data
source and the year-partitioning are different.

v1 reduces across every configured year in a single pass (running
team_game_high/team_game_low/player_game_high/player_game_high_by_pos
holders, updated as it walks years in order). That's not cacheable as-is -
a closed year's contribution can't be extracted from the middle of a
running reduction. This version splits the same reduction into a
per-year partial (compute_records_year()) plus a merge step
(merge_records_partials()), the same split head_to_head_v2.py uses:
each year's partial is independently cacheable, and merging is just
re-running the same >/< comparisons across partials instead of across
individual games - order-sensitive in the same way v1 is (ties keep
whichever year was compared first), so partials must be merged in the
same ascending year order v1 would have visited them in.

Per-week box-score data comes from league_reports.box_score_cache instead
of calling league.box_scores(week) directly - shared with
advanced_history_v2.py/weekly_summary_v2.py, so a week already cached by
one of those costs nothing here either.

Dropped, not carried over: v1's player_season_totals and
team_season_totals accumulators. Both are write-only in v1 - grepped the
whole repo, neither is read anywhere, including v1 itself. One of the two
(player_season_totals) was already flagged as apparently-dead in
.claude/TODO-backend.md; team_season_totals turned out to be dead too,
found while porting this.
"""
from collections import defaultdict

import pandas as pd

from league_reports.box_score_cache import get_box_scores
from league_reports.cache import get_or_compute_year
from league_reports.espn_client import get_league

_SENTINEL_HIGH = {"owner": "", "points": 0, "year": 0, "week": 0}
_SENTINEL_LOW = {"owner": "", "points": float('inf'), "year": 0, "week": 0}
_SENTINEL_PLAYER_HIGH = {"name": "", "owner": "", "points": 0, "year": 0, "week": 0}


def compute_records_year(league_id, year, swid, espn_s2, current_year=None, bucket=None, max_week=None):
    """One season's contribution to the all-time records, as a JSON-safe
    dict: {"team_game_high": {...}, "team_game_low": {...},
    "player_game_high": {...}, "player_game_high_by_pos": {pos: {...}}} -
    same shape as the running holders v1 keeps, just scoped to one year.

    bucket=None: every week fetched live via get_box_scores(..., is_closed=False)
    - same behavior as v1, used by build_records_v2() and scripts/compare_v2.py.
    max_week (validation-only, see scripts/replay_2025.py): caps which weeks
    get processed and stands in for league.current_week in the closedness
    check, same as advanced_history_v2.py.

    Lets get_league()'s own exception propagate - that catch belongs to the
    caller (build_records_v2()/build_records_v2_cached() below), matching
    every other _v2 report module's split of responsibility."""
    league = get_league(league_id, year, swid, espn_s2)

    team_id_to_owner = {
        team.team_id: f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}"
        for team in league.teams
    }

    team_game_high = dict(_SENTINEL_HIGH)
    team_game_low = dict(_SENTINEL_LOW)
    player_game_high = dict(_SENTINEL_PLAYER_HIGH)
    player_game_high_by_pos = {}

    last_week = league.settings.reg_season_count
    if max_week is not None:
        last_week = min(last_week, max_week)
    effective_current_week = max_week if max_week is not None else league.current_week

    for week in range(1, last_week + 1):
        try:
            week_is_closed = bucket is not None and (
                (current_year is not None and year < current_year) or week < effective_current_week
            )
            box_scores = get_box_scores(league, league_id, year, week, bucket, is_closed=week_is_closed)
        except Exception as e:
            print(f"Failed week {week} in {year}: {e}")
            continue

        for box in box_scores:
            for team_id, score in [(box['home_team_id'], box['home_score']), (box['away_team_id'], box['away_score'])]:
                owner = team_id_to_owner.get(team_id, "??")

                if score > team_game_high["points"]:
                    team_game_high = {"owner": owner, "points": score, "year": year, "week": week}
                if score < team_game_low["points"]:
                    team_game_low = {"owner": owner, "points": score, "year": year, "week": week}

            for team_id, players in [(box['home_team_id'], box['home_lineup']), (box['away_team_id'], box['away_lineup'])]:
                owner = team_id_to_owner.get(team_id, "??")
                for player in players:
                    if not player['name'] or player['points'] is None:
                        continue
                    name = player['name']
                    pos = player['position']
                    pts = player['points']

                    if pts > player_game_high["points"]:
                        player_game_high = {
                            "name": name, "owner": owner, "points": pts, "year": year, "week": week
                        }

                    if pos and (pos not in player_game_high_by_pos or pts > player_game_high_by_pos[pos]["points"]):
                        player_game_high_by_pos[pos] = {
                            "name": name, "owner": owner, "points": pts, "year": year, "week": week
                        }

    return {
        "team_game_high": team_game_high,
        "team_game_low": team_game_low,
        "player_game_high": player_game_high,
        "player_game_high_by_pos": player_game_high_by_pos,
    }


def merge_records_partials(year_partials):
    """Combines compute_records_year() results, in the same ascending-year
    order v1's single pass would have visited them in - ties keep whichever
    year compares first, matching v1's `>`/`<` (not `>=`/`<=`) exactly."""
    team_game_high = dict(_SENTINEL_HIGH)
    team_game_low = dict(_SENTINEL_LOW)
    player_game_high = dict(_SENTINEL_PLAYER_HIGH)
    player_game_high_by_pos = {}

    for partial in year_partials:
        if partial["team_game_high"]["points"] > team_game_high["points"]:
            team_game_high = partial["team_game_high"]
        if partial["team_game_low"]["points"] < team_game_low["points"]:
            team_game_low = partial["team_game_low"]
        if partial["player_game_high"]["points"] > player_game_high["points"]:
            player_game_high = partial["player_game_high"]

        for pos, rec in partial["player_game_high_by_pos"].items():
            if pos not in player_game_high_by_pos or rec["points"] > player_game_high_by_pos[pos]["points"]:
                player_game_high_by_pos[pos] = rec

    records = [
        {"Category": "Team Game", "Record": "Most Points", "Owner": team_game_high["owner"], "Detail": "", "Points": round(team_game_high["points"], 2), "Year": team_game_high["year"], "Week": team_game_high["week"]},
        {"Category": "Team Game", "Record": "Least Points", "Owner": team_game_low["owner"], "Detail": "", "Points": round(team_game_low["points"], 2), "Year": team_game_low["year"], "Week": team_game_low["week"]},
        {"Category": "Single Game", "Record": "Top Player", "Owner": player_game_high["owner"], "Detail": player_game_high["name"], "Points": round(player_game_high["points"], 2), "Year": player_game_high["year"], "Week": player_game_high["week"]},
    ]

    for pos, rec in player_game_high_by_pos.items():
        records.append({
            "Category": "Single Game", "Record": f"Top {pos}", "Owner": rec["owner"], "Detail": rec["name"],
            "Points": round(rec["points"], 2), "Year": rec["year"], "Week": rec["week"]
        })

    return pd.DataFrame(records)


def build_records_v2(league_id, years, swid, espn_s2):
    """Convenience wrapper: compute_records_year() for every year, fresh,
    no cache - what build_records() does. Kept for direct comparison
    against v1 (scripts/compare_v2.py) and for any caller without a bucket."""
    partials = []
    for year in years:
        print(f"Processing {year}...")
        try:
            partials.append(compute_records_year(league_id, year, swid, espn_s2))
        except Exception as e:
            print(f"Failed to load {year}: {e}")
            continue
    return merge_records_partials(partials)


def build_records_v2_cached(league_id, years, current_year, swid, espn_s2, bucket):
    """The production path (DESIGN.md decision #10a): closed years
    (year < current_year) are read from
    s3://<bucket>/leagues/<league_id>/cache/records/<year>.json if present,
    computed+cached once on a miss; the current year is always computed
    fresh, its already-played weeks going through box_score_cache so a
    mid-season re-run doesn't re-fetch them either."""
    partials = []
    for year in years:
        is_closed = year < current_year
        print(f"{'Cache-or-compute' if is_closed else 'Live (current year)'}: {year}...")
        try:
            partial = get_or_compute_year(
                bucket=bucket,
                league_id=league_id,
                report="records",
                year=year,
                is_closed=is_closed,
                compute_fn=lambda y=year: compute_records_year(
                    league_id, y, swid, espn_s2, current_year=current_year, bucket=bucket,
                ),
            )
        except Exception as e:
            print(f"Failed to load {year}: {e}")
            continue
        partials.append(partial)
    return merge_records_partials(partials)
