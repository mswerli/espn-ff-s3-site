# Backend TODO — Lambda Data Generation

Execution checklist for the Lambda that regenerates the site's CSV/JSON data. Rationale lives in
[DESIGN.md](DESIGN.md); this is just the ordered work. Companion: [TODO-frontend.md](TODO-frontend.md).

Done and deployed: Phase 1 (Prep), the Lambda handler + Lambda-side `template.yaml` infra, initial
testing, decision #13 (incremental current-year refresh — shared `League()` fetch, per-year + raw
box-score caching, `head_to_head`/`advanced_history`/`records`/`weekly_summary` cut over to
production), decision #11 (weekly-summary backfill + retention — `event["year"]` override,
`archive/` season-stamped copies), and decision #10's cadence-tier schedule split (weekly rollup
Tuesday 17:00; `weekly_summary_v2` payout tier hourly during Thu/Sun/Mon game windows — see
DESIGN.md decision #10's status table for the exact cron expressions). See [DESIGN.md](DESIGN.md) and
[DESIGN-incremental-espn-pipeline.md](DESIGN-incremental-espn-pipeline.md) for what shipped, `git log`
for when, [[espn-ff-live-infra]] memory for what's live today. This file only tracks what's still open.

- [ ] Frontend follow-up: whether/how `site/` ever surfaces `archive/` files (decision #11's
      season-stamped weekly-summary copies) — flag for [TODO-frontend.md](TODO-frontend.md) if wanted,
      not required for any backend item

## Multi-league support (DESIGN.md decision #12)

- [ ] `league_reports/config.py`: add `league_id` to every S3-backend function
      (`get_league_config_from_s3`, `get_owner_map_from_s3`, `get_payouts_config_from_s3`,
      `load_all_config`) — keys become `leagues/{league_id}/league_config.json`,
      `leagues/{league_id}/config/owner_map.json`, `leagues/{league_id}/config/weekly_payouts_config.json`
- [ ] `lambda_function.py`: `handler()` requires `event["league_ids"]` (list, non-empty) — missing/empty
      raises `ValueError`; no manifest, no "run every known league" default
- [ ] `lambda_function.py`: nested loop — for each `league_id`, load its config via
      `load_all_config(league_id)`, run the requested steps, upload to `leagues/{league_id}/<filename>`
      instead of a bucket-root filename; `succeeded`/`failed` entries keyed `f"{league_id}:{label}"`;
      one league's failure doesn't abort another's
- [ ] `template.yaml`: retire the decision #4/#7 explicit-key IAM statements (`WriteDataFiles`,
      `ReadConfig`) once outputs actually move under `leagues/<league_id>/` — the `leagues/*` grant
      itself already exists (`ReadWriteLeaguesPrefix`, shipped as part of decision #13), it's just not
      yet the *only* statement since the published CSV/JSON outputs are still flat at the bucket root
- [ ] `template.yaml`: update the live/weekly schedules' `Input` values to include `league_ids` for
      whichever league(s) each should cover
- [ ] One-time migration: move today's single league's existing bucket-root objects to
      `leagues/<league_id>/` before switching the Lambda's env/IAM over, so there's no window where
      neither location has current data
- [ ] Testing: a two-league invoke produces correctly separated output trees with no cross-league
      bleed; missing/empty `league_ids` fails loudly; confirm the retired decision #4/#7 IAM statements
      are actually gone once `leagues/*` is the only grant
- [ ] Frontend follow-up: `site/index.html`'s bare-filename `fetch()` calls break once outputs move
      under `leagues/<league_id>/` — flagged in [TODO-frontend.md](TODO-frontend.md), not required for
      this backend item

## Deferred / later

- [ ] Failure alerting (e.g. a CloudWatch alarm on Lambda errors) — right now there's no plan to
      notice a silent weekly failure other than checking the site itself
- [ ] Step Functions / per-report parallel Lambdas — only if the single-Lambda approach stops
      fitting in 15 minutes
