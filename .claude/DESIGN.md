# espn-ff-s3-site — AWS Hosting Design

Status: Phase 1 (Prep, `league_reports/`) and Phase 2 (static hosting) are done, see .claude/TODO-frontend.md.
Phases 3+ (Lambda automation, season-cache, multi-league) status lives in .claude/TODO-backend.md. What's
actually live in AWS right now: [[espn-ff-live-infra]] memory.

Sibling repo: a separate, older repo holds the original front-end (`index.html`/`style.css`) and
local ESPN-fetch scripts (`scripts/*.py`, `helpers/utilities.py`) this design replaces the
data-generation half of. It remains the **live GitHub Pages source today** and is intentionally
unmodified. The copy of the front-end under this repo's `site/` (see decision #7) has already
diverged from it — it picked up a config-driven title/subtitle read from `league_config.json` — and
going forward `site/` here is the one that evolves; that original repo isn't a live upstream to sync
from anymore.

## Goal

Move the site from "run scripts locally, copy CSVs next to `index.html`" to:

- a plain S3 bucket serving the static site (no CloudFront, no custom domain — S3 website endpoint,
  HTTP is acceptable for this project)
- an AWS Lambda that regenerates the CSV/JSON data files on a weekly schedule, taking a
  league configuration as input instead of hardcoded local paths
- AWS SAM as the one deployment tool for all of the infrastructure (bucket, Lambda, schedule, role)

## Current state (original repo, as of this design)

**Front-end**: `index.html` + `style.css`, vanilla JS, no build step. Does relative `fetch()` calls
against CSV/JSON files sitting next to it — e.g. `fetch("league_history.csv")`. This works unchanged
under S3 website hosting since it's all same-origin.

**Data generation**: six scripts under `scripts/` (`get_history.py`, `head_to_head.py`,
`owner_habits.py`, `records.py`, `advanced_history.py`, `weekly_summary.py`) that each:

1. load `league_config.json` (league id, year range, site title, award names),
   `config/weekly_payouts_config.json`, and two gitignored files —
   `ignore/espn_creds.json` (ESPN `swid`/`espn_s2` session cookies) and
   `ignore/owner_map.json` (team-id → owner-initials)
2. loop over configured years, call the `espn_api` Python library, crunch stats with pandas
3. write a CSV/JSON straight into the project root

Outputs consumed by the page: `league_history.csv`, `head_to_head_lifetime.csv`,
`most_drafted_players.csv`, `all_time_records.csv`, `advanced_team_metrics.csv`,
`weekly_efficiency_awards.csv`, `weekly_payout_winners.json`, `survivor_results.json`.

This is entirely manual today: someone runs the scripts locally and the outputs get copied wherever
the site is served from. No `requirements.txt` exists yet.

## Target architecture

```
EventBridge Scheduler (weekly cron, in-season)
        │
        ▼
   Lambda (data-generator)
   ├─ reads config from S3 (or the scheduler's fixed input payload) — league id, years,
   │  owner map, payouts rules, awards
   ├─ reads ESPN cookies from Secrets Manager
   ├─ calls espn_api, builds the same 8 CSV/JSON outputs (refactored from scripts/)
   └─ uploads outputs to  s3://<site-bucket>/*.csv, *.json   (bucket root — see decision #7)
        │
        ▼
   S3 bucket (static website hosting, plain HTTP endpoint — no CloudFront)
   ├─ index.html, style.css        ← synced from git on demand, rarely changes
   └─ *.csv, *.json                ← overwritten weekly by the Lambda, never committed to git
```

## Key decisions

### 1. Plain S3 static website hosting, no CloudFront

S3 website-hosting endpoints are HTTP-only. That's accepted for this project — no custom domain, no
HTTPS requirement. This drops CloudFront, ACM, and Route 53 entirely, which is most of the
infrastructure complexity a hobby fantasy-football site doesn't need. If HTTPS/a custom domain is
wanted later, CloudFront-in-front-of-S3 is a additive change, not a redesign.

`WebsiteConfiguration`'s `ErrorDocument` reuses `index.html` (same as `IndexDocument`) rather than a
dedicated error page — none was designed, and this is a one-page app anyway (tab-based navigation,
no real sub-paths), so a missing/mistyped path re-rendering the same app shell is a reasonable
default. A hard 404 or a small custom error page is a cheap follow-up if it's ever wanted (`site/`
would just need one more file and `template.yaml`'s `ErrorDocument` a one-line change).

### 2. AWS SAM for all infrastructure, including the bucket

SAM templates are CloudFormation underneath, so `template.yaml` declares everything in one stack:
the S3 bucket (with `WebsiteConfiguration` and a public-read bucket policy), the Lambda function, its
execution role, the Lambda layer, and the EventBridge schedule. One `sam deploy` stands up or updates
the whole stack.

SAM does **not** upload arbitrary files (`index.html`, `style.css`) into the bucket — `sam deploy`
only pushes infrastructure and Lambda code. Static asset publishing is a separate, simple step:

```
sam build && sam deploy                                      # infra + Lambda code
aws s3 sync site/ s3://$BUCKET/                               # static assets (rare changes)
aws s3 cp league_config.json s3://$BUCKET/league_config.json  # config the front-end also reads —
                                                                # canonical copy lives at repo root,
                                                                # not in site/ (see decision #4)
# The Lambda's own outputs (*.csv, survivor_results.json, weekly_payout_winners.json) are never
# synced from git — only the Lambda writes those, straight to the bucket root.
```

### 3. Lambda packaging: AWS-managed Pandas layer, zip-based, no container image

pandas/numpy are usually the reason people reach for container-image Lambdas, but AWS publishes a
public layer (`AWSSDKPandas-Python31x`) with pandas + numpy prebuilt. `espn_api` is pure
Python/`requests` and small. So: a normal zip-based Lambda plus that one managed layer — no ECR, no
Docker build step, faster iteration.

