"""Thin local CLI wrapper around who_dat.reports.history. Writes
league_history.csv to the project root. All the actual logic lives in
who_dat/ so the same code can run from a Lambda later."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from who_dat.config import get_credentials, get_owner_map, get_league_config, output_path, year_range
from who_dat.reports.history import build_history


def main():
    creds = get_credentials()
    owner_map = get_owner_map()
    config = get_league_config()

    df = build_history(
        league_id=config['league_id'],
        years=year_range(config, span="full"),
        owner_map=owner_map,
        swid=creds['swid'],
        espn_s2=creds['espn_s2'],
    )

    out = output_path('league_history.csv')
    df.to_csv(out, index=False)
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
