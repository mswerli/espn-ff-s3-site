"""Lifetime head-to-head records between every pair of owners — v2.

Same output shape as league_reports.reports.head_to_head.build_head_to_head
(see .claude/DESIGN-incremental-espn-pipeline.md decision #13, part B), but
derived entirely from Team.scores/schedule/outcomes instead of looping
league.scoreboard(week). Those arrays are already populated by the single
get_league() call League() construction makes, so this needs zero ESPN
calls beyond that one construction - no per-week fetch, no cache.

Intentional behavior difference from the v1 report: outcome == 'U'
(undecided/unplayed) weeks are skipped. v1's scoreboard()-based loop has no
equivalent skip, so an in-progress current season's unplayed future weeks
get counted as 0-0 results in its lifetime aggregate today - this fixes
that as a side effect of the refactor, not a separate change. See
scripts/compare_v2.py, which is expected to show exactly this one
difference and no others.
"""
from collections import defaultdict

import pandas as pd

from league_reports.espn_client import get_league


def build_head_to_head_v2(league_id, years, swid, espn_s2):
    """Returns a DataFrame: one row per (owner, opponent) pair. Same columns
    as build_head_to_head()."""
    matchups = defaultdict(lambda: {
        'Wins': 0, 'Losses': 0, 'Ties': 0,
        'Games Played': 0,
        'Points For': 0.0,
        'Points Against': 0.0
    })

    owner_id_to_name = {}

    for year in years:
        print(f"Processing {year}...")
        try:
            league = get_league(league_id, year, swid, espn_s2)
        except Exception as e:
            print(f"Error loading {year}: {e}")
            continue

        id_map = {team.team_id: team.owners[0]['id'] for team in league.teams}
        name_map = {team.owners[0]['id']: f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}" for team in league.teams}
        owner_id_to_name.update(name_map)

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

    df = pd.DataFrame(records)
    return df.sort_values(by=['Owner Name', 'Opponent Name'])
