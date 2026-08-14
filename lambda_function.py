"""Lambda entrypoint: regenerate every data file for the configured league
and upload it to the site bucket.

Mirrors scripts/run_all.py's structure (one try/except per step so one ESPN
hiccup doesn't abort the rest) but writes to /tmp and uploads to S3 instead
of writing to the project root. See .claude/DESIGN.md decision #7 for the
exact output filenames/Content-Types and decision #4 for where config comes
from.

`event.get("steps")` selects which steps run (DESIGN.md decision #10b): a
list of STEPS keys, e.g. `{"steps": ["head_to_head_v2"]}`; an unknown name
is a hard ValueError, not a silent skip. No "steps" key at all falls back
to DEFAULT_STEPS.

Cutover (see .claude/DESIGN-incremental-espn-pipeline.md decision #13 and
.claude/TODO-backend.md's "records.py per-year cache" item): DEFAULT_STEPS
runs the `_v2` builds (head_to_head_v2, advanced_history_v2,
weekly_summary_v2, records_v2) in place of their legacy counterparts
(head_to_head, advanced_history, weekly_summary, records) - cache-aware
(league_reports.cache + league_reports.box_score_cache), publishing
straight to the real bucket-root keys, same filenames the site already
fetches. `history` has no _v2 replacement (no per-year/per-week cost worth
caching - see advanced_history_v2.py's docstring) and stays legacy;
`owner_habits` stays excluded from DEFAULT_STEPS same as always (draft
picks only change on draft day, never implicit). The legacy
`head_to_head`/`advanced_history`/`weekly_summary`/`records` step
functions and their v1 report modules are untouched and still callable by
name (`{"steps": ["head_to_head"]}`) if ever needed - this was a
step-registry cutover, not a code deletion. Not scoped to multi-league
(DESIGN.md decision #12) yet - league_id comes from the single configured
league_config.json, not an event["league_ids"] list.

Pre-cutover, the `_v2` steps ran in shadow mode: an unconditional write to
a private `leagues/<league_id>/shadow/` prefix, plus an opt-in,
flag-gated (AllowV2RootPublish) second write to the real keys, so they
could run alongside the legacy steps and get diffed before anything public
depended on them. Retired once cutover landed and proved out (see git
history / DESIGN-incremental-espn-pipeline.md) - the `_v2` steps now
write directly to the real keys, nothing else, same as every legacy step
always has.

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

from league_reports.cache import put_cached_year
from league_reports.config import load_all_config, year_range
from league_reports.reports.advanced_history import build_advanced_history
from league_reports.reports.advanced_history_v2 import build_advanced_history_v2_cached
from league_reports.reports.head_to_head import build_head_to_head
from league_reports.reports.head_to_head_v2 import build_head_to_head_v2_cached
from league_reports.reports.history import build_history
from league_reports.reports.owner_habits import build_owner_habits
from league_reports.reports.records import build_records
from league_reports.reports.records_v2 import build_records_v2_cached
from league_reports.reports.weekly_summary import (
    DEFAULT_AWARDS,
    build_survivor_results,
    build_weekly_efficiency,
    build_weekly_payouts,
)
from league_reports.reports.weekly_summary_v2 import (
    DEFAULT_AWARDS as DEFAULT_AWARDS_V2,
    build_survivor_results_v2,
    build_weekly_efficiency_v2_cached,
    build_weekly_payouts_v2_cached,
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


def _upload(local_path, bucket, key=None):
    """Upload a /tmp file to s3://<bucket>/<key>. key defaults to the bare
    filename at the bucket root (.claude/DESIGN.md decision #7 - the site
    has no data/ prefix) - every step but the weekly-summary archive writes
    below relies on that default unchanged. Content-Type is always
    explicit - boto3 doesn't infer one from the extension."""
    key = key or local_path.name
    content_type = CONTENT_TYPES.get(local_path.suffix, "application/octet-stream")
    print(f"  Uploading {local_path} -> s3://{bucket}/{key} ({content_type})")
    _s3().upload_file(str(local_path), bucket, key, ExtraArgs={"ContentType": content_type})


def _upload_csv(df, filename, bucket, skip_if_empty=False, key=None):
    if skip_if_empty and (df is None or df.empty):
        print(f"  {filename}: no data collected, skipping upload")
        return
    _upload(_write_csv(df, filename), bucket, key=key)


def _upload_json(data, filename, bucket, key=None):
    _upload(_write_json(data, filename), bucket, key=key)


def _archive_key(league_id, filename):
    """DESIGN.md decision #11: season-stamped archive copies live at
    leagues/<league_id>/archive/<filename>, public-read like the rest of
    the bucket - not a shadow/private prefix, this is meant to be fetched."""
    return f"leagues/{league_id}/archive/{filename}"


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


