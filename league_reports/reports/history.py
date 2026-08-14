"""Season-by-season standings history. Was scripts/get_history.py."""
import pandas as pd
import requests

from league_reports.espn_client import get_league

LEGACY_HISTORY_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{league_id}"


def _fetch_legacy_standings(league_id, year, swid, espn_s2, owner_map):
    """Fallback for a season espn_api's League() can't construct at all.

    DESIGN.md decision #15h: discovered via a league migrated from NFL.com
    (all-for-the-shiva, league_id 854288221) - ESPN's modern
    /seasons/<year>/segments/0/leagues/<id> endpoint 404s for that league's
    pre-2026 seasons (they never existed there), but the older
    /leagueHistory/<id>?seasonId=<year> endpoint still has them, for every
    year back to when the league started - not just pre-2018 seasons, which
    is the only case espn_api's League() itself ever tries this endpoint
    for. That response has no "schedule"/"roster" key at all (whatever
    produced it didn't carry per-week matchups or rosters over), which is
    exactly why League() can't parse it (KeyError on 'schedule') - but it
    does have each team's id, name, and final overall win/loss/points
    record, which is everything build_history() actually needs. No
    per-league flag controls this - it's a plain "try harder before giving
    up" fallback, generic to any league that hits the same shape of gap,
    not hardcoded to this one.

    Returns [] (not an exception) on any failure - the caller already
    treats a missing year as "skip it"; this is strictly an extra attempt
    before that, never a new way to fail loudly.
    """
    try:
        r = requests.get(
            LEGACY_HISTORY_URL.format(league_id=league_id),
            params={"seasonId": year, "view": ["mTeam"]},
            cookies={"espn_s2": espn_s2, "SWID": swid},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        teams = data.get("teams", [])
    except Exception as e:
        print(f"  Legacy standings fetch also failed for {year}: {e}")
        return []

    rows = []
    for team in teams:
        record = team.get("record", {}).get("overall", {})
        rank = team.get("rankCalculatedFinal")
        rows.append({
            'Year': year,
            'Owner ID': team["id"],
            'Owner Name': owner_map.get(str(team["id"])),
            'Wins': record.get("wins", 0),
            'Losses': record.get("losses", 0),
            'Points For': record.get("pointsFor", 0.0),
            'Points Against': record.get("pointsAgainst", 0.0),
            'Final Standing': rank,
            'Champion': rank == 1,
            # True Sacko (lowest points-for by *weekly* standings position,
            # not final rank - see below) needs per-week point data this
            # endpoint doesn't have. False rather than an approximation
            # that could misattribute a real badge to the wrong team.
            'Sacko': False,
        })
    if rows:
        print(f"  Recovered {len(rows)} teams for {year} via the legacy standings endpoint")
    return rows


def build_history(league_id, years, owner_map, swid, espn_s2):
    """Returns a DataFrame: one row per team per season."""
    history_data = []

    for year in years:
        print(f"Fetching data for {year}...")
        try:
            league = get_league(league_id, year, swid, espn_s2)
        except Exception as e:
            print(f"Error loading year {year}: {e}")
            history_data.extend(_fetch_legacy_standings(league_id, year, swid, espn_s2, owner_map))
            continue

        team_count = len(league.teams)
        end_of_reg_season = league.settings.reg_season_count
        # A season with zero games played yet (pre-draft/pre-week-1, e.g.
        # all-for-the-shiva's 2026) still returns *some* ordering from
        # standings_weekly() - everyone's tied at 0, but .index() still
        # picks a "last place" team out of that tie, falsely flagging it
        # Sacko before a single game has been played. Champion already
        # avoids this (final_standing is 0/unset pre-season, never 1);
        # Sacko needs the same explicit guard since it's computed
        # independently, from weekly points rather than final standing.
        season_started = any((t.wins + t.losses) > 0 for t in league.teams)

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
                'Sacko': season_started and league.standings_weekly(end_of_reg_season).index(team) + 1 == team_count
            })

    df = pd.DataFrame(history_data)
    return df.sort_values(by=['Year', 'Final Standing'])
