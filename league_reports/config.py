"""Config/credential loading for local dev, and (below) for the Lambda.

Report-building functions in league_reports/reports/ take plain values (league id,
year range, owner map, credentials, ...) rather than reading config
themselves - that's what lets the same report code run from a local CLI
script (config from these local JSON files) and from a Lambda (config from
S3 / Secrets Manager - see the "S3 + Secrets Manager backend" section
below). This module is the "local file" backend by default; the
S3/Secrets Manager backend lives alongside it, not replacing it - local
scripts keep calling get_owner_map() etc. exactly as before.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # project_root

# DESIGN.md decision #15b: which league's leagues/<slug>/ directory local
# scripts read by default. FF_LEAGUE defaults to "who-dat" - this repo's
# original (and, until decision #15, only) league - so every existing local
# script (scripts/run_all.py, compare_v2.py, ...) keeps working unchanged
# with no env var set. Testing a second league locally is
# `FF_LEAGUE=<slug> python scripts/run_all.py`. Same env-var-driven-default
# pattern FF_CONFIG_BACKEND already uses below for local-vs-S3 selection,
# not a new mechanism.
#
# espn_creds.json stays a single shared file, not per-league (decision
# #12c's reasoning still holds - one ESPN login is a member of every league
# this repo drives). owner_map.json holds real names, so it lives under
# ignore/leagues/<slug>/ rather than the committed leagues/<slug>/ -
# league_config.json/weekly_payouts_config.json hold no personal data and
# are committed.
ACTIVE_LEAGUE = os.environ.get("FF_LEAGUE", "who-dat")

CREDS_PATH = BASE_DIR / "ignore" / "espn_creds.json"
OWNER_MAP_PATH = BASE_DIR / "ignore" / "leagues" / ACTIVE_LEAGUE / "owner_map.json"
LEAGUE_CONFIG_PATH = BASE_DIR / "leagues" / ACTIVE_LEAGUE / "league_config.json"
PAYOUTS_CONFIG_PATH = BASE_DIR / "leagues" / ACTIVE_LEAGUE / "weekly_payouts_config.json"


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
      "history"    -> years.history_start .. years.end (DESIGN.md decision #15h -
                       history.py can recover standings-only data for seasons a
                       migrated-from-another-platform league's other reports can't
                       touch at all, via a legacy-endpoint fallback, so its usable
                       range can start earlier than every other report's "start")
    """
    years = config["years"]
    end = years["end"]
    if span == "box_score":
        start = years.get("box_score_start", years["start"])
    elif span == "history":
        start = years.get("history_start", years["start"])
    else:
        start = years["start"]
    return range(start, end + 1)


# --- S3 + Secrets Manager backend (Lambda) ------------------------------
#
# .claude/DESIGN.md decision #4: league_config.json is public (the front-end fetches
# it directly), so its canonical copy already lives at the site bucket root
# (written by the frontend deploy step, s3://<bucket>/league_config.json).
# Rather than duplicating that file into a second, Lambda-only location,
# the Lambda reads that same object as its own config input. owner_map.json
# and weekly_payouts_config.json aren't fetched by the browser, so they live
# under a separate (still not-secret, just not front-end-facing) "config/"
# prefix in the same bucket instead. ESPN swid/espn_s2 always come from
# Secrets Manager, never S3.
#
# Backend selection is one env var, FF_CONFIG_BACKEND ("local", the
# default - the functions above - or "s3"), set by template.yaml on the
# Lambda's environment. boto3 is only imported lazily, inside the functions
# below, so local scripts/tests that never touch the S3 backend don't need
# it installed (it isn't in requirements.txt - the Lambda runtime provides
# it for free; local S3-backend testing needs `pip install boto3`).

CONFIG_BACKEND = os.environ.get("FF_CONFIG_BACKEND", "local")
CONFIG_BUCKET = os.environ.get("FF_SITE_BUCKET")  # required when backend == "s3"
CONFIG_PREFIX = os.environ.get("FF_CONFIG_PREFIX", "config/")  # owner_map.json, weekly_payouts_config.json
LEAGUE_CONFIG_KEY = os.environ.get("FF_LEAGUE_CONFIG_KEY", "league_config.json")  # bucket root - same object the front-end fetches
ESPN_SECRET_NAME = os.environ.get("FF_ESPN_SECRET_NAME")  # required when backend == "s3"


def _get_s3_json(bucket, key):
    import boto3

    client = boto3.client("s3")
    obj = client.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


def get_owner_map_from_s3(bucket=None, prefix=None):
    bucket = bucket or CONFIG_BUCKET
    key = (prefix if prefix is not None else CONFIG_PREFIX) + "owner_map.json"
    return _get_s3_json(bucket, key)


def get_league_config_from_s3(bucket=None, key=None):
    bucket = bucket or CONFIG_BUCKET
    key = key or LEAGUE_CONFIG_KEY
    return _get_s3_json(bucket, key)


def get_payouts_config_from_s3(bucket=None, prefix=None):
    bucket = bucket or CONFIG_BUCKET
    key = (prefix if prefix is not None else CONFIG_PREFIX) + "weekly_payouts_config.json"
    return _get_s3_json(bucket, key)


def get_credentials_from_secrets_manager(secret_name=None):
    """swid/espn_s2, stored as a single JSON secret: {"swid": ..., "espn_s2": ...}."""
    import boto3

    secret_name = secret_name or ESPN_SECRET_NAME
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def load_all_config():
    """Dispatch on FF_CONFIG_BACKEND and return the four inputs every
    report needs: (league_config, owner_map, payouts_config, credentials).

    lambda_function.py calls this instead of the individual get_*()
    functions above, so it doesn't need to know which backend is active.
    Defaults to "local", so the same call also works from a local script or
    `sam local invoke` run without FF_CONFIG_BACKEND set.
    """
    if CONFIG_BACKEND == "s3":
        if not CONFIG_BUCKET:
            raise RuntimeError("FF_SITE_BUCKET must be set when FF_CONFIG_BACKEND=s3")
        if not ESPN_SECRET_NAME:
            raise RuntimeError("FF_ESPN_SECRET_NAME must be set when FF_CONFIG_BACKEND=s3")
        return (
            get_league_config_from_s3(),
            get_owner_map_from_s3(),
            get_payouts_config_from_s3(),
            get_credentials_from_secrets_manager(),
        )
    return (
        get_league_config(),
        get_owner_map(),
        get_payouts_config(),
        get_credentials(),
    )
