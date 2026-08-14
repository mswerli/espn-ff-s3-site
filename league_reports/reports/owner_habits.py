"""Each owner's most-drafted player across all seasons. Was scripts/owner_habits.py."""
from collections import defaultdict, Counter

import pandas as pd

from league_reports.espn_client import get_league


def build_owner_habits(league_id, years, swid, espn_s2):
    """Returns a DataFrame: one row per owner's most-drafted player."""
    owner_player_counts = defaultdict(Counter)
    owner_player_years = defaultdict(lambda: defaultdict(list))  # owner_id -> player_name -> [years]
    owner_initials = {}

    for year in years:
        print(f"Processing {year}...")
        try:
            league = get_league(league_id, year, swid, espn_s2)
        except Exception as e:
            print(f"Failed to load league for {year}: {e}")
            continue

        team_id_to_owner = {}
        for team in league.teams:
            if not team.owners or not team.owners[0].get('id'):
                continue
            owner_id = team.owners[0]['id']
            initials = f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}"
            team_id_to_owner[team.team_id] = owner_id
            owner_initials[owner_id] = initials

        try:
            draft = league.draft
        except Exception as e:
            print(f"Could not load draft data for {year}: {e}")
            continue

        for pick in draft:
            team = pick.team
            player_name = pick.playerName
            if team.team_id not in team_id_to_owner:
                continue
            owner_id = team_id_to_owner[team.team_id]
            owner_player_counts[owner_id][player_name] += 1
            owner_player_years[owner_id][player_name].append(year)

    rows = []
    for owner_id, counter in owner_player_counts.items():
        if not counter:
            continue
        top_player, count = counter.most_common(1)[0]
        seasons = sorted(owner_player_years[owner_id][top_player])
        rows.append({
            "Owner ID": owner_id,
            "Owner Name": owner_initials.get(owner_id, "??"),
            "Most Drafted Player": top_player,
            "Times Drafted": count,
            "Drafted Seasons": " / ".join(map(str, seasons))
        })

    # DESIGN.md decision #15f: a league with no draft yet (e.g.
    # all-for-the-shiva pre-draft - league.draft is just an empty list, no
    # exception) produces an empty `rows`, and pd.DataFrame([]) has no
    # columns at all - sort_values(by="Times Drafted") would KeyError on a
    # column that doesn't exist rather than returning the expected empty
    # report, same class of fix as head_to_head_v2.py/weekly_summary_v2.py.
    columns = ["Owner ID", "Owner Name", "Most Drafted Player", "Times Drafted", "Drafted Seasons"]
    df = pd.DataFrame(rows, columns=columns)
    return df.sort_values(by="Times Drafted", ascending=False)