This means the repo-root `requirements.txt` that `sam build` packages into the Lambda **must not**
list pandas: `sam build`'s default Python builder reads exactly `requirements.txt` and `pip install`s
it straight into `/var/task`, on top of what the layer already provides at `/opt/python`. Bundling
pandas there too caused two real problems, not a hypothetical one: pandas' unpinned `numpy>=1.23.2`
floor let `pip` resolve to whatever the newest numpy release was on a given day, and one such day that
release had no linux/cp311 wheel yet, breaking `sam build` outright; and even when the resolve
succeeds, AWS's docs say deployment-package versions shadow layer versions on the import path, so a
bundled pandas would silently win over the layer's pandas rather than the layer actually being used —
defeating the entire point of this decision. Fix: `requirements.txt` lists only `espn-api`; a separate
`requirements-dev.txt` (`-r requirements.txt` plus `pandas`) is what local dev/`scripts/*.py` install
instead, since local runs have no Lambda layer supplying pandas for them.

**A second, more serious packaging problem surfaced once `sam build` actually succeeded**: `CodeUri`
is the whole repo root (`lambda_function.py` lives there per the target layout below), and the repo
root also holds `ignore/` — real ESPN `swid`/`espn_s2` session cookies. `sam build`'s default Python
builder has no working user-configurable exclude for its CopySource step: it only skips a small
hardcoded list (`.git`, `.venv`, `__pycache__`, etc.), and — despite general SAM CLI docs describing a
`.samignore` file as the way to add more exclusions — that support does not actually exist in the
Python pip build workflow shipped in the AWS SAM CLI version this was verified against (confirmed by
reading `aws_lambda_builders`' source directly, not just by a failed test: the `CopySourceAction` class
does take an `excludes` list, but the Python pip workflow only ever passes its own hardcoded
`EXCLUDED_FILES` tuple, never anything read from a `.samignore` file). A `.samignore` at the repo root
was tried first and confirmed, via an actual `sam build`, to have **no effect** —
`ignore/espn_creds.json` still landed in `.aws-sam/build/DataGeneratorFunction/`, which is exactly what
`sam deploy` zips and uploads, directly violating this same decision's "Secrets Manager, never
alongside public site data in S3" further down. (`.samignore` may well do something for other language
workflows/build methods in other SAM CLI versions — it just isn't this one, for this workflow — so
don't assume it works without testing against the actual `sam build` output again if this changes.)

The fix that actually works: `template.yaml`'s `DataGeneratorFunction` sets
`Metadata: BuildMethod: makefile`, and the repo-root `Makefile` has a `build-DataGeneratorFunction`
target that copies `lambda_function.py` + `league_reports/` and `pip install`s `requirements.txt` into
`$ARTIFACTS_DIR` explicitly — an **allow-list**, not a deny-list. Nothing else in the repo (`ignore/`,
`site/`, `scripts/`, `config/`, `league_config.json`, docs, `.git/`, `.venv/`, ...) is ever a build
input candidate, regardless of what gets added to the repo later — the opposite failure mode of a
deny-list, which only protects against files someone remembered to add to it. Verified: `sam build`
with this in place produces an artifact directory containing only `lambda_function.py`, `league_reports/`,
and `requirements.txt`'s installed dependencies — confirmed by grepping the entire build output for
fragments of the real ESPN cookies and finding none, and by `find`ing for `ignore/`, `site/`,
`scripts/`, `.git`, `.venv`, `.claude` anywhere under `.aws-sam/build/` and finding nothing.

### 4. Config vs. secrets are split — and `league_config.json` specifically is public

