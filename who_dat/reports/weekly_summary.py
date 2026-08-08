"""Current-season weekly efficiency awards, survivor pool, and weekly
payout winners. Was scripts/weekly_summary.py."""
import json
from collections import defaultdict

import pandas as pd

from who_dat.espn_client import get_league

DEFAULT_AWARDS = {
    "top_score": "🔥 The Regression Incoming Plaque",
    "bottom_score": "🧱 The Razz Memorial Crawlspace Trophy",
    "least_efficient": "🧠 Staniel's Should’ve Played My Bench Golden Clipboard",
}


def get_optimal_lineup(players, lineup_config):
    used_ids = set()
    optimal_lineup = []

    position_groups = defaultdict(list)
    for p in players:
        if p.points is not None and p.points >= 0:
            position_groups[p.position].append(p)

    def select_best(pos_list, count):
        nonlocal used_ids
        eligible = [p for p in pos_list if hasattr(p, 'playerId') and p.playerId not in used_ids]
        best = sorted(eligible, key=lambda p: p.points or 0, reverse=True)[:count]
        used_ids.update(p.playerId for p in best)
        return best

    for pos in ["QB", "RB", "WR", "TE", "K", "D/ST"]:
        count = lineup_config.get(pos, 0)
        optimal_lineup.extend(select_best(position_groups[pos], count))

    flex_count = lineup_config.get("FLEX", 0)
    flex_pool = []
    for pos in ["RB", "WR", "TE"]:
        flex_pool.extend([p for p in position_groups[pos] if p.playerId not in used_ids])
    optimal_lineup.extend(select_best(flex_pool, flex_count))

    return optimal_lineup


def calculate_team_efficiency(team, lineup, config, week):
    actual = sum(p.points for p in lineup if p.slot_position not in ['BE', 'IR'] and p.points is not None)
    optimal = sum(p.points for p in get_optimal_lineup(lineup, config) if p.points is not None)
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


def build_weekly_efficiency(league_id, year, swid, espn_s2, lineup_config, awards=DEFAULT_AWARDS):
    """Returns a DataFrame: one row per team per week, with this week's award (if any)."""
    league = get_league(league_id, year, swid, espn_s2)
    all_data = []

    for week in range(1, min(league.currentMatchupPeriod, league.settings.reg_season_count) + 1):
        try:
            box_scores = league.box_scores(week)
        except Exception as e:
            print(f"⚠️ Failed to load week {week}: {e}")
            continue

        week_data = []

        for box in box_scores:
            home_data = calculate_team_efficiency(box.home_team, box.home_lineup, lineup_config, week)
            away_data = calculate_team_efficiency(box.away_team, box.away_lineup, lineup_config, week)
            week_data.extend([home_data, away_data])

        assign_weekly_awards(week_data, awards)
        all_data.extend(week_data)

    df = pd.DataFrame(all_data)
    if not df.empty:
        df = df.sort_values(by=["Week", "Team Name"])
    return df


def build_survivor_results(efficiency_df, last_elimination_week=12):
    """Returns the {"eliminated": {...}, "remaining": [...]} dict, derived
    from the weekly efficiency DataFrame (lowest score each week is out)."""
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


def build_weekly_payouts(league_id, year, swid, espn_s2, payout_config):
    """Returns a list of weekly payout winners."""
    league = get_league(league_id, year, swid, espn_s2)

    all_winners = []

    def best(players, count):
        return sorted(players, key=lambda x: x.points or 0, reverse=True)[:count]

    def add_points(players):
        return sum(p.points or 0 for p in players)

    for week in range(1, league.current_week):
        if str(week) not in payout_config["weekly_payouts"]:
            continue  # skip weeks with no payout rule

        rule = payout_config["weekly_payouts"][str(week)]
        payout_type = rule["type"]
        box_scores = league.box_scores(week)
        winners = []

        for box in box_scores:
            for team, lineup in [(box.home_team, box.home_lineup), (box.away_team, box.away_lineup)]:
                owner = f"{team.owners[0]['firstName'][0]}{team.owners[0]['lastName'][0]}"
                name = team.team_name
                filtered = [p for p in lineup if p.slot_position != "BE" and p.points is not None]

                if payout_type == "highest_total_points":
                    total = sum(p.points for p in filtered)
                    winners.append({
                        "team": name,
                        "owner": owner,
                        "points": total,
                        "players": [p.name for p in filtered],
                        "text": "Highest Scoring Team"
                    })

                elif payout_type == "top_player_overall":
                    top = max(filtered, key=lambda p: p.points)
                    winners.append({
                        "team": name,
                        "owner": owner,
                        "points": top.points,
                        "players": [top.name],
                        "text": "Top Individual Player Score"
                    })

                elif payout_type == "top_slot":
                    max_combo = None
                    max_points = -1
                    slot_counts = rule["slots"]
                    for pos, count in slot_counts.items():
                        eligible = [p for p in filtered if p.position == pos]
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
                            "players": [p.name for p in max_combo],
                            "text": f"Top {', '.join([f'{v}×{k}' for k, v in slot_counts.items()])} Score"
                        })

                elif payout_type == "top_slot_combo":
                    slot_counts = rule["slots"]
                    selected = []
                    used_ids = set()

                    for pos, count in slot_counts.items():
                        eligible = [p for p in filtered if p.position == pos and p.playerId not in used_ids]
                        top_players = best(eligible, count)
                        used_ids.update(p.playerId for p in top_players)
                        selected.extend(top_players)

                    if len(selected) == sum(slot_counts.values()):
                        total = add_points(selected)
                        winners.append({
                            "team": name,
                            "owner": owner,
                            "points": total,
                            "players": [p.name for p in selected],
                            "text": f"Top Combo: {', '.join([f'{v}×{k}' for k, v in slot_counts.items()])}"
                        })

        if winners:
            top = max(winners, key=lambda w: w["points"])
            all_winners.append({
                "week": week,
                "payout_text": top["text"],
                "team": top["team"],
                "owner": top["owner"],
                "points": round(top["points"], 2),
                "players": top["players"]
            })

    return all_winners
