"""Thin local CLI wrapper around who_dat.reports.owner_habits. Writes
most_drafted_players.csv to the project root."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from who_dat.config import get_credentials, get_league_config, output_path, year_range
from who_dat.reports.owner_habits import build_owner_habits


def main():
    creds = get_credentials()
    config = get_league_config()

    df = build_owner_habits(
        league_id=config['league_id'],
        years=year_range(config, span="full"),
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
    )

    out = output_path('most_drafted_players.csv')
    df.to_csv(out, index=False)
    print(f"Exported to {out}")


if __name__ == "__main__":
    main()
