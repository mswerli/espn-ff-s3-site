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

## Multi-league support (DESIGN.md decision #15 — done, supersedes decision #12)

Decision #12's shared-bucket/`leagues/<league_id>/`-prefix design was never built — superseded before
implementation once the actual requirement showed up (sites need to be visibly separate, not merged
under one site with a league picker). What's built instead: `template.yaml`/`lambda_function.py`
unchanged (already fully parameterized — `SiteBucketName`/`FunctionName` — so a second league is just a
second stack deploy, not a code change); `leagues/<slug>/` + `ignore/leagues/<slug>/` repo layout;
`leagues/registry.json` mapping a slug to its deployed stack/bucket/function names; `FF_LEAGUE`-driven
local config resolution; `LEAGUE=`-parameterized Makefile targets (`deploy-league`, `publish-site`, ...).
Two leagues live: `who-dat` (original) and `all-for-the-shiva` (added same day — league_id 854288221,
migrated from NFL.com, first ESPN-native season 2026). See DESIGN.md decision #15 (15a–15g) for the
full design and 15f for how the second league's config was derived.

- [ ] Adding a third league is a pure config + `leagues/registry.json` + `make deploy-league` operation
      at this point — no code change expected, but worth confirming the first time it's actually done
- [ ] Frontend follow-up: `site/index.html`'s bare-filename `fetch()` calls needed **no** change (15d) —
      each league's bucket only ever holds its own flat files, same as today's single-league layout, so
      this resolves TODO-frontend.md's previously-flagged "what does the front end do once outputs are
      partitioned" question (answer: they aren't — sites are separate instead)

## Deferred / later

- [ ] Failure alerting (e.g. a CloudWatch alarm on Lambda errors) — right now there's no plan to
      notice a silent weekly failure other than checking the site itself
- [ ] Step Functions / per-report parallel Lambdas — only if the single-Lambda approach stops
      fitting in 15 minutes
