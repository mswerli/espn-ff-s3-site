# Who Dat League — AWS Hosting Design

Status: Phase 1 (Prep, `who_dat/`) and Phase 2 (static hosting) are done — the S3 site bucket
(`who-dat-league-217412666418`, us-west-2, stack `who-dat-infra`) is live and seeded, see
TODO-frontend.md. Phases 3+ (Lambda automation, season-cache, multi-league) status lives in
TODO-backend.md.

Sibling repo: `who_dat_history` — the original front-end (`index.html`/`style.css`) and local
ESPN-fetch scripts (`scripts/*.py`, `helpers/utilities.py`) this design replaces the data-generation
half of. It remains the **live GitHub Pages source today** and is intentionally unmodified. The copy
of the front-end under this repo's `site/` (see decision #7) has already diverged from it — it picked
up a config-driven title/subtitle read from `league_config.json` — and going forward `site/` here is
the one that evolves; `who_dat_history` isn't a live upstream to sync from anymore.

## Goal

Move the site from "run scripts locally, copy CSVs next to `index.html`" to:

- a plain S3 bucket serving the static site (no CloudFront, no custom domain — S3 website endpoint,
  HTTP is acceptable for this project)
- an AWS Lambda that regenerates the CSV/JSON data files on a weekly schedule, taking a
  league configuration as input instead of hardcoded local paths
- AWS SAM as the one deployment tool for all of the infrastructure (bucket, Lambda, schedule, role)

## Current state (who_dat_history repo, as of this design)

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
target that copies `lambda_function.py` + `who_dat/` and `pip install`s `requirements.txt` into
`$ARTIFACTS_DIR` explicitly — an **allow-list**, not a deny-list. Nothing else in the repo (`ignore/`,
`site/`, `scripts/`, `config/`, `league_config.json`, docs, `.git/`, `.venv/`, ...) is ever a build
input candidate, regardless of what gets added to the repo later — the opposite failure mode of a
deny-list, which only protects against files someone remembered to add to it. Verified: `sam build`
with this in place produces an artifact directory containing only `lambda_function.py`, `who_dat/`,
and `requirements.txt`'s installed dependencies — confirmed by grepping the entire build output for
fragments of the real ESPN cookies and finding none, and by `find`ing for `ignore/`, `site/`,
`scripts/`, `.git`, `.venv`, `.claude` anywhere under `.aws-sam/build/` and finding nothing.

### 4. Config vs. secrets are split — and `league_config.json` specifically is public

- `league_config.json` is no longer Lambda-input-only: `site/index.html` now fetches it directly
  (`fetch("league_config.json")`, with a `.catch()` that falls back to hardcoded defaults) to render
  the site title/subtitle, so the page itself is config-driven rather than hardcoded per league. That
  means `league_config.json` must be publicly readable at the site bucket root, same as the CSVs.
  Its canonical location stays where `who_dat/config.py`'s `LEAGUE_CONFIG_PATH` already puts it — the
  **project root**, alongside `who_dat/`, not inside `site/` — since that's already built, tested, and
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
  The frontend bucket policy makes the whole bucket public-read anyway (see TODO-frontend.md), so
  "private prefix" here is organizational, not an actual access boundary — consistent with these two
  files already being "not sensitive" above.
- `who_dat/config.py` implements this as an S3+Secrets-Manager backend selected by one env var
  (`WHO_DAT_CONFIG_BACKEND=s3`, set on the Lambda in `template.yaml`), alongside — not replacing — the
  local-file backend local scripts already use (`WHO_DAT_CONFIG_BACKEND` defaults to `"local"`).

### 5. Avoid re-fetching the entire league history every run

The original scripts re-fetch *every* configured year from ESPN on every run. Once a season ends its
stats don't change, so the Lambda should cache each prior season's computed output in S3 and, on each
weekly run, only re-fetch/recompute the **current** season and merge it with the cached history. This
keeps each invocation well inside Lambda's 15-minute limit and avoids hammering ESPN's undocumented
API. If league history ever grows enough that this isn't sufficient, the natural next step is a Step
Functions state machine with one Lambda per report type run in parallel — not needed at today's data
volume.

### 6. Shared code between the Lambda and local dev scripts

Split each script's "fetch from ESPN → compute → write CSV to a hardcoded local path" into
importable compute functions, so both the Lambda handler and a local CLI call the same logic:

```
who_dat/
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

(`site/index.html` isn't a byte-for-byte copy of the original `who_dat_history/index.html` — it also
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

## Repo layout (this repo)

```
who-dat-infra/
  DESIGN.md              # this file
  template.yaml           # SAM template: bucket, Lambda, layer, role, schedule
  site/                   # index.html, style.css — synced to the bucket root; Lambda output (*.csv/*.json) is not part of this folder
  who_dat/                # shared report-building code (see above)
  lambda_function.py      # Lambda entrypoint
  scripts/                # local CLI wrappers
  requirements.txt         # what `sam build` packages into the Lambda — espn_api only, no pandas (decision #3)
  requirements-dev.txt     # local dev only — requirements.txt + pandas, not read by `sam build`
  Makefile                 # build-DataGeneratorFunction (SAM's custom build hook, decision #3) +
                            # sam build/deploy + s3 sync convenience targets (TODO-frontend.md)
```

## Rollout phases

1. **Prep**: pin `requirements.txt` (`espn_api`, `pandas`); port each script's compute logic into
   `who_dat/reports/*` as importable functions (behavior-preserving — verify local CSV output is
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
6. **Season-cache optimization**: add the prior-season-cache/merge logic (decision #5 above) once the
   basic pipeline is proven, so weekly runs stay fast as more seasons accumulate.

## Open items / deferred

- Custom domain + HTTPS (would mean adding CloudFront + ACM + Route 53) — explicitly out of scope for
  now, revisit if wanted later.
- CI (GitHub Actions) to run `aws s3 sync` on push to `site/` — nice-to-have, not required for the
  first working version.
- Multi-league reuse of this stack (parameterizing the SAM template per league) — the original repo's
  `league_config.json` already points this direction but it's not a near-term goal.
