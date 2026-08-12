"""League() construction, shared by every report and by both the local CLI
and (eventually) the Lambda handler.

ESPN's API is undocumented and occasionally drops connections mid-request
(observed directly while porting this code: `ConnectionError`/timeout on an
otherwise-successful season fetch). A couple of retries with a short backoff
absorbs that without masking real failures (bad league id, expired cookies,
a season ESPN genuinely has no data for).
"""
import time

from espn_api.football import League
from requests.exceptions import ConnectionError, Timeout

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2


def get_league(league_id, year, swid, espn_s2, retries=DEFAULT_RETRIES, backoff=DEFAULT_BACKOFF_SECONDS):
    """Construct a League(), retrying transient connection failures.

    Raises the last exception if all attempts fail - callers already catch
    and log/skip per-season failures, so this doesn't change behavior for
    non-transient errors, it just stops one dropped connection from
    aborting an otherwise-successful multi-year run.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return League(league_id=league_id, year=year, swid=swid, espn_s2=espn_s2)
        except (ConnectionError, Timeout) as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_error
