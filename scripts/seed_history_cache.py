"""One-off/repeatable tool: push leagues/<slug>/history_seed.json's manually
transcribed seasons into that league's S3 history cache (DESIGN.md decision
#15h), so lambda_function.py's _merge_cached_history_years() picks them up
on every run. Needs boto3 + real AWS credentials (this hits S3 directly via
league_reports.cache, the same Lambda-only backend config.py's S3 functions
use) - not part of the local-file backend, so it's a script, not something
scripts/run_all.py touches.

Usage, from the project root:  python scripts/seed_history_cache.py <slug>

Idempotent - re-running overwrites each seeded year with the same content,
safe to re-run after editing history_seed.json.
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from league_reports.cache import put_cached_year


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_history_cache.py <league-slug>")
        sys.exit(1)
    slug = sys.argv[1]

    registry = json.loads((PROJECT_ROOT / "leagues" / "registry.json").read_text())
    if slug not in registry:
        print(f"Unknown league slug {slug!r} - not in leagues/registry.json")
        sys.exit(1)
    bucket = registry[slug]["site_bucket"]

    league_config = json.loads((PROJECT_ROOT / "leagues" / slug / "league_config.json").read_text())
    league_id = league_config["league_id"]

    seed_path = PROJECT_ROOT / "leagues" / slug / "history_seed.json"
    if not seed_path.exists():
        print(f"No {seed_path} - nothing to seed for {slug!r}")
        sys.exit(1)
    seed = json.loads(seed_path.read_text())

    for year_str, rows in seed.items():
        if year_str == "_comment":
            continue
        year = int(year_str)
        put_cached_year(bucket, league_id, "history", year, rows)
        print(f"Seeded {slug}/history/{year}: {len(rows)} rows -> s3://{bucket}/leagues/{league_id}/cache/history/{year}.json")


if __name__ == "__main__":
    main()