def _step_head_to_head_v2(league_config, creds, bucket):
    league_id = league_config["league_id"]
    df = build_head_to_head_v2_cached(
        league_id=league_id,
        years=year_range(league_config, span="full"),
        current_year=league_config["years"]["current"],
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        bucket=bucket,
    )
    _upload_csv(df, "head_to_head_lifetime.csv", bucket)


def _step_advanced_history_v2(league_config, owner_map, creds, bucket):
    league_id = league_config["league_id"]
    df = build_advanced_history_v2_cached(
        league_id=league_id,
        years=year_range(league_config, span="box_score"),
        current_year=league_config["years"]["current"],
        owner_map=owner_map,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        bucket=bucket,
    )
    # Matches _step_advanced_history: box_scores() isn't reliable for older
    # seasons, so an empty result is expected/skipped, not an error.
    _upload_csv(df, "advanced_team_metrics.csv", bucket, skip_if_empty=True)


def _step_records_v2(league_config, creds, bucket):
    league_id = league_config["league_id"]
    df = build_records_v2_cached(
        league_id=league_id,
        years=year_range(league_config, span="box_score"),
        current_year=league_config["years"]["current"],
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        bucket=bucket,
    )
    _upload_csv(df, "all_time_records.csv", bucket)


def _step_weekly_summary_v2(league_config, payouts_config, creds, bucket, year=None):
    """DESIGN.md decision #11. year=None (the default, what the schedule
    always passes): processes league_config["years"]["current"] and updates
    the current-facing files, same as before this decision. year=<a past
    season>: backfills that season instead - the current-facing files are
    left alone (they only ever reflect the true current season, per
    decision #11's contract), but the archive/cache writes below still
    happen, so any past season's weekly data can be (re)built on demand:
    {"steps": ["weekly_summary_v2"], "year": 2024}.

    real_current_year (not target_year) is what gets passed as every
    builder's current_year - that's the value the box_score_cache
    closedness check (`year < current_year`) needs to correctly treat a
    backfilled season as fully closed, including its very last week (a
    plain `week < league.current_week` check alone would leave that one
    week perpetually uncached - see advanced_history_v2.py's docstring for
    why). This was already a latent gap before this decision existed
    (nothing called this step with year != current before), so fixing it
    falls out naturally here rather than needing its own separate change."""
    league_id = league_config["league_id"]
    real_current_year = league_config["years"]["current"]
    target_year = year if year is not None else real_current_year
    is_current = target_year == real_current_year

    lineup_config = league_config.get("lineup", DEFAULT_LINEUP_CONFIG)
    awards = league_config.get("awards", DEFAULT_AWARDS_V2)
    last_elimination_week = league_config.get("survivor", {}).get("last_elimination_week", 12)

    # Same order as _step_weekly_summary: payouts computed first, but
    # written last (efficiency -> survivor -> payouts), since survivor
    # results are derived from the efficiency DataFrame.
    winners = build_weekly_payouts_v2_cached(
        league_id=league_id,
        year=target_year,
        current_year=real_current_year,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        payout_config=payouts_config,
        bucket=bucket,
    )

    efficiency_df = build_weekly_efficiency_v2_cached(
        league_id=league_id,
        year=target_year,
        current_year=real_current_year,
        swid=creds["swid"],
        espn_s2=creds["espn_s2"],
        lineup_config=lineup_config,
        bucket=bucket,
        awards=awards,
    )

    survivor_result = build_survivor_results_v2(
        efficiency_df, last_elimination_week=last_elimination_week
    )

    # Archival cache (decision #11) - always writes, current or backfilled,
    # unlike decision #10a's closed-years-only re-fetch cache. Not gated on
    # is_closed since this is retention, not re-fetch avoidance: the point
    # is a durable record of what this year's weekly data looked like, not
    # skipping ESPN calls (box_score_cache above already did that part).
    put_cached_year(bucket, league_id, "weekly_efficiency", target_year, efficiency_df.to_dict(orient="records"))
    put_cached_year(bucket, league_id, "survivor_results", target_year, survivor_result)
    put_cached_year(bucket, league_id, "weekly_payouts", target_year, winners)

    # Season-stamped archive copies (decision #11) - public, published,
    # always written - so target_year's final weekly data survives a later
    # season's rollover instead of being silently overwritten.
    _upload_csv(efficiency_df, "weekly_efficiency_awards.csv", bucket,
                key=_archive_key(league_id, f"weekly_efficiency_awards_{target_year}.csv"))
    _upload_json(survivor_result, "survivor_results.json", bucket,
                 key=_archive_key(league_id, f"survivor_results_{target_year}.json"))
    _upload_json(winners, "weekly_payout_winners.json", bucket,
                 key=_archive_key(league_id, f"weekly_payout_winners_{target_year}.json"))

    # Current-facing files - only when this run reflects the real current
    # season. A backfill run must never overwrite live current-season data
    # with a past season's - that's the one thing decision #11 explicitly
    # protects ("the three current-facing files keep their exact contract").
    if is_current:
        _upload_csv(efficiency_df, "weekly_efficiency_awards.csv", bucket)
        _upload_json(survivor_result, "survivor_results.json", bucket)
        _upload_json(winners, "weekly_payout_winners.json", bucket)


