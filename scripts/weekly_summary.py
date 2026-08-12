"""Thin local CLI wrapper around league_reports.reports.weekly_summary. Writes
weekly_efficiency_awards.csv, survivor_results.json, and
weekly_payout_winners.json to the project root."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from league_reports.config import get_credentials, get_league_config, get_payouts_config, output_path
from league_reports.reports.weekly_summary import (
    DEFAULT_AWARDS,
    build_survivor_results,
    build_weekly_efficiency,
    build_weekly_payouts,
)


def main():
    creds = get_credentials()
    config = get_league_config()
    payout_config = get_payouts_config()

    league_id = config['league_id']
    year = config['years']['current']
    lineup_config = config.get('lineup', {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "D/ST": 1})
    awards = config.get('awards', DEFAULT_AWARDS)
    last_elimination_week = config.get('survivor', {}).get('last_elimination_week', 12)

    winners = build_weekly_payouts(
        league_id=league_id,
        year=year,
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
        payout_config=payout_config,
    )

    efficiency_df = build_weekly_efficiency(
        league_id=league_id,
        year=year,
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
        lineup_config=lineup_config,
        awards=awards,
    )
    efficiency_out = output_path("weekly_efficiency_awards.csv")
    efficiency_df.to_csv(efficiency_out, index=False)
    print(f"✅ Exported to {efficiency_out}")

    survivor_result = build_survivor_results(efficiency_df, last_elimination_week=last_elimination_week)
    survivor_out = output_path("survivor_results.json")
    with open(survivor_out, "w") as f:
        json.dump(survivor_result, f, indent=2)
    print(f"✅ Survivor results saved to {survivor_out}")

    payouts_out = output_path("weekly_payout_winners.json")
    with open(payouts_out, "w") as f:
        json.dump(winners, f, indent=2)
    print(f"✅ Payout winners saved to {payouts_out}")


if __name__ == "__main__":
    main()
