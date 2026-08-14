"""Lifetime head-to-head records between every pair of owners — v2.

Same output shape as league_reports.reports.head_to_head.build_head_to_head
(see .claude/DESIGN-incremental-espn-pipeline.md decision #13, part B), but
derived entirely from Team.scores/schedule/outcomes instead of looping
league.scoreboard(week). Those arrays are already populated by the single
get_league() call League() construction makes, so a single season needs
zero ESPN calls beyond that one construction - no per-week fetch, no
per-week cache.

Intentional behavior difference from the v1 report: outcome == 'U'
(undecided/unplayed) weeks are skipped. v1's scoreboard()-based loop has no
equivalent skip, so an in-progress current season's unplayed future weeks
get counted as 0-0 results in its lifetime aggregate today - this fixes
that as a side effect of the refactor, not a separate change. See
scripts/compare_v2.py, which is expected to show exactly this one
difference and no others.

Per-week caching isn't needed here (see above), but per-*year* caching
still is: a lifetime table spans every configured season, and closed
seasons can never produce different data again, so re-fetching them every
run is pure waste - exactly what league_reports.cache (DESIGN.md decision
#10a) exists to avoid. This module is split into three layers so that cache
can slot in:

  compute_head_to_head_year()  - one season's contribution, JSON-safe
  merge_head_to_head_partials() - combine any number of seasons' partials
  build_head_to_head_v2()       - convenience: compute every year fresh, no
                                   cache (what scripts/compare_v2.py exercises
                                   today)
  build_head_to_head_v2_cached() - the production path: cache-or-compute
                                    each year via league_reports.cache, then
                                    merge
"""
from collections import defaultdict

import pandas as pd

from league_reports.cache import get_or_compute_year
from league_reports.espn_client import get_league


def compute_head_to_head_year(league_id, year, swid, espn_s2):
    """Returns one season's contribution to the lifetime table, as a plain
    JSON-safe dict: {"matchups": [...], "names": {owner_id: initials}}.
    Never raises for an ESPN-side per-season failure - callers loop years
    and already handle a missing/broken season the same way build_history()
    etc. do (skip it, keep going), so this lets the caller's try/except
    decide, same as every other report function's per-year loop body."""
    league = get_league(league_id, year, swid, espn_s2)

    id_map = {team.team_id: team.owners[0]['id'] for team in league.teams}
    names = {
        team.owners[0]['id']: f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}"
        for team in league.teams
    }

    matchups = defaultdict(lambda: {
        'Wins': 0, 'Losses': 0, 'Ties': 0,
        'Games Played': 0,
        'Points For': 0.0,
        'Points Against': 0.0
    })

    reg_season_count = league.settings.reg_season_count

    for team in league.teams:
        p1 = id_map[team.team_id]
        weeks = min(reg_season_count, len(team.schedule), len(team.outcomes), len(team.scores))

        for w in range(weeks):
            # league.py's _fetch_teams() post-processes team.schedule to hold
            # opponent Team objects, not team_id ints (see league.py:52-57) -
            # a bye week resolves to the team's own Team instance.
            opponent = team.schedule[w]
            if opponent.team_id == team.team_id:
                continue  # bye week

            outcome = team.outcomes[w]
            if outcome == 'U':
                continue  # unplayed/undecided - the intentional fix noted above

            p2 = id_map[opponent.team_id]
            p1_pts = team.scores[w]
            p2_pts = opponent.scores[w]

            key = (p1, p2)
            matchups[key]['Games Played'] += 1
            matchups[key]['Points For'] += p1_pts
            matchups[key]['Points Against'] += p2_pts

            if outcome == 'W':
                matchups[key]['Wins'] += 1
            elif outcome == 'L':
                matchups[key]['Losses'] += 1
            elif outcome == 'T':
                matchups[key]['Ties'] += 1

    return {
        "matchups": [
            {"owner_id": p1, "opponent_id": p2, **stats}
            for (p1, p2), stats in matchups.items()
        ],
        "names": names,
    }


