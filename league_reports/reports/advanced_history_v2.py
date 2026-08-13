"""Luck-adjusted "true" win/loss, strength of schedule, manager efficiency
— v2. Same output shape/columns as
league_reports.reports.advanced_history.build_advanced_history (see
.claude/DESIGN-incremental-espn-pipeline.md decision #13, part C). The
aggregation math is unchanged from v1, line for line - only where the
per-week box-score data comes from is different.

v1 calls league.box_scores(week) directly, once per week, and so does
weekly_summary.py (twice, for its two builders) - 3x redundant ESPN calls
for the same (year, week) during a normal run. This version reads through
league_reports.box_score_cache.get_box_scores() instead, which is what lets
that be shared: once any one of the three consumers has fetched+cached a
closed week, none of the others re-fetch it either.

Two independent cache layers, matching how the redundancy actually splits:

- Per-*year* (league_reports.cache, decision #10a): once a season is
  closed, its final computed rows never change - compute_advanced_history_year()
  runs "the whole loop over that year's weeks" for real once, gets cached
  whole, and is never touched by ESPN again. Unlike head_to_head, no
  cross-year merge is needed - each row only depends on its own year's
  data (Strength-of-Schedule averages within the season, not across
  seasons), so the cached unit is already the final per-row output, not a
  partial to be summed later.
- Per-*week*, within a still-open current season (box_score_cache,
  decision #13 part C): weeks of the current season that have already been
  played are also immutable in practice, so re-running mid-season doesn't
  re-fetch them either - only the just-completed/in-progress week is live
  every time.
"""
from collections import Counter, defaultdict

import pandas as pd

from league_reports.box_score_cache import get_box_scores
from league_reports.cache import get_or_compute_year
from league_reports.espn_client import get_league


def compute_advanced_history_year(league_id, year, owner_map, swid, espn_s2, current_year=None, bucket=None):
    """One season's fully-computed advanced-metrics rows, as a JSON-safe
    list of dicts matching build_advanced_history()'s per-row shape. No
    cross-year accumulation needed - safe to cache whole, per year.

    bucket=None: every week fetched live via get_box_scores(..., is_closed=False)
    - same behavior as v1, used by build_advanced_history_v2() and by
    scripts/compare_v2.py. bucket set: weeks before current_week (or every
    week, if this whole year is before current_year) are read from/written
    to league_reports.box_score_cache instead.

    Lets get_league()'s own exception propagate (same as v1's per-year
    try/except, but that catch belongs to the caller - see build_advanced_history_v2()/
    build_advanced_history_v2_cached() below - not this per-year function,
    matching head_to_head_v2.py's split of responsibility."""
    league = get_league(league_id, year, swid, espn_s2)

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
            team_wins[owner_id] = (team.wins, team.losses)
            team_owner_map[team.team_id] = (owner_id, initials)
        except Exception:
            continue

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
            week_is_closed = bucket is not None and (
                (current_year is not None and year < current_year) or week < league.current_week
            )
            box_scores = get_box_scores(league, league_id, year, week, bucket, is_closed=week_is_closed)
        except Exception:
            continue

        weekly_points = {}

        for box in box_scores:
            # box_score_cache._trim_box_scores() already drops bye-week
            # entries (a bye's home/away team_id comes back as a bare int
            # in the untrimmed BoxScore) - no bye check needed here, v1's
            # `isinstance(box.home_team, int)` guard moved into the cache
            # layer since it's the same for every consumer.
            for side in ['home', 'away']:
                team_id = box[f"{side}_team_id"]
                starter_lineup = box[f"{side}_lineup"]

                owner_id, initials = team_owner_map.get(team_id, (None, None))
                if owner_id is None:
                    continue

                # Calculate starter points
                starter_points = sum(p['points'] or 0 for p in starter_lineup if p['slot_position'] not in ['BE', 'IR'])
                team_stats[(owner_id, year)]['Starter Points'] += starter_points
                team_stats[(owner_id, year)]['Points For'] += starter_points

                # Build optimal lineup
                position_groups = defaultdict(list)
                for p in starter_lineup:
                    if p.get('position'):
                        position_groups[p['position']].append(p)

                optimal_points = 0.0
                used_ids = set()

                def best_player(pos_list, count):
                    return sorted(
                        [p for p in pos_list if p.get('player_id') is not None and p['player_id'] not in used_ids],
                        key=lambda x: x['points'] if x['points'] is not None else float('-inf'),
                        reverse=True
                    )[:count]

                def add_points(players):
                    total = 0.0
                    for p in players:
                        if p['points'] is not None and p['points'] >= 0:
                            total += p['points']
                            used_ids.add(p['player_id'])
                    return total

                optimal_points += add_points(best_player(position_groups['QB'], slot_counts['QB']))
                optimal_points += add_points(best_player(position_groups['RB'], slot_counts['RB']))
                optimal_points += add_points(best_player(position_groups['WR'], slot_counts['WR']))
                optimal_points += add_points(best_player(position_groups['TE'], slot_counts['TE']))
                optimal_points += add_points(best_player(position_groups['K'], slot_counts['K']))
                optimal_points += add_points(best_player(position_groups['D/ST'], slot_counts['D/ST']))

                flex_pool = []
                for pos in ['RB', 'WR', 'TE']:
                    flex_pool.extend([p for p in position_groups[pos] if p['player_id'] not in used_ids])
                optimal_points += add_points(best_player(flex_pool, slot_counts['RB/WR/TE']))

                team_stats[(owner_id, year)]['Optimal Points'] += optimal_points
                weekly_points[(owner_id, year)] = starter_points

            # Record opponent scores
            home_id = team_owner_map.get(box['home_team_id'], (None, None))[0]
            away_id = team_owner_map.get(box['away_team_id'], (None, None))[0]
            if home_id and away_id:
                team_stats[(home_id, year)]['Opponent Points'].append(box['away_score'])
                team_stats[(away_id, year)]['Opponent Points'].append(box['home_score'])

        # Calculate true wins/losses
        for team_key, score in weekly_points.items():
            wins = sum(1 for s in weekly_points.values() if score > s)
            losses = sum(1 for s in weekly_points.values() if score < s)
            team_stats[team_key]['True Wins'] += wins
            team_stats[team_key]['True Losses'] += losses
            team_stats[team_key]['Games Played'] += len(weekly_points.values()) - 1

    sos_all = [s for stats in team_stats.values() for s in stats['Opponent Points']]
    avg_sos = sum(sos_all) / len(sos_all) if sos_all else 0

    rows = []
    for (owner_id, yr), stats_dict in team_stats.items():
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
        normalized_win_pct = round(normalized_true_wins / league_games, 3)
        net_lucky_wins = real_wins - normalized_true_wins
        luck_adjustment = (sos - avg_sos) / 10
        adjusted_luck_index = round(net_lucky_wins - luck_adjustment, 2)

        rows.append({
            "Year": yr,
            "Owner ID": owner_id,
            "Owner Name": initials,
            "Normalized True Wins": normalized_true_wins,
            "Normalized True Losses": normalized_true_losses,
            "Scheduled Games": scheduled_games,
            "Legaue Games": league_games,
            "Wins": real_wins,
            "Losses": real_losses,
            "True W/L": f"{normalized_true_wins} - {normalized_true_losses}",
            "True W/L %": normalized_win_pct,
            "Luck Index": adjusted_luck_index,
            "Strength of Schedule": round(sos, 2),
            "Manager Efficiency": round(efficiency, 2)
        })

    return rows


