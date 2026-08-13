"""Shared raw box-score cache (.claude/DESIGN-incremental-espn-pipeline.md
decision #13, part C).

`box_scores(week)` is the one ESPN resource that's genuinely server-side
filtered per week (scoringPeriodId + filterMatchupPeriodIds) - unlike
scoreboard() (league.py), which re-fetches the whole season every call
regardless of the week argument (see head_to_head_v2.py's docstring).
advanced_history.py and both weekly_summary.py builders each call
box_scores(week) separately for the same (year, week) today, up to 3x
redundant per week. This caches the trimmed result once per
(league, year, week) so every _v2 consumer reads the same cached JSON
instead of hitting ESPN again.

Lambda-only, like config.py's S3 backend and league_reports/cache.py - boto3
imported lazily. Key shape:
s3://<bucket>/leagues/<league_id>/raw/<year>/box_scores/week_<n>.json
(a private/internal prefix, not a published output - front-end never
fetches this).

A week is "closed" the same way league_reports.cache treats a year: once it
can never produce different data again. The caller decides what that means
(typically week < league.currentMatchupPeriod, or the whole season already
being closed per league_reports.cache's year check) and passes is_closed
in - this module has no opinion on it, same split of responsibility as
league_reports.cache.get_or_compute_year.

Payload is trimmed to only the fields any current _v2 consumer reads - not
the full BoxScore/BoxPlayer shape, which carries pro_opponent/pro_pos_rank/
game_played/on_bye_week fields nothing here uses (see espn_api's
box_player.py) - so the cache is smaller and decoupled from espn_api's
internal class shapes:

{
  "home_team_id": int, "home_score": float,
  "away_team_id": int, "away_score": float,
  "home_lineup": [{"player_id", "name", "position", "slot_position", "points"}, ...],
  "away_lineup": [...]
}
"""
import json


def _cache_key(league_id, year, week):
    return f"leagues/{league_id}/raw/{year}/box_scores/week_{week}.json"


def _trim_lineup(lineup):
    return [
        {
            "player_id": p.playerId,
            "name": p.name,
            "position": p.position,
            "slot_position": p.slot_position,
            "points": p.points,
        }
        for p in lineup
    ]


def _trim_box_scores(box_scores):
    """box_scores: what league.box_scores(week) returns (a list of espn_api
    BoxScore objects) -> a plain JSON-safe list of trimmed dicts, one per
    matchup. Skips bye weeks the same way advanced_history.py already does
    (a bye's home_team/away_team come back as a bare int, not a Team)."""
    trimmed = []
    for box in box_scores:
        if isinstance(box.home_team, int) or isinstance(box.away_team, int):
            continue
        trimmed.append({
            "home_team_id": box.home_team.team_id,
            "home_score": box.home_score,
            "away_team_id": box.away_team.team_id,
            "away_score": box.away_score,
            "home_lineup": _trim_lineup(box.home_lineup),
            "away_lineup": _trim_lineup(box.away_lineup),
        })
    return trimmed


def _get_cached(bucket, league_id, year, week):
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("s3")
    key = _cache_key(league_id, year, week)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())


def _put_cached(bucket, league_id, year, week, data):
    import boto3

    client = boto3.client("s3")
    key = _cache_key(league_id, year, week)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json",
    )


def get_box_scores(league, league_id, year, week, bucket, is_closed):
    """Returns trimmed box-score data (the JSON shape documented at module
    level) for (league_id, year, week): read from cache when is_closed and
    already cached; otherwise fetched live via league.box_scores(week) and,
    if is_closed, cached for next time. `league` must already be
    constructed for (league_id, year) - this never constructs its own."""
    if is_closed:
        cached = _get_cached(bucket, league_id, year, week)
        if cached is not None:
            return cached

    trimmed = _trim_box_scores(league.box_scores(week))

    if is_closed:
        _put_cached(bucket, league_id, year, week, trimmed)

    return trimmed