- `league_config.json` is no longer Lambda-input-only: `site/index.html` now fetches it directly
  (`fetch("league_config.json")`, with a `.catch()` that falls back to hardcoded defaults) to render
  the site title/subtitle, so the page itself is config-driven rather than hardcoded per league. That
  means `league_config.json` must be publicly readable at the site bucket root, same as the CSVs.
  Its canonical location stays where `league_reports/config.py`'s `LEAGUE_CONFIG_PATH` already puts it — the
  **project root**, alongside `league_reports/`, not inside `site/` — since that's already built, tested, and
  committed, and both the Lambda and the local scripts read it from there. Rather than moving it (and
  touching tested code) or duplicating it into `site/`, the deploy step copies that one file to the
  bucket root as an explicit step alongside the `site/` sync (see decision #7). Same Lambda config
  input, same file the browser fetches, no drift — just not literally inside `site/` on disk.
- `config/weekly_payouts_config.json` and `owner_map.json`: still not sensitive, but not fetched by
  the browser (only used server-side, by the Lambda, to build payout rules and owner initials). These
  can stay in a private S3 config prefix (or get passed directly in the EventBridge invocation
  payload) — no reason to expose them publicly just because `league_config.json` now needs to be.
- ESPN `swid`/`espn_s2` cookies: real session credentials. These go in **Secrets Manager**, never
  alongside public site data in S3.

**Decided (backend build): S3 config prefix, not the EventBridge payload.** Concretely:

- The Lambda's own `league_config.json` input is read from `s3://<site-bucket>/league_config.json` —
  the exact same object the frontend deploy step publishes for the browser to fetch (see decision #7).
  One object, two consumers, no separate Lambda-only copy to keep in sync.
- `owner_map.json` and `weekly_payouts_config.json` live under a `config/` prefix in the same bucket
  (`s3://<site-bucket>/config/owner_map.json`, `.../config/weekly_payouts_config.json`) rather than in
  the EventBridge Scheduler's invocation payload. Reasons: (1) EventBridge Scheduler input is a fixed
  JSON blob baked into the schedule resource itself, so changing config (a new season's payout rules,
  a roster change to `owner_map.json`) would mean a template/stack update instead of a plain `aws s3
  cp`; (2) the Lambda already needs S3 read access for `league_config.json` per the point above, so
  granting `s3:GetObject` on two more keys in the same bucket is a marginal addition, not a new
  mechanism; (3) it keeps `lambda_function.py`'s `event`/`context` params unused, so the same handler
  can be invoked identically from the schedule, the console, or the CLI with no payload to remember.
  The frontend bucket policy makes the whole bucket public-read anyway (see .claude/TODO-frontend.md), so
  "private prefix" here is organizational, not an actual access boundary — consistent with these two
  files already being "not sensitive" above.
- `league_reports/config.py` implements this as an S3+Secrets-Manager backend selected by one env var
  (`FF_CONFIG_BACKEND=s3`, set on the Lambda in `template.yaml`), alongside — not replacing — the
  local-file backend local scripts already use (`FF_CONFIG_BACKEND` defaults to `"local"`).

**Extended by decision #12 below** for multi-league support: the single fixed keys here
(`league_config.json`, `config/owner_map.json`, `config/weekly_payouts_config.json`) become per-league
paths under a `leagues/<league_id>/` prefix once more than one league is configured. The Secrets
Manager secret stays a single shared one across leagues (see #12) — nothing here changes for it.

### 5. Avoid re-fetching the entire league history every run

The original scripts re-fetch *every* configured year from ESPN on every run. Once a season ends its
stats don't change, so the Lambda should cache each prior season's computed output in S3 and, on each
weekly run, only re-fetch/recompute the **current** season and merge it with the cached history. This
keeps each invocation well inside Lambda's 15-minute limit and avoids hammering ESPN's undocumented
API. If league history ever grows enough that this isn't sufficient, the natural next step is a Step
Functions state machine with one Lambda per report type run in parallel — not needed at today's data
volume.

**Superseded by decisions #10 and #11 below**, which turn this from a one-line aspiration into a
concrete per-year cache design plus the cadence tiers (live / weekly / on-demand) that decide *when*
each report even needs the current season re-fetched (#10), and — since "only re-fetch the current
season" turned out to have its own history-loss problem for the weekly-summary reports specifically —
how that same cache module also keeps past seasons' data around instead of letting it get overwritten
at each rollover (#11).

### 6. Shared code between the Lambda and local dev scripts

Split each script's "fetch from ESPN → compute → write CSV to a hardcoded local path" into
importable compute functions, so both the Lambda handler and a local CLI call the same logic:

```
league_reports/
  espn_client.py        # League() construction, retry/backoff
  config.py              # local file, OR S3, OR Secrets Manager, based on env
  reports/
    history.py           # build_history(...) -> DataFrame        (was scripts/get_history.py)
    head_to_head.py
    owner_habits.py
    records.py
    advanced_history.py
    weekly_summary.py
lambda_function.py       # handler: load config+creds, run reports, write /tmp, upload to S3
scripts/                 # thin CLI wrappers for local runs/debugging, unchanged local behavior
```

Local dev keeps working like the current repo (same `ignore/*.json` files, same
write-to-project-root behavior) — the Lambda path is additive, not a replacement for local iteration.

### 7. Publishing — flat bucket root, not a `data/` prefix

`site/index.html` fetches its data with bare relative filenames — `fetch("league_history.csv")`,
`fetch("head_to_head_lifetime.csv")`, etc. — so it expects those files in the **same directory** as
`index.html`, not under a subfolder. An earlier draft of this design had the Lambda writing to a
`data/` prefix; that was a front-end/infra mismatch (every table would have 404'd) and has been
corrected here: the Lambda writes flat to the bucket root, matching exactly what the existing local
scripts already do via `output_path()`. This also means the local CSV output path and the S3 upload
path are identical, so there's one thing less to keep in sync.

(`site/index.html` isn't a byte-for-byte copy of the original repo's `index.html` — it also
picked up a config-driven title/subtitle: `id="site-title"`/`id="site-subtitle"` header elements plus
a `fetch("league_config.json")` block, with a `.catch()` that falls back to the hardcoded defaults if
the file is missing. See decision #4.)

The Lambda must upload each file under the **exact same name** the front-end already fetches
(case-sensitive, S3 keys aren't forgiving like a case-insensitive local filesystem):

| File | Written by |
|---|---|
| `league_config.json` | `aws s3 cp` from the repo root, alongside the `site/` sync — see decision #4; not the Lambda |
| `league_history.csv` | Lambda |
| `head_to_head_lifetime.csv` | Lambda |
| `advanced_team_metrics.csv` | Lambda |
| `all_time_records.csv` | Lambda |
| `most_drafted_players.csv` | Lambda |
| `weekly_efficiency_awards.csv` | Lambda |
| `survivor_results.json` | Lambda |
| `weekly_payout_winners.json` | Lambda |

The Lambda writes each of its outputs to `/tmp`, then uses `boto3` to upload it to
`s3://<site-bucket>/<file>` with the right `Content-Type` set explicitly (`text/csv`,
`application/json` — `boto3.upload_file` does not infer this the way `aws s3 sync` does for the
static assets, so it must be passed per-file). No cache invalidation step is needed since there's no
CloudFront in front of the bucket.

`site/` itself never contains any Lambda-written file (all eight of those are gitignored at the repo
root, not under `site/`), so the `aws s3 sync site/ ...` step (decision #2) needs no excludes at all —
it can only ever touch `index.html`/`style.css`. The one extra file the deploy step must also push,
`league_config.json`, is handled by its own explicit `aws s3 cp` (shown in decision #2), precisely
because it lives outside `site/` and isn't something a `site/` sync would pick up anyway.

**This flat layout is single-league and stays exactly as-is for a single-league deploy.** Decision #12
below partitions the Lambda's outputs under `leagues/<league_id>/` once more than one league is
configured, which means `site/index.html`'s bare-filename `fetch()` calls stop resolving as soon as a
second league exists — a real front-end coupling, not just a backend implementation detail, called out
explicitly in #12 and flagged for [TODO-frontend.md](.claude/TODO-frontend.md) rather than decided here.

### 8. Front-end error handling is inconsistent, and that's a minor known gap

Only the new `league_config.json` fetch has a `.catch()` (falls back to hardcoded defaults). The
other eight `fetch()` chains in `index.html` don't — a missing or failed file just leaves that
section's table empty rather than showing an error. That's mostly pre-existing behavior, not
something this migration introduces, but it's more likely to matter once data freshness depends on an
unattended weekly Lambda run instead of a human confirming the files exist before copying them up.
Not a blocker for the initial build; worth revisiting once the pipeline is live (e.g. a small "data
last updated" indicator, or at least a visible error state instead of a silently empty table).

### 9. Trigger

EventBridge Scheduler, weekly cron during the NFL season (e.g. Tuesday morning after Monday Night
Football). The Lambda can also be invoked manually via the AWS console/CLI for on-demand refresh — no
public API Gateway endpoint is needed for a personal project.

### 10. Incremental data generation: per-year season cache + configurable per-invocation steps

**Problem.** Every one of the six report-building functions loops over the *entire* configured year
range (`year_range(config, span=...)`, currently 2013/2019–2025) on every single invocation, whether
that's the Tuesday-morning weekly run or a manual test invoke. Two things are wasteful about that:

- **Re-fetching seasons that can't have changed.** A season is immutable from the moment it ends —
  `league_history.csv`, `head_to_head_lifetime.csv`, `advanced_team_metrics.csv`, and
  `all_time_records.csv` all re-derive 2013–2024 from ESPN every run just to get 2025's numbers, which
  is the only year that could have moved.
- **One fixed cadence for data with very different freshness needs.** Right now all eight outputs are
  produced by one handler run on one schedule. But they don't need the same freshness:
  - `weekly_efficiency_awards.csv` / `survivor_results.json` / `weekly_payout_winners.json` (built by
    `_step_weekly_summary`) reflect the **current week's in-progress box scores** — these are the ones
    Morrie actually wants fresh multiple times on a game day (Thu/Sun/Mon), since payouts and survivor
    eliminations depend on that week's live scoring.
  - `league_history.csv`, `head_to_head_lifetime.csv`, `advanced_team_metrics.csv`,
    `all_time_records.csv` are season-level rollups. Standings/records only meaningfully change once a
    week's games have all finished, not mid-game — running these several times a day is pure waste on
    top of the re-fetch waste above.
  - `most_drafted_players.csv` (`_step_owner_habits`, draft-pick history) changes once a year, on draft
    day, and never again until the next draft. It shouldn't be on *any* recurring schedule at all —
    only built when someone explicitly asks for it.

**Design: two changes, addressing each half of the problem.**

_(Key patterns below are written in their final, multi-league form already — see decision #12, which
adds `league_id` as an input dimension alongside `steps`/`year` and partitions every S3 path this
decision introduces under `leagues/<league_id>/`. Written this way from the start since neither #10 nor
#11 is built yet, rather than documenting a single-league scheme here and revising it in #12.)_

#### 10a. Per-year season cache (closes the re-fetch waste)

- New `league_reports/cache.py`, S3-only (boto3 imported lazily, like `config.py`'s S3 backend — local
  scripts never touch this). Two functions: `get_cached_year(league_id, report, year)` /
  `put_cached_year(league_id, report, year, data)`, storing/reading JSON at
  `s3://<bucket>/leagues/<league_id>/cache/<report>/<year>.json` (a new prefix, not the public bucket
  root — these are intermediate computed rows, not a published output).
- A year counts as **closed** once `year < league_config["years"]["current"]`. Closed years are
  read from cache if present; on a cache miss (first time that season is ever processed) they're
  fetched from ESPN once and the result is written to cache. The **current** year is never read from
  cache — it's always fetched live, every time its step runs (that's what the cadence tiering in 10b
  controls, not this layer).
- This is a straightforward per-year split for `history.py` and `advanced_history.py`: both already
  produce independent rows per `(team, year)` inside a `for year in years:` loop, so the loop body
  becomes a `compute_<report>_year(...)` helper and the orchestrator (in `lambda_function.py`, not
  inside the report module) does cache-or-compute per year, then concatenates closed-year cache hits
  with the freshly-computed current year before building the DataFrame.
- `head_to_head.py` and `records.py` need a real refactor, not just a split, because they currently
  *reduce* across all years into one running structure inside a single loop (a nested
  win/loss/points-for dict in `head_to_head.py`; running max/min holders — `team_game_high`,
  `player_game_high_by_pos`, etc. — in `records.py`) rather than emitting one row per year. Each needs
  to compute a **per-year partial** (a year's own matchup deltas / that year's local record
  candidates), which is what gets cached per year, plus a **merge step** that folds the cached partials
  for closed years together with the fresh current-year partial into the same final aggregate the
  functions produce today. (Side note found while reading `records.py` for this: `player_season_totals`
  is accumulated across the whole loop but never read anywhere in the output — looks like dead code
  predating this refactor; worth confirming and dropping it while touching this function, not carrying
  it into the cached version.)
- `owner_habits.py` (draft picks) doesn't need this — see 10b, it's not on a recurring schedule at all,
  so the re-fetch cost only happens on the rare occasions someone asks for it. Per-year caching there is
  a nice-to-have, not required.
- `weekly_summary.py` already only ever touches the single current year — nothing to cache here in
  the re-fetch-avoidance sense, it's supposed to hit ESPN fresh every time it runs. But "only ever
  touches the current year" turns out to have a retention gap of its own once you also want past
  seasons' weekly data around after rollover — see decision #11 below, which reuses this same
  `league_reports/cache.py` module for archival, not re-fetch avoidance.
- Cache staleness is accepted as permanent once written for a closed year — there's no automatic
  invalidation. If `owner_map.json` or league config ever changes something that affects historical
  rows retroactively (e.g. renaming an owner), the fix is deleting the affected
  `leagues/<league_id>/cache/<report>/<year>.json` object(s) so the next run recomputes them, not a TTL
  or version check. Worth a one-line README/TODO note so that's not a mystery six months from now.

#### 10b. Configurable per-invocation steps (closes the one-cadence problem)

- `lambda_function.py` gains a `STEPS = {"history": ..., "head_to_head": ..., "advanced_history": ...,
  "records": ..., "owner_habits": ..., "weekly_summary": ...}` registry (one entry per existing
  `_step_*` function, same names already used in the `steps` list/labels today).
- `handler(event, context)` reads `event.get("steps")` — a list of step names — and runs only those,
  same per-step try/except and succeeded/failed reporting as today. Unknown names in the list are a
  hard failure (`ValueError`) rather than a silent skip, since the step list only ever comes from an
  operator-controlled EventBridge `Input` or a manual invoke payload, not end-user input — a typo
  should be loud.
- No `steps` key at all (an empty `{}` payload, e.g. a console "Test" invoke) falls back to
  `DEFAULT_STEPS` = every step **except** `owner_habits` — i.e. today's "run everything" behavior minus
  the one report that should never run implicitly, per the requirement above.
- This is what makes decision #9's "invoke manually via console/CLI" story still work for an
  on-demand draft-habits refresh: `aws lambda invoke --payload '{"steps":["owner_habits"]}' ...`, with
  no schedule resource for it at all.

#### Cadence tiers this unlocks (EventBridge Scheduler resources in `template.yaml`)

| Tier | Steps | Cadence | Rationale |
|---|---|---|---|
| Live | `weekly_summary` | Several times per game day — proposed default `cron(0 13,16,19,22 ? * THU,SUN,MON *)` (America/Chicago), i.e. ~1pm/4pm/7pm/10pm on Thu/Sun/Mon | Payouts/survivor depend on in-progress box scores; only step that re-fetches the current week mid-week on purpose |
| Weekly | `history`, `head_to_head`, `advanced_history`, `records`, `weekly_summary` | Once/week, existing Tuesday-morning slot (`cron(0 9 ? * TUE *)`) | Once the week's games are all final; also re-runs `weekly_summary` so the live tier's last snapshot gets one settled confirmation pass |
| On-demand | `owner_habits` | No schedule — manual invoke only | Draft picks don't change outside draft day; must never run implicitly (this task's explicit requirement) |

Two `AWS::Scheduler::Schedule` resources replace today's single `WeeklyDataRefreshSchedule`, each with
an explicit `Input` (the JSON payload above) so nothing depends on the handler's `DEFAULT_STEPS`
fallback in production — that fallback exists for manual/console invokes, not as something a schedule
should lean on. Both get a `ScheduleState` parameter (`ENABLED`/`DISABLED`, following the existing
"confirm exact day/time... in-season" placeholder note) so they can be toggled off in the offseason
without a template change, just a parameter override.

The live-tier cron's exact hours are a starting guess (covers early/late Sunday windows and
Thu/Mon night reasonably but not perfectly — e.g. it'll catch Sunday Night Football only partway
through) — tune once real game-day timing is confirmed, same spirit as the existing "confirm exact
day/time with Morrie" note on the current schedule.

#### New IAM surface

The cache prefix needs read/write access the existing policies don't grant: add
`s3:GetObject`/`s3:PutObject` on `arn:aws:s3:::<bucket>/leagues/*` to `DataGeneratorExecutionRole` (see
decision #12 — this one statement also covers the per-league config/data/archive paths #12
introduces, not just the cache, since they all live under the same `leagues/` prefix). This is the one
place a prefix wildcard is fine (unlike the flat public bucket root in decision #7) — `leagues/` is a
real subfolder with no sibling file at that prefix a wildcard could accidentally touch.

#### Rollout order

1. Step registry + event schema in `lambda_function.py` (10b) — non-invasive, works against the
   existing full-year-range report functions unchanged, and already lets `owner_habits` be pulled off
   any implicit run today.
2. Season cache for `history.py` + `advanced_history.py` (10a) — the straightforward per-year split.
3. Season cache for `head_to_head.py` + `records.py` (10a) — the aggregation-to-partial-plus-merge
   refactor; more invasive, do it once the pattern from step 2 is proven.
4. `template.yaml`: split `WeeklyDataRefreshSchedule` into the live/weekly schedules above, add the
   `cache/*` IAM statement, add `ScheduleState` parameters.
5. Testing: confirm cached-vs-live output is byte-identical to today's full-refetch output for the same
   config; confirm a closed year, once cached, produces zero ESPN calls on a second run (e.g. by
   asserting on log output / call count, not just eyeballing timing); confirm `owner_habits` never
   appears in a scheduled run's `succeeded`/`failed` lists, only a manual-invoke one.

### 11. Weekly-summary reports need backfill + retention across season rollover, not just caching — done

_(Same note as 10a/10b: key patterns below are already written in their `leagues/<league_id>/`-partitioned
form — see decision #12.)_

**Shipped**, on the `weekly-summary-backfill-retention` branch, against `weekly_summary_v2` (decision
#13's cutover replacement for `weekly_summary.py`, not the original `_step_weekly_summary` this section
was written against — the pattern below carried over unchanged, just onto the current code). `handler()`
takes an optional `event["year"]`, threaded through the `STEPS` registry to every step (ignored by all
but `weekly_summary_v2`, same "uniform signature" pattern `owner_map`/`payouts_config` already use).
Archival cache and season-stamped `archive/` copies write unconditionally, every run, current or
backfilled; the three current-facing files only get overwritten when the requested year matches
`league_config["years"]["current"]` — a backfill run can never clobber live current-season data.
One correctness fix fell out of building this, not a separate change: every builder's `current_year`
parameter now gets the *real* configured current year, not the year being processed — passing the
processed year there (what the code did pre-decision-#11, since nothing had ever called these builders
with `year != current` before) left a backfilled season's very last week perpetually uncached, the same
off-by-one gap `advanced_history_v2.py`'s docstring describes for the closedness check generally.
Validated end-to-end against the scratch bucket: a backfill run for a closed season while a later season
is configured as current writes archive/cache only, confirmed by the current-facing files' timestamps
staying untouched; a subsequent current-year run updates the current-facing files and that year's own
archive/cache, confirmed by the backfilled season's archive files' timestamps staying untouched across
it (the actual rollover-safety property this decision exists for); the backfilled season's cached output
matched an independent fully-live recompute exactly; every week of the backfilled season — including the
last one — ended up cached, confirming the `current_year` fix; re-running the same backfill afterward
cost only the two `League()` constructions, zero `box_scores` calls.

Decision #10 treats `weekly_summary.py` as "nothing to cache — it's supposed to hit ESPN fresh every
time." True for avoiding *re-fetches*, but it exposed a second gap: `weekly_efficiency_awards.csv`,
`survivor_results.json`, and `weekly_payout_winners.json` are single mutable slots (at the bucket root
today; under `leagues/<league_id>/` once #12 lands), hardcoded to `league_config["years"]["current"]`,
with **no year of their own in the data and no archive**. The moment `league_config.json`'s
`years.current` rolls from 2025 to 2026 next season, the live tier's very first run overwrites 2025's
final week-18 snapshot with 2026's in-progress week 1 — 2025's weekly efficiency/survivor/payout
history is gone unless someone manually copied the files out first. This is different from (and not
fixed by) decisions #5/#10's re-fetch cache, because there was never a copy of a *past* season's
weekly-summary output anywhere to re-fetch avoidance would preserve — each season's data only ever
existed in the one current-pointing slot.

Two gaps, one fix each, both building on decision #10's machinery instead of introducing new machinery:

- **Backfill.** `build_weekly_efficiency`/`build_survivor_results`/`build_weekly_payouts` already take
  an explicit `year` parameter — it's only `_step_weekly_summary` and the handler that hardcode it to
  `league_config["years"]["current"]`. Fix: let the event payload override it —
  `{"steps": ["weekly_summary"], "year": 2024}` — defaulting to `years["current"]` when omitted, so
  today's behavior is unchanged when nothing new is passed. This alone lets any past season's weekly
  data be (re)built on demand, e.g. `aws lambda invoke --payload '{"league_ids":["885349"],
  "steps":["weekly_summary"],"year":2024}' ...` — the same manual-invoke pattern decision #10b already
  establishes for `owner_habits`.
- **Go-forward retention.** Reuse `league_reports/cache.py` from 10a, but for archival rather than re-fetch
  avoidance: every time `_step_weekly_summary` computes a year's efficiency/survivor/payouts data
  (current or backfilled), it also writes that year's result to
  `leagues/<league_id>/cache/weekly_efficiency/<year>.json`,
  `leagues/<league_id>/cache/survivor_results/<year>.json`, and
  `leagues/<league_id>/cache/weekly_payouts/<year>.json` — cheap, since it's already computed and this
  is the same put-JSON-to-S3 primitive 10a needs anyway. Separately, publish a season-stamped copy
  alongside the current-facing files instead of only those:
  `leagues/<league_id>/archive/weekly_efficiency_awards_<year>.csv`,
  `leagues/<league_id>/archive/survivor_results_<year>.json`,
  `leagues/<league_id>/archive/weekly_payout_winners_<year>.json` (new `archive/` subprefix under that
  league, public-read like the rest of the bucket per decision #7's bucket policy — this is the same
  content that's already public, just keyed by season instead of overwritten). The three existing
  current-facing files keep their exact contract (current season only, same filenames, just relocated
  under the league prefix by #12) — the archive copies are purely additive, so 2025's final snapshot
  survives the 2026 rollover automatically instead of needing a manual "grab it before it's clobbered"
  step.
- Whether/when the front end ever gets a "browse past seasons' weekly awards" view against those
  `archive/` files is a `site/`/frontend decision, not made here — .claude/TODO-frontend.md's problem if wanted,
  not required for this backend change to be complete.
- New IAM: covered by decision #12's single `leagues/*` grant — no separate `archive/`-specific
  statement needed since it's nested under the same per-league prefix.
- Rollout: do this alongside decision #10's step 1 (step registry) rather than as a separate pass —
  the `year` override is a small addition to the same `_step_weekly_summary` wiring, and the
  archive-write is a small addition to the same `_upload_csv`/`_upload_json` calls that step already
  makes.

### 12. Multi-league support: per-invocation league selection + S3 output partitioning

**Problem.** Everything built and planned so far (decisions #4, #7, #10, #11) assumes exactly one
league: one `league_config.json`, one `owner_map.json`, one `weekly_payouts_config.json`, one flat set
of output keys at the bucket root. Running this for a second ESPN league today would mean a second,
entirely separate stack (own bucket, own Lambda, own everything) — no sharing, and no way to add a
league without a full redeploy of a parallel copy of the infrastructure. The requirement: one Lambda,
driven by what's in its invocation event, should be able to generate data for whichever league(s) that
event names, with outputs kept fully separate per league.

**Design.**

#### 12a. League selection lives entirely in the event, no manifest, no auto-discovery

- The event gains `league_ids`: a list, e.g. `{"league_ids": ["885349"], "steps": [...]}` (or
  `["885349", "223344"]` to process more than one league in a single invocation, looping the same way
  decision #10b already loops over `steps`).
- `league_ids` is **required** — no default/fallback the way `DEFAULT_STEPS` covers an omitted `steps`
  key. Rationale: `steps` has a small, fixed, hardcoded set of valid values, so "assume everything
  except the one explicitly-opt-in report" is a safe, bounded default; the set of leagues is
  open-ended and grows over time, so there's no safe hardcoded default for it, and silently running
  "every league we've ever configured" on a schedule meant for one league is exactly the kind of
  surprise decision #10b's "no `steps` key → loud failure for typos" reasoning already argues against.
  An omitted or empty `league_ids` is a hard `ValueError`, same failure mode as an unknown step name.
- Explicitly **not** doing: a `leagues.json` manifest file + "no `league_ids` means run every league
  in the manifest" convenience default. Considered it (it would let a schedule stay league-agnostic and
  pick up new leagues automatically), but it re-introduces exactly the implicit-blast-radius problem
  the point above rules out, and it's not needed for the stated requirement ("driven by values passed
  to lambda function" already implies the event is the source of truth). If a "run every league we
  know about" convenience is wanted later, add it as an opt-in event value
  (`{"league_ids": "ALL"}` reading a manifest only when that literal is passed), not as the default
  behavior of omitting the field.
- Consequence for `template.yaml`: adding a league still means a `template.yaml`/`sam deploy` change —
  each `AWS::Scheduler::Schedule`'s `Input` has to list the league_ids it should cover — but adding a
  league needs **zero** Lambda code change, and (per 12c) zero IAM change. Only that league's config
  files need uploading to S3 and its league_id needs adding to the relevant schedules' `Input`.

#### 12b. Per-league config loading, S3 output partitioning

- New top-level bucket prefix, `leagues/<league_id>/`, replacing today's bucket-root/`config/` layout
  for everything league-specific:

  ```
  s3://<bucket>/
    index.html, style.css                              (frontend-owned, unpartitioned — out of scope)
    leagues/<league_id>/league_config.json              (was bucket-root league_config.json, decision #4)
    leagues/<league_id>/config/owner_map.json            (was config/owner_map.json)
    leagues/<league_id>/config/weekly_payouts_config.json
    leagues/<league_id>/league_history.csv               (was bucket-root, decision #7's 8 files)
    leagues/<league_id>/head_to_head_lifetime.csv
    leagues/<league_id>/advanced_team_metrics.csv
    leagues/<league_id>/all_time_records.csv
    leagues/<league_id>/most_drafted_players.csv
    leagues/<league_id>/weekly_efficiency_awards.csv
    leagues/<league_id>/survivor_results.json
    leagues/<league_id>/weekly_payout_winners.json
    leagues/<league_id>/cache/<report>/<year>.json       (decision #10a)
    leagues/<league_id>/archive/<report>_<year>.<ext>    (decision #11)
  ```

  `leagues/` (not a bare `<league_id>/` straight off the bucket root) is deliberate: it gives the IAM
  policy in 12c one unambiguous prefix to grant against, with no risk of a numeric league_id ever
  colliding with `index.html`/`style.css`/a future top-level frontend asset — decision #7's "flat
  bucket root can't tell files apart" problem, solved by construction instead of by an explicit key
  list this time, since an explicit list isn't possible without a template change per league (defeats
  12a's goal).
- `league_reports/config.py`'s S3 backend functions all gain a `league_id` parameter and build their key off
  it: `LEAGUE_CONFIG_KEY` becomes `f"leagues/{league_id}/league_config.json"`, `CONFIG_PREFIX` becomes
  `f"leagues/{league_id}/config/"`. `load_all_config()` takes `league_id` and returns that league's
  four inputs, same shape as today.
- `lambda_function.py`'s `handler()` becomes a nested loop: `for league_id in event["league_ids"]:
  load that league's config, then for label, step in requested_steps: run it`, uploading to
  `leagues/{league_id}/<filename>` instead of bucket-root `<filename>`. Failure isolation extends to
  match: one league's step failing doesn't abort another league's run, same try/except-per-step as
  today, just keyed `f"{league_id}:{label}"` in the `succeeded`/`failed` lists so a multi-league
  invocation's result is still legible.
- Local dev (`scripts/*.py`, the local-file config backend) stays single-league and unpartitioned —
  this whole prefix scheme is Lambda/S3-only. Multi-league local testing isn't a stated requirement;
  if it's ever wanted, the local backend could take an optional `league_id` that maps to a
  `leagues/<league_id>/` subdirectory under the project root, mirroring the S3 layout, but that's
  deferred, not designed here.

#### 12c. ESPN credentials: one shared Secrets Manager secret across all leagues

Decided: a single shared secret (today's `EspnCredsSecret`), used for every league, not one secret per
league_id. This assumes the same ESPN login is a member of every league this Lambda is asked to pull —
true for Morrie's actual setup. The alternative (a secret per league) was considered and rejected for
now because it reintroduces exactly the "must touch `template.yaml`/IAM to add a league" cost 12a and
12b both specifically avoid, for a generality that isn't needed today.

To not paint into a corner if that assumption ever breaks: `get_credentials_from_secrets_manager()`
keeps accepting an explicit `secret_name` override (it already does, for tests), and a future league
whose owner uses a different ESPN login can be supported by passing a per-league secret name through
`league_config.json` (e.g. an optional `"espn_secret_name"` key, defaulting to the shared secret when
absent) without changing the shared-secret path for every other league. Not building this now — noted
so it's a small addition later, not a redesign.

#### New IAM surface

One `DataGeneratorExecutionRole` statement covers all of 12b's per-league paths at once:
`s3:GetObject`/`s3:PutObject` on `arn:aws:s3:::${SiteBucketName}/leagues/*` (folds in decision #10a's
cache grant and #11's archive grant — both already live under this same prefix, see the notes on those
two decisions above; this replaces what would otherwise be three separate statements with one). The
explicit-8-keys `s3:PutObject` list from decision #7 and the `config/owner_map.json`/
`weekly_payouts_config.json`/`league_config.json` list from decision #4 are retired once this lands —
superseded by this single prefix grant, not kept alongside it.

#### Frontend coupling — flagged, not designed here

`site/index.html`'s bare-filename `fetch("league_history.csv")` calls (decision #7) stop working the
moment outputs move under `leagues/<league_id>/` — this is a real breaking change for the front end,
not just an internal backend refactor, since the published key names change. What the front end should
do about it (a league picker page, a build-time/query-string league selector, one `site/` deploy per
league under a per-league prefix, etc.) is a `site/`/.claude/TODO-frontend.md decision, deliberately not made
here. Flagged in .claude/TODO-frontend.md so it isn't lost, but this backend change should not be considered
blocked on a frontend answer — the S3 layout and Lambda behavior above are complete and correct on
their own; only `site/index.html`'s fetch paths need to catch up separately.

#### Rollout order

1. `league_reports/config.py`: add `league_id` to every S3-backend function and `load_all_config()`.
2. `lambda_function.py`: `league_ids` in the event, the nested league × step loop, per-league upload
   paths, per-league keyed `succeeded`/`failed` reporting.
3. Land alongside (not after) decision #10/#11's work, since `league_reports/cache.py` and the archive-write
   paths should be built with `league_id` in their signature from the start rather than retrofitted —
   see the note at the top of decision #10.
4. `template.yaml`: collapse the decision #4/#7 IAM statements into the single `leagues/*` grant;
   update the live/weekly `AWS::Scheduler::Schedule` `Input` values to include `league_ids` for
   whichever league(s) each schedule should cover.
5. Migrate today's single league's existing bucket-root objects to `leagues/<league_id>/` (one-time
   `aws s3 mv`/`cp` pass) before switching the Lambda over, so there's no gap where neither location has
   current data.
6. Testing: a two-league test invoke (`{"league_ids": ["<real>", "<scratch>"]}`) produces correctly
   separated output trees with no cross-league bleed; an empty/missing `league_ids` fails loudly;
   confirm the retired decision #4/#7 IAM statements are actually gone, not just unused, once the
   `leagues/*` grant is in place.

### 13. Incremental current-year refresh: shared fetch + raw box-score cache, not per-report caches

Refines decision #10's plan for the *current* year (closed-year per-year caching above is unchanged).
Reading `espn_api`'s source turned up that most of the current year's redundant ESPN cost isn't a
per-week re-walk needing a partial-plus-merge cache per report — it's the same already-fetched data
being independently re-fetched by every report function, including data (`head_to_head`'s per-week
results) that was never genuinely per-week server-side to begin with. Full design, S3 layout, and a
shadow-mode rollout plan (new code runs alongside the production pipeline, publishing to a separate
`shadow/` prefix, diffed against production before any cutover) live in their own document:
[DESIGN-incremental-espn-pipeline.md](DESIGN-incremental-espn-pipeline.md).

### 14. Weekly payout rules can vary by year

`config/weekly_payouts_config.json` was a single flat `{"weekly_payouts": {"<week>": {rule}}}` map,
used for every season identically — no way to express "week 6's rule was different in 2023 than it is
now" without hand-editing the file before and after computing each affected season, a manual step
nobody would remember to do correctly across backfills (decision #11 made backfilling an actual
on-demand operation, not just a hypothetical, which is what made this gap worth closing now rather
than staying theoretical).

Added, additively — `weekly_payouts_by_year`, a sibling key holding whole-season overrides keyed by
year: `{"weekly_payouts": {...default...}, "weekly_payouts_by_year": {"2023": {...complete override
for 2023...}}}`. `weekly_summary_v2.py`'s `resolve_payout_rules(payout_config, year)` looks up
`weekly_payouts_by_year[str(year)]` first, falling back to `weekly_payouts` (the default) when that
year has no entry — so an unedited config (or a year nobody's added an override for) behaves exactly
as before. Deliberately **not** a per-week merge against the default: a year's override, once present,
is a complete week→rule map on its own, so "what rules applied in year X" is answerable by reading one
block, not reconciling two.

`v1`'s `weekly_summary.py`/`build_weekly_payouts` was **not** touched — it still reads
`payout_config["weekly_payouts"]` directly, which continues to resolve exactly as it always has since
the new key is additive, not a schema migration. Only `weekly_summary_v2.py` (the step actually running
in production since decision #13's cutover) gained year-awareness.

New Makefile target, `sync-payouts-config` — this file was never previously scripted; pushing an edited
copy to the deployed Lambda's config was always an ad hoc `aws s3 cp`. Not front-end-facing (the browser
never fetches it, only the Lambda reads it from the `config/` prefix), so it's a separate target, not
folded into `publish-site`.

Validated: `resolve_payout_rules()` unit-checked directly (no override falls back to default; an
override for one year doesn't leak into another; the old config shape with no `weekly_payouts_by_year`
key at all still resolves) and end-to-end — a deliberately different rule type for one week of a real
closed season produced a genuinely different payout winner than the default rule would have, confirmed
through both the no-cache and cached (Lambda-facing) builders against the scratch bucket.

## Repo layout (this repo)

```
espn-ff-s3-site/
  .claude/DESIGN.md       # this file
  template.yaml           # SAM template: bucket, Lambda, layer, role, schedule(s)
  site/                   # index.html, style.css — synced to the bucket root; Lambda output (*.csv/*.json) is not part of this folder
  league_reports/                # shared report-building code (see above)
    cache.py              # per-league, per-year S3 cache: re-fetch avoidance (#10a) + weekly-summary archival (#11) — Lambda-only, like config.py's S3 backend
  lambda_function.py      # Lambda entrypoint; nested league_ids x steps loop, event["year"] override (decisions #10b/#11/#12)
  scripts/                # local CLI wrappers
  requirements.txt         # what `sam build` packages into the Lambda — espn_api only, no pandas (decision #3)
  requirements-dev.txt     # local dev only — requirements.txt + pandas, not read by `sam build`
  Makefile                 # build-DataGeneratorFunction (SAM's custom build hook, decision #3) +
                            # sam build/deploy + s3 sync convenience targets (.claude/TODO-frontend.md)
```

## Rollout phases

1. **Prep**: pin `requirements.txt` (`espn_api`, `pandas`); port each script's compute logic into
   `league_reports/reports/*` as importable functions (behavior-preserving — verify local CSV output is
   byte-identical to today's).
2. **Static hosting only**: SAM-deploy just the S3 bucket, manually `aws s3 sync` today's
   `index.html`/`style.css`/CSVs, confirm the site renders identically to local. Validates hosting
   independent of the Lambda.
3. **Lambda MVP**: build `lambda_function.py` on the refactored report functions + the AWS Pandas
   layer; test by invoking directly against a scratch S3 bucket/prefix, not the live site bucket.
4. **Secrets/config wiring**: move ESPN cookies to Secrets Manager; put league/owner/payout config in
   the S3 config prefix (or pass via the EventBridge input payload).
5. **Automate**: EventBridge weekly schedule → Lambda → bucket root of the site bucket. Run
   end-to-end once, verify the live site picks up fresh data.
6. **Season-cache optimization + configurable steps**: add the prior-season-cache/merge logic and the
   per-invocation step registry + tiered EventBridge schedules (decisions #5/#10 above) once the basic
   pipeline is proven, so weekly runs stay fast as more seasons accumulate and each report refreshes on
   a cadence that matches how often its data actually changes. Same pass adds decision #11's
   weekly-summary backfill (`year` override) and per-season archive copies, so rolling over to a new
   season stops silently deleting the previous one's weekly data, and decision #12's multi-league
   partitioning (`league_ids` in the event, `leagues/<league_id>/` in S3) — the three are built together
   since #10a/#11's S3 key patterns are already written in their `league_id`-partitioned form.

## Open items / deferred

- Custom domain + HTTPS (would mean adding CloudFront + ACM + Route 53) — explicitly out of scope for
  now, revisit if wanted later.
- CI (GitHub Actions) to run `aws s3 sync` on push to `site/` — nice-to-have, not required for the
  first working version.
- ~~Multi-league reuse of this stack~~ — no longer deferred, now designed in decision #12 above (event-driven
  `league_ids` + `leagues/<league_id>/` S3 partitioning).
