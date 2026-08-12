"""Thin local CLI wrapper around league_reports.reports.head_to_head. Writes
head_to_head_lifetime.csv to the project root."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from league_reports.config import get_credentials, get_league_config, output_path, year_range
from league_reports.reports.head_to_head import build_head_to_head


def main():
    creds = get_credentials()
    config = get_league_config()

    df = build_head_to_head(
        league_id=config['league_id'],
        years=year_range(config, span="full"),
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
    )

    out = output_path('head_to_head_lifetime.csv')
    df.to_csv(out, index=False)
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