def _rows_to_df(rows):
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(by=["Year", "Owner Name"])
    return df


def build_advanced_history_v2(league_id, years, owner_map, swid, espn_s2):
    """Convenience wrapper: compute_advanced_history_year() for every year,
    fresh, no cache - what build_advanced_history() does. Kept for direct
    comparison against v1 (scripts/compare_v2.py) and for any caller
    without a bucket to cache against."""
    data = []
    for year in years:
        print(f"Processing {year}...")
        try:
            data.extend(compute_advanced_history_year(league_id, year, owner_map, swid, espn_s2))
        except Exception as e:
            print(f"  Failed to load league for {year}: {e}")
            continue
    return _rows_to_df(data)


def build_advanced_history_v2_cached(league_id, years, current_year, owner_map, swid, espn_s2, bucket):
    """The production path. Per year: closed years (year < current_year)
    are read from s3://<bucket>/leagues/<league_id>/cache/advanced_history/<year>.json
    if present, computed+cached once on a miss; the current year is always
    computed fresh (and never cached at the year level), but its
    already-played weeks still go through box_score_cache so a mid-season
    re-run doesn't re-fetch them either."""
    data = []
    for year in years:
        is_closed = year < current_year
        print(f"{'Cache-or-compute' if is_closed else 'Live (current year)'}: {year}...")
        try:
            rows = get_or_compute_year(
                bucket=bucket,
                league_id=league_id,
                report="advanced_history",
                year=year,
                is_closed=is_closed,
                compute_fn=lambda y=year: compute_advanced_history_year(
                    league_id, y, owner_map, swid, espn_s2, current_year=current_year, bucket=bucket,
                ),
            )
        except Exception as e:
            print(f"  Failed to load league for {year}: {e}")
            continue
        data.extend(rows)
    return _rows_to_df(data)
