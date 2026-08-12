# Backend TODO — Lambda Data Generation

Execution checklist for the Lambda that regenerates the site's CSV/JSON data. Rationale lives in
[DESIGN.md](DESIGN.md); this is just the ordered work. Companion: [TODO-frontend.md](TODO-frontend.md).

## Done

- [x] Phase 1 (Prep): ported `scripts/*.py` into importable `who_dat/reports/*.py` `build_*()`
      functions, plus `who_dat/espn_client.py` (retry/backoff) and `who_dat/config.py`
      (local-file config/creds loading) — verified byte-identical output vs. the original scripts
      (commit `8368d2f`)
- [x] `requirements.txt` pinned (`espn-api==0.45.1` — **don't loosen `espn-api` without retesting
      bye-week handling in `advanced_history.py`**, see project memory). `pandas==2.3.1` lives in
      `requirements-dev.txt` instead, not `requirements.txt`: `sam build` packages exactly
      `requirements.txt` into the Lambda, and pandas belongs to the `AWSSDKPandas-Python311` layer
      there (DESIGN.md decision #3) — bundling it too broke `sam build` outright (pandas' unpinned
      `numpy` floor resolved to a version with no linux/cp311 wheel yet) and would have shadowed the
      layer's pandas even on days it didn't. `requirements-dev.txt` (`-r requirements.txt` + pandas)
      is what local dev/`scripts/*.py` install instead.
