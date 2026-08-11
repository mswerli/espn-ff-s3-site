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

## Deferred / later

- [ ] Season-cache optimization (DESIGN.md decision #5): cache each completed season's output in
      S3, only re-fetch the current season each run — needed once weekly runtimes start pushing the
      timeout, not needed for MVP
- [ ] Failure alerting (e.g. a CloudWatch alarm on Lambda errors) — right now there's no plan to
      notice a silent weekly failure other than checking the site itself
- [ ] Step Functions / per-report parallel Lambdas — only if the single-Lambda approach stops
      fitting in 15 minutes
