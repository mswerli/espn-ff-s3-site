"""Week-by-week replay of a real, already-completed season, simulating what
a live weekly Lambda run would have seen "as it happened" - validation for
.claude/DESIGN-incremental-espn-pipeline.md's testing checklist item "a
week's cache entry is fully overwritten... while it's still the current
week," which nothing else this session has actually exercised: the real
league's configured current season is already fully complete by today's
date, so every prior test of the _v2 pipeline saw either a fully-closed
season or (for the "current" year) a season whose own current_week already
equals its final week - never a genuinely in-progress one.

The `max_week` parameter added to advanced_history_v2.py/weekly_summary_v2.py
for exactly this purpose stands in for league.current_week, so a real
completed season can be replayed one week at a time: run 1 processes only
week 1 (live, gets cached); run 2 processes weeks 1-2 (week 1 read from the
cache run 1 wrote, week 2 live); and so on. This never asks ESPN for
anything it wouldn't otherwise answer - box_scores(week) for a past,
completed week returns that week's real final result regardless of what
"today" the caller is pretending it is - max_week only controls which
weeks the code is *allowed to look at* and which of those it treats as
still-cacheable, not what ESPN itself returns.

Uses the scratch bucket (not the branch stack's own bucket, which already
has this season's box-score cache fully populated from earlier full-season
runs this session - a fresh league_id/bucket combination is needed here so
each week's cache-or-compute really does start from nothing). Cleans up
after itself.

Usage:
    python scripts/replay_2025.py                # weeks 1..reg season count
    python scripts/replay_2025.py --through 5     # stop after week 5
"""
import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from league_reports.config import get_credentials, get_league_config, get_owner_map, get_payouts_config
from league_reports.espn_client import get_league
from league_reports.reports.advanced_history_v2 import build_advanced_history_v2_cached
from league_reports.reports.weekly_summary_v2 import (
    build_weekly_efficiency_v2_cached, build_weekly_payouts_v2_cached,
)

BUCKET = "espn-ff-site-scratch-217412666418"
YEAR = 2025


class CallCounter:
    """Counts outbound requests.get() calls during a `with` block, to show
    each replay step only ever pays for the one new week."""
    def __enter__(self):
        self.count = 0
        self._orig = requests.get
        def counted_get(*a, **k):
            self.count += 1
            return self._orig(*a, **k)
        requests.get = counted_get
        return self

    def __exit__(self, *exc):
        requests.get = self._orig


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--through", type=int, default=None, help="last week to replay (default: full regular season)")
    args = parser.parse_args()

    config = get_league_config()
    creds = get_credentials()
    owner_map = get_owner_map()
    payout_config = get_payouts_config()
    league_id = config["league_id"]

    league = get_league(league_id, YEAR, creds["swid"], creds["espn_s2"])
    last_week = args.through or league.settings.reg_season_count
    print(f"Replaying {YEAR} weeks 1..{last_week} against s3://{BUCKET} (reg_season_count={league.settings.reg_season_count})\n")

    for week in range(1, last_week + 1):
        print(f"=== Simulated week {week} ===")

        with CallCounter() as calls:
            adv_df = build_advanced_history_v2_cached(
                league_id=league_id, years=[YEAR], current_year=YEAR, owner_map=owner_map,
                swid=creds["swid"], espn_s2=creds["espn_s2"], bucket=BUCKET, max_week=week,
            )
        print(f"  advanced_history_v2:  {calls.count:3d} HTTP calls, {len(adv_df)} rows")

        with CallCounter() as calls:
            eff_df = build_weekly_efficiency_v2_cached(
                league_id=league_id, year=YEAR, current_year=YEAR, swid=creds["swid"], espn_s2=creds["espn_s2"],
                lineup_config=config["lineup"], bucket=BUCKET, awards=config["awards"], max_week=week,
            )
        print(f"  weekly_efficiency_v2: {calls.count:3d} HTTP calls, {len(eff_df)} rows")

        with CallCounter() as calls:
            payouts = build_weekly_payouts_v2_cached(
                league_id=league_id, year=YEAR, current_year=YEAR, swid=creds["swid"], espn_s2=creds["espn_s2"],
                payout_config=payout_config, bucket=BUCKET, max_week=week,
            )
        print(f"  weekly_payouts_v2:    {calls.count:3d} HTTP calls, {len(payouts)} winners")
        print()

    print(f"Replay complete through week {last_week}.")


if __name__ == "__main__":
    main()
