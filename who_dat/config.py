"""Config/credential loading for local dev.

Report-building functions in who_dat/reports/ take plain values (league id,
year range, owner map, credentials, ...) rather than reading config
themselves - that's what lets the same report code run from a local CLI
script (config from these local JSON files) and, later, from a Lambda
(config from S3 / Secrets Manager / the EventBridge payload - see
DESIGN.md's `who_dat/config.py: local file, OR S3, OR Secrets Manager,
based on env`). This module is the "local file" backend; a future
S3/Secrets Manager backend would live alongside it, not replace it.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # project_root

CREDS_PATH = BASE_DIR / "ignore" / "espn_creds.json"
OWNER_MAP_PATH = BASE_DIR / "ignore" / "owner_map.json"
LEAGUE_CONFIG_PATH = BASE_DIR / "league_config.json"
PAYOUTS_CONFIG_PATH = BASE_DIR / "config" / "weekly_payouts_config.json"


def get_credentials(path=CREDS_PATH):
    with open(path) as f:
        return json.load(f)


def get_owner_map(path=OWNER_MAP_PATH):
    with open(path) as f:
        return json.load(f)


def get_league_config(path=LEAGUE_CONFIG_PATH):
    """Load league_config.json - the per-league settings (league id, year
    range, site title, award names, etc.) that make this codebase runnable
    for any ESPN fantasy football league, not just the one it started as."""
    with open(path) as f:
        return json.load(f)


def get_payouts_config(path=PAYOUTS_CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def output_path(filename):
    """Resolve a data-output filename against the project root, so scripts
    produce the same result whether run from the repo root or from scripts/."""
    return BASE_DIR / filename


def year_range(config, span="full"):
    """Build an inclusive year range from league_config.json's "years" block.

    span:
      "full"       -> years.start .. years.end        (draft/season history)
      "box_score"  -> years.box_score_start .. years.end (needs box_scores(),
                       which ESPN doesn't reliably expose for older seasons)
    """
    years = config["years"]
    end = years["end"]
    if span == "box_score":
        start = years.get("box_score_start", years["start"])
    else:
        start = years["start"]
    return range(start, end + 1)
