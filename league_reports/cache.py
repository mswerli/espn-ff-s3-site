"""S3-only per-year cache for report partials (.claude/DESIGN.md decision #10a,
extended for the current year vs. per-week question in
.claude/DESIGN-incremental-espn-pipeline.md decision #13).

Lambda-only, like config.py's S3 backend - local scripts never touch this,
so boto3 is imported lazily inside the functions below rather than at
module load time.

Key shape: s3://<bucket>/leagues/<league_id>/cache/<report>/<year>.json -
one JSON object per (league, report, year). "report" is a report-module
name (e.g. "head_to_head"), not a filename - this is intermediate computed
data, not a published output, so it doesn't need to match any CSV/JSON
filename the front-end fetches.

A year is "closed" once it can never produce different data again
(year < league_config["years"]["current"]) - that comparison is the
caller's job, not this module's (this module has no opinion on what
"closed" means, it just reads/writes/orchestrates against whatever the
caller decides). Closed years are cached forever once computed; the
current year is always recomputed and never read from or written to cache.
"""
import json


def _cache_key(league_id, report, year):
    return f"leagues/{league_id}/cache/{report}/{year}.json"


def list_cached_years(bucket, league_id, report):
    """Returns every year actually cached for (league_id, report), sorted.

    DESIGN.md decision #15h: unlike get_cached_year/put_cached_year (keyed
    off a year the caller already knows to ask about - a season it fetched
    or a season < years["current"]), this exists for manually-seeded years
    a league's own league_config.json's "years" range doesn't cover at all
    (e.g. scripts/seed_history_cache.py's transcribed pre-ESPN seasons for a
    league migrated from NFL.com) - there's no "closed years" comparison to
    derive the year list from, so it has to come from what's actually in
    S3. Uses the same leagues/* prefix the ReadWriteLeaguesPrefix IAM policy
    already grants s3:ListBucket on - no new IAM surface needed."""
    import boto3

    client = boto3.client("s3")
    prefix = f"leagues/{league_id}/cache/{report}/"
    years = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix):]
            if name.endswith(".json"):
                try:
                    years.append(int(name[:-len(".json")]))
                except ValueError:
                    continue
    return sorted(years)


def get_cached_year(bucket, league_id, report, year):
    """Returns the cached partial for (league_id, report, year), or None if
    nothing's cached yet for it."""
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("s3")
    key = _cache_key(league_id, report, year)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())


def put_cached_year(bucket, league_id, report, year, data):
    """Writes (overwrites) the cached partial for (league_id, report, year)."""
    import boto3

    client = boto3.client("s3")
    key = _cache_key(league_id, report, year)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )


def get_or_compute_year(bucket, league_id, report, year, is_closed, compute_fn):
    """The per-year cache-or-compute decision every report's orchestrator
    makes, in one place instead of copy-pasted per report:

    - is_closed=True: read the cache; on a hit, return it without calling
      compute_fn at all (no ESPN call). On a miss (first time this season
      is ever processed), call compute_fn(), cache the result, return it.
    - is_closed=False (the current season): always call compute_fn(), never
      read or write the cache - decision #10a's "current year is never
      cached" rule.
    """
    if is_closed:
        cached = get_cached_year(bucket, league_id, report, year)
        if cached is not None:
            return cached

    data = compute_fn()

    if is_closed:
        put_cached_year(bucket, league_id, report, year, data)

    return data
