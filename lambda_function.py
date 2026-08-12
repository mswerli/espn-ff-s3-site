"""Lambda entrypoint: regenerate every data file for the configured league
and upload it to the site bucket.

Mirrors scripts/run_all.py's structure (six steps, same labels, one
try/except per step so one ESPN hiccup doesn't abort the rest) but writes to
/tmp and uploads to S3 instead of writing to the project root. See
DESIGN.md decision #7 for the exact output filenames/Content-Types and
decision #4 for where config comes from.

Config input: league_reports.config.load_all_config(), which (per
FF_CONFIG_BACKEND) reads league_config.json/owner_map.json/
weekly_payouts_config.json from S3 and swid/espn_s2 from Secrets Manager
(template.yaml sets that env var to "s3" for the deployed Lambda; it
defaults to "local" so this module could also be exercised locally/against
`sam local invoke` without S3 or Secrets Manager, if ever useful).
"""
import json
import os
from pathlib import Path

import boto3

from league_reports.config import load_all_config, year_range
from league_reports.reports.advanced_history import build_advanced_history
from league_reports.reports.head_to_head import build_head_to_head
from league_reports.reports.history import build_history
from league_reports.reports.owner_habits import build_owner_habits
from league_reports.reports.records import build_records
from league_reports.reports.weekly_summary import (
    DEFAULT_AWARDS,
    build_survivor_results,
    build_weekly_efficiency,
    build_weekly_payouts,
)

TMP_DIR = Path("/tmp")

CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
}

DEFAULT_LINEUP_CONFIG = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "D/ST": 1}

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _write_csv(df, filename):
    out = TMP_DIR / filename
    df.to_csv(out, index=False)
    return out


def _write_json(data, filename):
    out = TMP_DIR / filename
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    return out


def _upload(local_path, bucket):
    """Upload a /tmp file to s3://<bucket>/<same filename>, at the bucket
    root (DESIGN.md decision #7 - the site has no data/ prefix), with an
    explicit Content-Type (boto3 doesn't infer one from the extension)."""
    key = local_path.name
    content_type = CONTENT_TYPES.get(local_path.suffix, "application/octet-stream")
    print(f"  Uploading {local_path} -> s3://{bucket}/{key} ({content_type})")
    _s3().upload_file(str(local_path), bucket, key, ExtraArgs={"ContentType": content_type})


def _upload_csv(df, filename, bucket, skip_if_empty=False):
    if skip_if_empty and (df is None or df.empty):
        print(f"  {filename}: no data collected, skipping upload")
        return
    _upload(_write_csv(df, filename), bucket)


def _upload_json(data, filename, bucket):
    _upload(_write_json(data, filename), bucket)


def _step_history(league_config, owner_map, creds, bucket):
    df = build_history(
        league_id=league_config["league_id"],
        years=year_range(league_config, span="full"),
        owner_map=owner_map,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
    )
    _upload_csv(df, "league_history.csv", bucket)


def _step_head_to_head(league_config, creds, bucket):
    df = build_head_to_head(
        league_id=league_config["league_id"],
        years=year_range(league_config, span="full"),
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
    )
    _upload_csv(df, "head_to_head_lifetime.csv", bucket)


def _step_advanced_history(league_config, owner_map, creds, bucket):
    df = build_advanced_history(
        league_id=league_config["league_id"],
        years=year_range(league_config, span="box_score"),
        owner_map=owner_map,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
    )
    # Matches scripts/advanced_history.py: box_score() isn't reliable for
    # older seasons, so an empty result is expected/skipped, not an error.
    _upload_csv(df, "advanced_team_metrics.csv", bucket, skip_if_empty=True)


def _step_owner_habits(league_config, creds, bucket):
    df = build_owner_habits(
        league_id=league_config["league_id"],
        years=year_range(league_config, span="full"),
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
    )
    _upload_csv(df, "most_drafted_players.csv", bucket)


def _step_records(league_config, creds, bucket):
    df = build_records(
        league_id=league_config["league_id"],
        years=year_range(league_config, span="box_score"),
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
    )
    _upload_csv(df, "all_time_records.csv", bucket)


def _step_weekly_summary(league_config, payouts_config, creds, bucket):
    league_id = league_config["league_id"]
    year = league_config["years"]["current"]
    lineup_config = league_config.get("lineup", DEFAULT_LINEUP_CONFIG)
    awards = league_config.get("awards", DEFAULT_AWARDS)
    last_elimination_week = league_config.get("survivor", {}).get("last_elimination_week", 12)

    # Same order as scripts/weekly_summary.py: payouts computed first, but
    # written last (efficiency -> survivor -> payouts), since survivor
    # results are derived from the efficiency DataFrame.
    winners = build_weekly_payouts(
        league_id=league_id,
        year=year,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        payout_config=payouts_config,
    )

    efficiency_df = build_weekly_efficiency(
        league_id=league_id,
        year=year,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        lineup_config=lineup_config,
        awards=awards,
    )
    _upload_csv(efficiency_df, "weekly_efficiency_awards.csv", bucket)

    survivor_result = build_survivor_results(
        efficiency_df, last_elimination_week=last_elimination_week
    )
    _upload_json(survivor_result, "survivor_results.json", bucket)

    _upload_json(winners, "weekly_payout_winners.json", bucket)


def handler(event, context):
    """EventBridge Scheduler entrypoint (also invokable manually via the
    console/CLI - see DESIGN.md decision #9). `event`/`context` are unused:
    all configuration comes from league_reports.config.load_all_config(), not the
    invocation payload (see the config-source decision in DESIGN.md
    decision #4)."""
    league_config, owner_map, payouts_config, creds = load_all_config()
    bucket = os.environ["FF_SITE_BUCKET"]

    steps = [
        ("League history",
         lambda: _step_history(league_config, owner_map, creds, bucket)),
        ("Head-to-head records",
         lambda: _step_head_to_head(league_config, creds, bucket)),
        ("Advanced team metrics",
         lambda: _step_advanced_history(league_config, owner_map, creds, bucket)),
        ("Draft habits",
         lambda: _step_owner_habits(league_config, creds, bucket)),
        ("All-time records",
         lambda: _step_records(league_config, creds, bucket)),
        ("Weekly summaries / payouts / survivor pool",
         lambda: _step_weekly_summary(league_config, payouts_config, creds, bucket)),
    ]

    succeeded = []
    failed = []

    for label, step in steps:
        print(f"\n=== {label} ===")
        try:
            step()
            succeeded.append(label)
        except Exception as e:
            print(f"FAILED: {label}: {e}")
            failed.append(label)

    print("\n=== Done ===")
    print(f"{len(succeeded)} step(s) succeeded: {', '.join(succeeded) or '(none)'}")
    if failed:
        print(f"{len(failed)} step(s) failed: {', '.join(failed)}")

    result = {"bucket": bucket, "succeeded": succeeded, "failed": failed}
    if failed:
        # Surface as a Lambda error (non-2xx CloudWatch metric) so a fully
        # broken run is visible, without having stopped any step that could
        # still complete. See TODO-backend.md "Deferred" for actual alerting.
        raise RuntimeError(f"{len(failed)} report(s) failed: {failed}; result={result}")
    return result


if __name__ == "__main__":
    # Local smoke test: FF_CONFIG_BACKEND defaults to "local", so this
    # runs against ignore/espn_creds.json etc. exactly like scripts/run_all.py,
    # except it uploads to S3 instead of writing to the project root - set
    # FF_SITE_BUCKET to a scratch bucket/prefix before running this
    # directly. Prefer `python scripts/run_all.py` for pure local iteration.
    handler({}, None)
