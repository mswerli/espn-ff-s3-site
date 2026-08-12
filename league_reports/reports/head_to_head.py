"""Lifetime head-to-head records between every pair of owners. Was scripts/head_to_head.py."""
from collections import defaultdict

import pandas as pd

from league_reports.espn_client import get_league


def build_head_to_head(league_id, years, swid, espn_s2):
    """Returns a DataFrame: one row per (owner, opponent) pair."""
    matchups = defaultdict(lambda: {
        'Wins': 0, 'Losses': 0, 'Ties': 0,
        'Games Played': 0,
        'Points For': 0.0,
        'Points Against': 0.0
    })

    # Map owner_id to latest name
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

        for week in range(1, league.settings.reg_season_count + 1):
            scoreboard = league.scoreboard(week)
            for match in scoreboard:
                if not match.home_team or not match.away_team:
                    continue

                home_id = id_map[match.home_team.team_id]
                away_id = id_map[match.away_team.team_id]

                home_points = match.home_score
                away_points = match.away_score

                # Track for both perspectives
                for p1, p2, p1_pts, p2_pts in [
                    (home_id, away_id, home_points, away_points),
                    (away_id, home_id, away_points, home_points)
                ]:
                    key = (p1, p2)
                    matchups[key]['Games Played'] += 1
                    matchups[key]['Points For'] += p1_pts
                    matchups[key]['Points Against'] += p2_pts

                    if p1_pts > p2_pts:
                        matchups[key]['Wins'] += 1
                    elif p1_pts < p2_pts:
                        matchups[key]['Losses'] += 1
                    else:
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
