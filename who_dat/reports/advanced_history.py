"""Luck-adjusted "true" win/loss, strength of schedule, manager efficiency.
Was scripts/advanced_history.py."""
from collections import defaultdict, Counter

import pandas as pd

from who_dat.espn_client import get_league


def build_advanced_history(league_id, years, owner_map, swid, espn_s2):
    """Returns a DataFrame: one row per team per season."""
    data = []

    for year in years:
        print(f"Processing {year}...")
        try:
            league = get_league(league_id, year, swid, espn_s2)
        except Exception as e:
            print(f"  Failed to load league for {year}: {e}")
            continue

        # Extract lineup slot requirements from league settings
        slot_counts = Counter()
        for slot in league.settings.position_slot_counts:
            if slot == "RB/WR/TE":
                slot_counts["RB/WR/TE"] += league.settings.position_slot_counts[slot]
            elif slot in ["QB", "RB", "WR", "TE", "K", "D/ST"]:
                slot_counts[slot] += league.settings.position_slot_counts[slot]

        # Map team ID to owner ID and initials
        team_owner_map = {}
        team_wins = {}
        for team in league.teams:
            try:
                owner_id = team.team_id
                initials = owner_map.get(str(team.team_id))
                team_wins.update({owner_id: (team.wins, team.losses)})
                team_owner_map[team.team_id] = (owner_id, initials)
            except Exception:
                continue

        # Initialize per-team metrics
        team_stats = defaultdict(lambda: {
            'Wins': 0,
            'Losses': 0,
            'Points For': 0,
            'Opponent Points': [],
            'Starter Points': 0.0,
            'Optimal Points': 0.0,
            'True Wins': 0,
            'True Losses': 0,
            'Games Played': 0,
        })

        week_numbers = sorted(int(w) for w in league.settings.matchup_periods.keys() if int(w) < league.currentMatchupPeriod)

        for week in week_numbers:
            try:
                box_scores = league.box_scores(week)
            except Exception:
                continue

            weekly_points = {}

            for box in box_scores:
                if isinstance(box.home_team, int) or isinstance(box.away_team, int):
                    continue

                for side in ['home', 'away']:
                    team = getattr(box, f"{side}_team")
                    team_id = team.team_id
                    starter_lineup = getattr(box, f"{side}_lineup")

                    owner_id, initials = team_owner_map.get(team_id, (None, None))
                    if owner_id is None:
                        continue

                    # Calculate starter points
                    starter_points = sum(p.points or 0 for p in starter_lineup if p.slot_position not in ['BE', 'IR'])
                    team_stats[(owner_id, year)]['Starter Points'] += starter_points
                    team_stats[(owner_id, year)]['Points For'] += starter_points

                    # Build optimal lineup
                    position_groups = defaultdict(list)
                    for p in starter_lineup:
                        if hasattr(p, 'points') and p.position:
                            position_groups[p.position].append(p)

                    optimal_points = 0.0
                    used_ids = set()

                    def best_player(pos_list, count):
                        return sorted(
                            [p for p in pos_list if hasattr(p, 'playerId') and p.playerId not in used_ids],
                            key=lambda x: x.points if x.points is not None else float('-inf'),
                            reverse=True
                        )[:count]

                    def add_points(players):
                        total = 0.0
                        for p in players:
                            if p.points is not None and p.points >= 0:
                                total += p.points
                                used_ids.add(p.playerId)
                        return total

                    optimal_points += add_points(best_player(position_groups['QB'], slot_counts['QB']))
                    optimal_points += add_points(best_player(position_groups['RB'], slot_counts['RB']))
                    optimal_points += add_points(best_player(position_groups['WR'], slot_counts['WR']))
                    optimal_points += add_points(best_player(position_groups['TE'], slot_counts['TE']))
                    optimal_points += add_points(best_player(position_groups['K'], slot_counts['K']))
                    optimal_points += add_points(best_player(position_groups['D/ST'], slot_counts['D/ST']))

                    flex_pool = []
                    for pos in ['RB', 'WR', 'TE']:
                        flex_pool.extend([p for p in position_groups[pos] if p.playerId not in used_ids])
                    optimal_points += add_points(best_player(flex_pool, slot_counts['RB/WR/TE']))

                    team_stats[(owner_id, year)]['Optimal Points'] += optimal_points
                    weekly_points[(owner_id, year)] = starter_points

                # Record opponent scores
                home_id = team_owner_map.get(box.home_team.team_id, (None, None))[0]
                away_id = team_owner_map.get(box.away_team.team_id, (None, None))[0]
                if home_id and away_id:
                    team_stats[(home_id, year)]['Opponent Points'].append(box.away_score)
                    team_stats[(away_id, year)]['Opponent Points'].append(box.home_score)

            # Calculate true wins/losses
            for team_key, score in weekly_points.items():
                wins = sum(1 for s in weekly_points.values() if score > s)
                losses = sum(1 for s in weekly_points.values() if score < s)
                team_stats[team_key]['True Wins'] += wins
                team_stats[team_key]['True Losses'] += losses
                team_stats[team_key]['Games Played'] += len(weekly_points.values()) - 1

        sos_all = [s for stats in team_stats.values() for s in stats['Opponent Points']]
        avg_sos = sum(sos_all) / len(sos_all) if sos_all else 0
        # Compile final metrics
        for (owner_id, year), stats_dict in team_stats.items():
            real_wins = team_wins.get(owner_id)[0]
            real_losses = team_wins.get(owner_id)[1]
            league_games = real_wins + real_losses
            initials = team_owner_map.get([tid for tid, v in team_owner_map.items() if v[0] == owner_id][0], (None, None))[1]
            true_wins = stats_dict['True Wins']
            sos = sum(stats_dict['Opponent Points']) / len(stats_dict['Opponent Points']) if stats_dict['Opponent Points'] else 0.0
            efficiency = (stats_dict['Starter Points'] / stats_dict['Optimal Points']) * 100 if stats_dict['Optimal Points'] > 0 else 0.0
            games_played = stats_dict['Games Played']

            # Normalize True Wins/Losses to match actual number of scheduled games (if needed)
            scheduled_games = len(week_numbers)
            true_win_ratio = true_wins / games_played if games_played else 0
            normalized_true_wins = round(true_win_ratio * league_games)
            normalized_true_losses = league_games - normalized_true_wins
            noramlized_win_pct = round(normalized_true_wins / league_games, 3)
            net_lucky_wins = real_wins - normalized_true_wins
            luck_adjustment = (sos - avg_sos) / 10
            adjusted_luck_index = round(net_lucky_wins - luck_adjustment, 2)

            data.append({
                "Year": year,
                "Owner ID": owner_id,
                "Owner Name": initials,
                "Normalized True Wins": normalized_true_wins,
                "Normalized True Losses": normalized_true_losses,
                "Scheduled Games": scheduled_games,
                "Legaue Games": league_games,
                "Wins": real_wins,
                "Losses": real_losses,
                "True W/L": f"{normalized_true_wins} - {normalized_true_losses}",
                "True W/L %": noramlized_win_pct,
                "Luck Index": adjusted_luck_index,
                "Strength of Schedule": round(sos, 2),
                "Manager Efficiency": round(efficiency, 2)
            })

    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values(by=["Year", "Owner Name"])
    return df
