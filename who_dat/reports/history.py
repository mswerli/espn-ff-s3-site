"""Season-by-season standings history. Was scripts/get_history.py."""
import pandas as pd

from who_dat.espn_client import get_league


def build_history(league_id, years, owner_map, swid, espn_s2):
    """Returns a DataFrame: one row per team per season."""
    history_data = []

    for year in years:
        print(f"Fetching data for {year}...")
        try:
            league = get_league(league_id, year, swid, espn_s2)
        except Exception as e:
            print(f"Error loading year {year}: {e}")
            continue

        team_count = len(league.teams)
        end_of_reg_season = league.settings.reg_season_count

        for team in league.teams:
            history_data.append({
                'Year': year,
                'Owner ID': team.team_id,
                'Owner Name': owner_map.get(str(team.team_id)),
                'Wins': team.wins,
                'Losses': team.losses,
                'Points For': team.points_for,
                'Points Against': team.points_against,
                'Final Standing': team.final_standing,
                'Champion': team.final_standing == 1,
                'Sacko': league.standings_weekly(end_of_reg_season).index(team) + 1 == team_count
            })

    df = pd.DataFrame(history_data)
    return df.sort_values(by=['Year', 'Final Standing'])
