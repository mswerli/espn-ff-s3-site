"""Thin local CLI wrapper around who_dat.reports.advanced_history. Writes
advanced_team_metrics.csv to the project root."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from who_dat.config import get_credentials, get_owner_map, get_league_config, output_path, year_range
from who_dat.reports.advanced_history import build_advanced_history


def main():
    creds = get_credentials()
    owner_map = get_owner_map()
    config = get_league_config()

    df = build_advanced_history(
        league_id=config['league_id'],
        years=year_range(config, span="box_score"),
        owner_map=owner_map,
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
    )

    out = output_path('advanced_team_metrics.csv')
    if df.empty:
        print("No data collected.")
    else:
        df.to_csv(out, index=False)
        print(f"Saved to {out}")


if __name__ == "__main__":
    main()