- [x] `sam build` packaging fixed to not ship `ignore/` (real ESPN creds) or anything else
      non-runtime into the Lambda deployment package: `CodeUri: ./` means `sam build`'s CopySource
      step covers the whole repo root by default, and a `.samignore` file does **not** work around
      that (tried it, verified via an actual `sam build` that it has no effect on this SAM CLI
      version's Python build workflow — see DESIGN.md decision #3). Fixed with
      `Metadata: BuildMethod: makefile` on `DataGeneratorFunction` + a `build-DataGeneratorFunction`
      target in the repo-root `Makefile` that explicitly allow-lists what gets copied
      (`lambda_function.py`, `who_dat/`, `requirements.txt`'s pip deps) instead of trying to deny-list
      everything that must NOT ship. Verified: `sam build` succeeds and the resulting
      `.aws-sam/build/DataGeneratorFunction/` contains none of `ignore/`, `site/`, `scripts/`,
      `config/`, `league_config.json`, docs, `.git/`, `.venv/`, `.claude/` — checked both by directory
      listing and by grepping the whole build output for fragments of the real ESPN cookies.
- [x] `scripts/run_all.py` + thin CLI wrappers for local runs

## Lambda handler

- [x] `lambda_function.py`: single handler that
  - [x] loads `league_config.json`, `owner_map.json`, `weekly_payouts_config.json` (source: see
        the config-input item below — this is separate from the *public* copy of
        `league_config.json` the front-end reads, which is [TODO-frontend.md](TODO-frontend.md)'s job)
  - [x] loads ESPN `swid`/`espn_s2` from Secrets Manager
  - [x] calls each `who_dat/reports` `build_*()` in sequence; one failure shouldn't abort the rest
        (mirror `scripts/run_all.py`'s per-step try/except)
  - [x] writes each output to `/tmp`
  - [x] uploads each output to `s3://<site-bucket>/<file>` at the bucket root, with explicit
        `Content-Type` (`text/csv` / `application/json` — `boto3` doesn't infer this)
  - [x] logs progress per report/year/week (the existing `print()` statements are enough — Lambda
        captures stdout to CloudWatch automatically)
- [x] Extend `who_dat/config.py` with an S3 + Secrets Manager backend *alongside* the existing
      local-file one (env-var-selected, per DESIGN.md decision #6) — don't replace the local-file
      path, local scripts still need it
- [x] Decide + implement where the Lambda's own config input comes from: an S3 config prefix vs.
      the EventBridge invocation payload (DESIGN.md decision #4) — decided: S3 config prefix, see
      DESIGN.md decision #4's "Decided (backend build)" note

## Infrastructure (`template.yaml` — Lambda-side resources; bucket resources are in TODO-frontend.md)

- [x] Lambda function resource: Python runtime, `AWSSDKPandas-Python31x` layer ARN, generous
      timeout (start at the 900s max, tune down once real runtime is known), memory sized for pandas
- [x] IAM execution role:
  - [x] `s3:PutObject` scoped to the **specific 8 data-file keys only**, not a bucket-wide wildcard
        — the bucket root is a flat namespace shared with `index.html`/`style.css`/`league_config.json`,
        so prefix-based scoping can't tell them apart; list the 8 keys explicitly in the policy's
        `Resource` array
  - [x] `s3:GetObject` on the config prefix (if that's the path chosen above)
  - [x] `secretsmanager:GetSecretValue` on the ESPN creds secret
  - [x] `logs:*` for its own log group
- [x] Secrets Manager secret for `swid`/`espn_s2` — create out of band (not templated with real
      values in git); either a manual `aws secretsmanager create-secret` or a SAM parameter with
      `NoEcho` filled in at deploy time — went with the `NoEcho` SAM parameter option
- [x] EventBridge Scheduler: weekly cron, in-season (confirm exact day/time with Morrie — proposed
      Tuesday morning after Monday Night Football)

## Testing

- [ ] Invoke locally (`sam local invoke` or a direct Python call) against a **scratch S3
      prefix/bucket**, not the live site bucket
- [ ] Measure total runtime for a full run (all 6 reports × full configured year range) — check
      against the 900s Lambda cap before relying on it in prod
- [ ] Confirm output is byte-identical (or intentionally different — e.g. sort order) to what the
      local scripts produce for the same config
- [ ] Only point it at the real site bucket after a scratch-prefix run has been eyeballed

## Incremental data generation (DESIGN.md decision #10)

Promoted out of "Deferred / later" — this is the current work item. Full design/rationale in
DESIGN.md decision #10; this is just the ordered checklist.

- [ ] `who_dat/cache.py`: S3-only per-year cache (`get_cached_year(report, year)` /
      `put_cached_year(report, year, data)`) at `s3://<bucket>/cache/<report>/<year>.json`, boto3
      imported lazily like `config.py`'s S3 backend
- [ ] `lambda_function.py`: `STEPS` registry (one entry per existing `_step_*`) +
      `event.get("steps")` selection in `handler()`, unknown names raise `ValueError`, no `steps` key
      falls back to `DEFAULT_STEPS` (everything except `owner_habits`)
- [ ] `who_dat/reports/history.py` + `advanced_history.py`: split the per-year loop body into a
      `compute_*_year(...)` helper; orchestrator (in `lambda_function.py`) does cache-or-compute per
      year (closed years = `year < league_config["years"]["current"]`) and concatenates before
      building the DataFrame
- [ ] `who_dat/reports/head_to_head.py` + `records.py`: refactor from "reduce across all years in one
      pass" to "per-year partial + merge step" so each year's partial is independently cacheable —
      more invasive than the above two, do it second; drop `records.py`'s apparently-dead
      `player_season_totals` accumulator while in there (confirm it's truly unread first)
- [ ] `owner_habits.py`: no cache needed — it's moving to on-demand-only (see steps registry above),
      so the re-fetch cost only hits the rare manual invoke
- [ ] `template.yaml`: replace `WeeklyDataRefreshSchedule` with two `AWS::Scheduler::Schedule`
      resources (live tier: `weekly_summary` only, several times/game day; weekly tier: everything
      except `owner_habits`, once/week) per DESIGN.md decision #10's cadence table, each with an
      explicit `Input`; add a `ScheduleState` parameter to each for offseason toggling; no schedule
      resource for `owner_habits` at all
- [ ] `template.yaml`: add `s3:GetObject`/`s3:PutObject` on `arn:aws:s3:::${SiteBucketName}/cache/*`
      to `DataGeneratorExecutionRole`
- [ ] Testing: cached-vs-live output byte-identical to today's full-refetch output for the same
      config; a closed year produces zero ESPN calls on a second run once cached; `owner_habits`
      never appears in a scheduled run, only a manual-invoke one; confirm live-tier cron hours with
      Morrie against actual game-day timing (starting guess in DESIGN.md, not confirmed)

## Weekly-summary backfill + retention (DESIGN.md decision #11)

Same pass as the step registry item above (`_step_weekly_summary` is the thing being touched either
way) — do this alongside, not after. Full rationale in DESIGN.md decision #11. Key patterns below are
already written in their multi-league form (decision #12) since #10/#11/#12 are being built together —
see that section below.

- [ ] `_step_weekly_summary` (and `handler()`'s event handling): accept an optional `year` in the
      event payload (`{"league_ids": [...], "steps": ["weekly_summary"], "year": 2024}`), defaulting to
      `league_config["years"]["current"]` when omitted — `build_weekly_efficiency`/
      `build_survivor_results`/`build_weekly_payouts` already take `year` as a parameter, so this is
      wiring, not a report-layer change
- [ ] `who_dat/cache.py` (same module as decision #10a): after computing a year's weekly-summary data,
      write it to `leagues/<league_id>/cache/weekly_efficiency/<year>.json`,
      `leagues/<league_id>/cache/survivor_results/<year>.json`,
      `leagues/<league_id>/cache/weekly_payouts/<year>.json` — archival, not re-fetch avoidance, so
      this always writes (current or backfilled year), unlike 10a's closed-years-only cache reads
- [ ] Publish season-stamped archive copies alongside the existing current-season files —
      `leagues/<league_id>/archive/weekly_efficiency_awards_<year>.csv`,
      `leagues/<league_id>/archive/survivor_results_<year>.json`,
      `leagues/<league_id>/archive/weekly_payout_winners_<year>.json` — so a season's final weekly data
      survives the next season's rollover instead of being silently overwritten. The three current-facing
      files keep their exact contract; this is additive only
- [ ] Testing: backfilling a past year via `{"league_ids":[...],"steps":["weekly_summary"],
      "year":2024}` produces the same output as the original 2024 run would have; confirm a season
      rollover (bump `league_config.json`'s `years.current`) leaves the previous year's `archive/*`
      files untouched while the current-facing files start reflecting the new year
- [ ] Frontend follow-up (not this checklist's job): whether/how `site/` ever surfaces the `archive/`
      files as a past-seasons browser — flag for [TODO-frontend.md](TODO-frontend.md) if wanted, not
      required for this item to be done

## Multi-league support (DESIGN.md decision #12)

Build alongside the two sections above, not after — `who_dat/cache.py` and `_step_weekly_summary`'s
archive-write paths should take `league_id` from the start rather than being retrofitted. Full
rationale in DESIGN.md decision #12.

- [ ] `who_dat/config.py`: add `league_id` to every S3-backend function
      (`get_league_config_from_s3`, `get_owner_map_from_s3`, `get_payouts_config_from_s3`,
      `load_all_config`) — keys become `leagues/{league_id}/league_config.json`,
      `leagues/{league_id}/config/owner_map.json`, `leagues/{league_id}/config/weekly_payouts_config.json`
- [ ] `lambda_function.py`: `handler()` requires `event["league_ids"]` (list, non-empty) — missing/empty
      raises `ValueError`, same failure mode as an unknown step name; no manifest, no "run every known
      league" default (see decision #12a for why)
- [ ] `lambda_function.py`: nested loop — for each `league_id`, load that league's config via
      `load_all_config(league_id)`, then run the requested steps, uploading to
      `leagues/{league_id}/<filename>` instead of a bucket-root filename; `succeeded`/`failed` entries
      keyed `f"{league_id}:{label}"` so a multi-league invocation's result stays legible; one league's
      failure doesn't abort another league's run
- [ ] `template.yaml`: retire the decision #4/#7 explicit-key IAM statements (`WriteDataFiles`,
      `ReadConfig`); replace with one `s3:GetObject`/`s3:PutObject` statement on
      `arn:aws:s3:::${SiteBucketName}/leagues/*` (covers config + data outputs + `cache/` + `archive/`
      for every league in one grant)
- [ ] `template.yaml`: update the live/weekly `AWS::Scheduler::Schedule` `Input` values to include
      `league_ids` for whichever league(s) each schedule should cover
- [ ] One-time migration: move today's single league's existing bucket-root objects to
      `leagues/<league_id>/` (`aws s3 mv`/`cp`) before switching the Lambda's env/IAM over, so there's
      no window where neither location has current data
- [ ] ESPN creds: keep the single shared `EspnCredsSecret` (decision #12c) — no per-league secret
      resource for now; `get_credentials_from_secrets_manager()` keeps its existing `secret_name`
      override so a future per-league secret is a small addition, not a redesign, if ever needed
- [ ] Testing: a two-league invoke (`{"league_ids": ["<real>", "<scratch>"]}`) produces correctly
      separated output trees with no cross-league bleed; missing/empty `league_ids` fails loudly;
      confirm the retired decision #4/#7 IAM statements are actually gone once `leagues/*` is in place
- [ ] Frontend follow-up (not this checklist's job): `site/index.html`'s bare-filename `fetch()` calls
      break once outputs move under `leagues/<league_id>/` — flagged in
      [TODO-frontend.md](TODO-frontend.md), not required for this backend item to be done

## Deferred / later

- [ ] Failure alerting (e.g. a CloudWatch alarm on Lambda errors) — right now there's no plan to
      notice a silent weekly failure other than checking the site itself
- [ ] Step Functions / per-report parallel Lambdas — only if the single-Lambda approach stops
      fitting in 15 minutes