def merge_head_to_head_partials(year_partials):
    """Combines any number of compute_head_to_head_year() results (cached
    or fresh, any order) into the final lifetime DataFrame - same columns/
    sort as build_head_to_head_v2()/build_head_to_head()."""
    matchups = defaultdict(lambda: {
        'Wins': 0, 'Losses': 0, 'Ties': 0,
        'Games Played': 0,
        'Points For': 0.0,
        'Points Against': 0.0
    })
    owner_id_to_name = {}

    for partial in year_partials:
        owner_id_to_name.update(partial["names"])
        for row in partial["matchups"]:
            key = (row["owner_id"], row["opponent_id"])
            for stat in ('Wins', 'Losses', 'Ties', 'Games Played', 'Points For', 'Points Against'):
                matchups[key][stat] += row[stat]

    records = []
    for (owner_id, opp_id), stats in matchups.items():
        records.append({
            'Owner ID': owner_id,
            'Owner Name': owner_id_to_name.get(owner_id, 'Unknown'),
            'Opponent ID': opp_id,
            'Opponent Name': owner_id_to_name.get(opp_id, 'Unknown'),
            'Win %': round(100 * stats['Wins'] / stats['Games Played'], 2) if stats['Games Played'] > 0 else 0.0,
            **stats
        })

    # DESIGN.md decision #15f: a brand-new league with zero completed weeks
    # yet (every year_partials entry failed/empty - e.g. all-for-the-shiva
    # pre-draft) produces an empty `records`, and pd.DataFrame([]) has no
    # columns at all - sort_values(by=['Owner Name', ...]) would KeyError on
    # a column that doesn't exist rather than returning the expected empty
    # report. Explicit columns keep this the same "just empty" degrade every
    # other report already has (records_v2/advanced_history_v2/history.py),
    # not a crash.
    columns = ['Owner ID', 'Owner Name', 'Opponent ID', 'Opponent Name', 'Win %',
               'Wins', 'Losses', 'Ties', 'Games Played', 'Points For', 'Points Against']
    df = pd.DataFrame(records, columns=columns)
    return df.sort_values(by=['Owner Name', 'Opponent Name'])


def build_head_to_head_v2(league_id, years, swid, espn_s2):
    """Convenience wrapper: compute_head_to_head_year() for every year,
    fresh, no cache - what build_head_to_head() does, functionally
    equivalent to build_head_to_head_v2_cached() with every year treated
    as non-closed. Kept for direct comparison against v1 (scripts/compare_v2.py)
    and for any caller that doesn't have a bucket to cache against."""
    partials = []
    for year in years:
        print(f"Processing {year}...")
        try:
            partials.append(compute_head_to_head_year(league_id, year, swid, espn_s2))
        except Exception as e:
            print(f"Error loading {year}: {e}")
            continue
    return merge_head_to_head_partials(partials)


def build_head_to_head_v2_cached(league_id, years, current_year, swid, espn_s2, bucket):
    """The production path (DESIGN.md decision #10a, DESIGN-incremental-espn-pipeline.md
    decision #13): closed years (year < current_year) are read from
    s3://<bucket>/leagues/<league_id>/cache/head_to_head/<year>.json if
    present, computed+cached once on a miss; the current year is always
    computed fresh and never cached. Every run after a season closes costs
    zero ESPN calls for that season."""
    partials = []
    for year in years:
        is_closed = year < current_year
        print(f"{'Cache-or-compute' if is_closed else 'Live (current year)'}: {year}...")
        try:
            partial = get_or_compute_year(
                bucket=bucket,
                league_id=league_id,
                report="head_to_head",
                year=year,
                is_closed=is_closed,
                compute_fn=lambda y=year: compute_head_to_head_year(league_id, y, swid, espn_s2),
            )
        except Exception as e:
            print(f"Error loading {year}: {e}")
            continue
        partials.append(partial)
    return merge_head_to_head_partials(partials)