# DESIGN.md decision #10b's step registry: one entry per _step_* function,
# uniform (league_config, owner_map, payouts_config, creds, bucket, year)
# signature regardless of which args a given step actually needs, so
# handler() doesn't need to know each step's individual shape. `year`
# (decision #11) is ignored by every step but weekly_summary_v2 - same
# "uniform signature, steps ignore what they don't need" pattern
# owner_map/payouts_config already follow.
STEPS = {
    "history": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_history(league_config, owner_map, creds, bucket),
    "head_to_head": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_head_to_head(league_config, creds, bucket),
    "advanced_history": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_advanced_history(league_config, owner_map, creds, bucket),
    "owner_habits": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_owner_habits(league_config, creds, bucket),
    "records": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_records(league_config, creds, bucket),
    "weekly_summary": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_weekly_summary(league_config, payouts_config, creds, bucket),
    "head_to_head_v2": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_head_to_head_v2(league_config, creds, bucket),
    "advanced_history_v2": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_advanced_history_v2(league_config, owner_map, creds, bucket),
    "records_v2": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_records_v2(league_config, creds, bucket),
    "weekly_summary_v2": lambda league_config, owner_map, payouts_config, creds, bucket, year:
        _step_weekly_summary_v2(league_config, payouts_config, creds, bucket, year=year),
}

STEP_LABELS = {
    "history": "League history",
    "head_to_head": "Head-to-head records",
    "advanced_history": "Advanced team metrics",
    "owner_habits": "Draft habits",
    "records": "All-time records",
    "weekly_summary": "Weekly summaries / payouts / survivor pool",
    "head_to_head_v2": "Head-to-head records (v2)",
    "advanced_history_v2": "Advanced team metrics (v2)",
    "records_v2": "All-time records (v2)",
    "weekly_summary_v2": "Weekly summaries / payouts / survivor pool (v2)",
}

# Cutover (decision #13): head_to_head/advanced_history/records/weekly_summary
# run via their _v2 replacements now, not the legacy modules. history has no
# _v2 counterpart and stays legacy. owner_habits stays excluded (draft picks
# only change on draft day - never implicit, decision #10b).
DEFAULT_STEPS = ["history", "head_to_head_v2", "advanced_history_v2", "records_v2", "weekly_summary_v2"]


def handler(event, context):
    """EventBridge Scheduler entrypoint (also invokable manually via the
    console/CLI - see .claude/DESIGN.md decision #9). `context` is unused;
    `event` supplies an optional "steps" list (DESIGN.md decision #10b) and
    an optional "year" (DESIGN.md decision #11, weekly_summary_v2's
    backfill override - every other step ignores it) - see the module
    docstring. All other configuration still comes from
    league_reports.config.load_all_config(), not the invocation payload
    (see the config-source decision in .claude/DESIGN.md decision #4)."""
    league_config, owner_map, payouts_config, creds = load_all_config()
    bucket = os.environ["FF_SITE_BUCKET"]
    year = event.get("year")

    requested = event.get("steps")
    if requested is None:
        step_names = DEFAULT_STEPS
    else:
        unknown = [name for name in requested if name not in STEPS]
        if unknown:
            raise ValueError(f"Unknown step(s): {unknown}. Valid steps: {sorted(STEPS.keys())}")
        step_names = requested

    succeeded = []
    failed = []

    for name in step_names:
        label = STEP_LABELS.get(name, name)
        print(f"\n=== {label} ===")
        try:
            STEPS[name](league_config, owner_map, payouts_config, creds, bucket, year)
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
        # still complete. See .claude/TODO-backend.md "Deferred" for actual alerting.
        raise RuntimeError(f"{len(failed)} report(s) failed: {failed}; result={result}")
    return result


if __name__ == "__main__":
    # Local smoke test: FF_CONFIG_BACKEND defaults to "local", so this
    # runs against ignore/espn_creds.json etc. exactly like scripts/run_all.py,
    # except it uploads to S3 instead of writing to the project root - set
    # FF_SITE_BUCKET to a scratch bucket/prefix before running this
    # directly. Prefer `python scripts/run_all.py` for pure local iteration.
    handler({}, None)
