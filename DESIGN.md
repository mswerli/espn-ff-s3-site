# Who Dat League — AWS Hosting Design

Status: draft, not yet built
Companion repo: [who_dat_history](https://github.com/) — front-end (`index.html`/`style.css`) and the
original local ESPN-fetch scripts (`scripts/*.py`, `helpers/utilities.py`) this design replaces the
data-generation half of.

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

### 2. AWS SAM for all infrastructure, including the bucket

SAM templates are CloudFormation underneath, so `template.yaml` declares everything in one stack:
the S3 bucket (with `WebsiteConfiguration` and a public-read bucket policy), the Lambda function, its
execution role, the Lambda layer, and the EventBridge schedule. One `sam deploy` stands up or updates
the whole stack.

SAM does **not** upload arbitrary files (`index.html`, `style.css`) into the bucket — `sam deploy`
only pushes infrastructure and Lambda code. Static asset publishing is a separate, simple step:

```
sam build && sam deploy                                              # infra + Lambda code
aws s3 sync site/ s3://$BUCKET/ --exclude "*.csv" --exclude "*.json"  # static assets (rare changes)
# *.csv and *.json are never synced from git — only the Lambda writes those, to the bucket root
```

### 3. Lambda packaging: AWS-managed Pandas layer, zip-based, no container image

pandas/numpy are usually the reason people reach for container-image Lambdas, but AWS publishes a
public layer (`AWSSDKPandas-Python31x`) with pandas + numpy prebuilt. `espn_api` is pure
Python/`requests` and small. So: a normal zip-based Lambda plus that one managed layer — no ECR, no
Docker build step, faster iteration.

### 4. Config vs. secrets are split

- `league_config.json`, `config/weekly_payouts_config.json`, `owner_map.json`: not sensitive, and the
  original repo already treats `league_config.json` as the thing that makes it reusable for other
  ESPN leagues (see the docstring on `get_league_config` in `helpers/utilities.py`). These live in an
  S3 config prefix (or get passed directly in the EventBridge invocation payload) so the Lambda
  genuinely *takes a configuration* as input rather than having one baked into the deployment package.
- ESPN `swid`/`espn_s2` cookies: real session credentials. These go in **Secrets Manager**, never
  alongside public site data in S3.

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

`site/index.html` ([copied unmodified from `who_dat_history`](site/index.html)) fetches its data with
bare relative filenames — `fetch("league_history.csv")`, `fetch("head_to_head_lifetime.csv")`, etc. —
so it expects those files in the **same directory** as `index.html`, not under a subfolder. An earlier
draft of this design had the Lambda writing to a `data/` prefix; that was a front-end/infra mismatch
(every table would have 404'd) and has been corrected here: the Lambda writes flat to the bucket root,
matching exactly what the existing local scripts already do via `output_path()`. This also means the
local CSV output path and the S3 upload path are identical, so there's one thing less to keep in sync.

The Lambda must upload each file under the **exact same name** the front-end already fetches
(case-sensitive, S3 keys aren't forgiving like a case-insensitive local filesystem):

| File | Front-end reference |
|---|---|
| `league_history.csv` | [index.html](site/index.html) |
| `head_to_head_lifetime.csv` | [index.html](site/index.html) |
| `advanced_team_metrics.csv` | [index.html](site/index.html) |
| `all_time_records.csv` | [index.html](site/index.html) |
| `most_drafted_players.csv` | [index.html](site/index.html) |
| `weekly_efficiency_awards.csv` | [index.html](site/index.html) |
| `survivor_results.json` | [index.html](site/index.html) |
| `weekly_payout_winners.json` | [index.html](site/index.html) |

The Lambda writes each output to `/tmp`, then uses `boto3` to upload it to `s3://<site-bucket>/<file>`
with the right `Content-Type` set explicitly (`text/csv`, `application/json` — `boto3.upload_file`
does not infer this the way `aws s3 sync` does for the static assets, so it must be passed
per-file). No cache invalidation step is needed since there's no CloudFront in front of the bucket.

The `aws s3 sync site/ ...` step for static assets (decision #2) must exclude `*.csv`/`*.json` so it
never clobbers the Lambda's output — already reflected in that command.

### 8. Front-end error handling is unchanged, and that's a minor known gap

None of `index.html`'s eight `fetch()` chains has a `.catch()` — a missing or failed file just leaves
that section's table empty rather than showing an error. That's pre-existing behavior, not something
this migration introduces, but it's more likely to matter once data freshness depends on an unattended
weekly Lambda run instead of a human confirming the files exist before copying them up. Not a blocker
for the initial build; worth revisiting once the pipeline is live (e.g. a small "data last updated"
indicator, or at least a visible error state instead of a silently empty table).

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
  requirements.txt         # pinned espn_api, pandas
  Makefile                 # sam build/deploy + s3 sync targets
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
