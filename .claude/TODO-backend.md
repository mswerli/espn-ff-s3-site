# Backend TODO — Lambda Data Generation

Execution checklist for the Lambda that regenerates the site's CSV/JSON data. Rationale lives in
[DESIGN.md](DESIGN.md); this is just the ordered work. Companion: [TODO-frontend.md](TODO-frontend.md).

Phase 1 (Prep), the Lambda handler, and its Lambda-side `template.yaml` infrastructure are done and
deployed — see DESIGN.md decisions #3/#4/#6/#7 for the rationale, `git log` for when, and
[[espn-ff-live-infra]] memory for what's actually live today.

## Testing — done

- [x] Invoke locally (`sam local invoke` or a direct Python call) against a **scratch S3
      prefix/bucket**, not the live site bucket — exercised extensively via `espn-ff-site-scratch-*`
      throughout decision #13's work, plus a full parallel branch stack for real Lambda/IAM testing
- [x] Measure total runtime for a full run — cold-cache multi-season runs observed directly (a few
      minutes for a 13-season cold `advanced_history_v2`/`head_to_head_v2` run), comfortably inside
      the 900s cap; no formal profiling doc beyond that, not felt necessary at this data volume
- [x] Confirm output is byte-identical (or intentionally different — e.g. sort order) to what the
      local scripts produce for the same config — `scripts/compare_v2.py` across multiple seasons for
      every report that got a `_v2` rewrite; one intentional, documented difference (head_to_head's
      unplayed-week fix) and one incidental, benign one (floating-point summation-order noise, invisible
      after `toFixed(2)`) found and written up, not silently ignored
- [x] Only point it at the real site bucket after a scratch-prefix run has been eyeballed — production
      wasn't touched until after local + branch-stack + local-replay validation; a real test invocation
      against production itself confirmed the final cutover end-to-end (`curl`-verified live data)

## Incremental data generation (DESIGN.md decision #10) — mostly done, via decision #13

Promoted out of "Deferred / later" a while back; most of it ended up landing as part of decision #13's
work (see that section below and [DESIGN-incremental-espn-pipeline.md](DESIGN-incremental-espn-pipeline.md))
rather than as its own separate pass. Rationale for what's still open stays in DESIGN.md decision #10.

- [x] `league_reports/cache.py`: built — `get_cached_year`/`put_cached_year`/`get_or_compute_year` at
      `leagues/<league_id>/cache/<report>/<year>.json` (the multi-league-shaped path, pulled forward
      from decision #12 rather than the bucket-root `cache/<report>/<year>.json` originally sketched
      here), boto3 imported lazily
- [x] `lambda_function.py`: `STEPS` registry + `event.get("steps")` selection in `handler()` — built,
      unknown names raise `ValueError`, no `steps` key falls back to `DEFAULT_STEPS`
- [~] `advanced_history.py`: got the per-year cache-or-compute split, via `advanced_history_v2.py`
      (`compute_advanced_history_year()` + `build_advanced_history_v2_cached()`) — done.
      `history.py` did **not** get this treatment, deliberately: decision #13 found it doesn't loop
      per-week/per-year at all (`team.wins`/`points_for`/etc. already come back cumulative from a
      single `get_league()` call), so there's no re-fetch cost worth caching — it stays fully legacy,
      full-refetch every run, on purpose
- [~] `head_to_head.py`: done, but via a better mechanism than the one planned here — turned out to
      need **no per-week cache at all** (derives lifetime results straight from `Team.scores/schedule/
      outcomes`, already populated by the one `get_league()` call), just the same per-year cache as
      advanced_history for closed-year re-fetch avoidance. `records.py` was **not** touched — still
      needs the "reduce across all years" → "per-year partial + merge" refactor this item originally
      called for, including confirming and dropping the apparently-dead `player_season_totals`
      accumulator; genuinely still open
- [x] `owner_habits.py`: no cache needed, confirmed — excluded from `DEFAULT_STEPS`, on-demand-only via
      the `STEPS` registry
- [ ] `template.yaml`: **still open** — only one `WeeklyDataRefreshSchedule` exists; the live/weekly
      cadence-tier split (live tier: `weekly_summary` several times/game day; weekly tier: everything
      else once/week) was never built. Everything currently runs on the single weekly Tuesday-morning
      schedule, `_v2` steps included
- [x] `template.yaml` cache IAM grant: done differently than sketched — one `s3:GetObject`/
      `s3:PutObject` statement on `leagues/*` (pulled forward from decision #12) covers `cache/`, `raw/`,
      and any future per-league sub-prefix in one grant, not a `cache/*`-only statement
- [~] Testing: cached-vs-live byte-identical — done, extensively, for head_to_head/advanced_history/
      weekly_summary (decision #13's testing checklist). **Still open**: live-tier cron hours were
      never confirmed with Morrie against actual game-day timing — moot until the live/weekly split
      above is actually built

## Weekly-summary backfill + retention (DESIGN.md decision #11) — still not started

Same pass as the step registry item above (`_step_weekly_summary` is the thing being touched either
way) — do this alongside, not after. Full rationale in DESIGN.md decision #11. Key patterns below are
already written in their multi-league form (decision #12) since #10/#11/#12 are being built together —
see that section below.

Genuinely untouched by decision #13's work — `_step_weekly_summary_v2` hardcodes
`year = league_config["years"]["current"]`, no event-level `year` override, and no `archive/`-prefix
season-stamped copies exist. `league_reports/cache.py`/`box_score_cache.py` (decision #13) are exactly
the primitives this item would build on, though — a real head start if this gets picked up.

- [ ] `_step_weekly_summary` (and `handler()`'s event handling): accept an optional `year` in the
      event payload (`{"league_ids": [...], "steps": ["weekly_summary"], "year": 2024}`), defaulting to
      `league_config["years"]["current"]` when omitted — `build_weekly_efficiency`/
      `build_survivor_results`/`build_weekly_payouts` already take `year` as a parameter, so this is
      wiring, not a report-layer change
- [ ] `league_reports/cache.py` (same module as decision #10a): after computing a year's weekly-summary data,
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

## Multi-league support (DESIGN.md decision #12) — still not started

Build alongside the two sections above, not after — `league_reports/cache.py` and `_step_weekly_summary`'s
archive-write paths should take `league_id` from the start rather than being retrofitted. Full
rationale in DESIGN.md decision #12.

Partial overlap worth knowing about: the `leagues/<league_id>/` prefix shape below is already live in
production (`league_reports/cache.py`/`box_score_cache.py` write `leagues/885349/cache/...` and
`leagues/885349/raw/...` today, and `DataGeneratorExecutionRole` already has the `leagues/*` grant this
section calls for) — but only for the one hardcoded league `league_config.json` points at. None of the
actual multi-league machinery below (`event["league_ids"]`, the nested per-league loop, per-league
config-file paths, the bucket-root → `leagues/<league_id>/` migration for the *published* CSV/JSON
outputs) exists yet — those outputs are still flat at the bucket root, not under a `leagues/` prefix.

- [ ] `league_reports/config.py`: add `league_id` to every S3-backend function
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

## Incremental current-year refresh (DESIGN.md decision #13) — done, cut over to production

Refines the current-year half of the "Incremental data generation" section above — closed-year caching
there is unchanged. Full design, S3 layout, and rollout history (shared `League()` fetch, `head_to_head`
derived from `Team` arrays with no per-week cost, a shared raw box-score cache, the shadow-mode
validation mechanism, and the eventual cutover) live in
[DESIGN-incremental-espn-pipeline.md](DESIGN-incremental-espn-pipeline.md); this is just the pointer.

Turned out **not** to need the "Incremental data generation" and "Multi-league support" sections above
to land first, despite the original note here assuming it did — it only needed the one specific piece
those sections would have provided (the `leagues/<league_id>/` prefix's IAM grant), which got pulled
forward directly into `template.yaml` rather than waiting on the rest of decision #10/#12's scope
(the `STEPS` registry itself also ended up minimal, single-league, built alongside decision #13 rather
than as its own prerequisite). The actual multi-league feature (per-league config/outputs,
`event["league_ids"]`) is still fully unstarted, as noted in that section above — only the IAM/prefix
groundwork got pulled forward.

`DEFAULT_STEPS` and the weekly schedule's `Input` now run `head_to_head_v2`/`advanced_history_v2`/
`weekly_summary_v2` in place of their legacy counterparts — cut over deliberately without the
originally-planned N-cycle live-shadow observation window (a hobby project's call, not an oversight;
see DESIGN-incremental-espn-pipeline.md's "Cutover criteria" for the full reasoning). Legacy
`head_to_head`/`advanced_history`/`weekly_summary` modules and steps are untouched and still callable
by name — deleting them is optional future cleanup, not required.

**Fully closed out**: a real test invocation against production itself (not just the branch stack)
succeeded end-to-end post-cutover — all 5 requested steps green, fresh root-key data confirmed live on
the public site via `curl`. The shadow-mode dual write and its `AllowV2RootPublish` flag were then
retired entirely (no longer serving any purpose once cutover was proven) — each `_v2` step now writes
to exactly one place, same as every legacy step always has; the leftover `shadow/` objects were deleted
from the production bucket. The `espn-ff-s3-site-docs-incremental-esp` parallel branch stack used for
validation has been fully torn down (stack, bucket, and Lambda all confirmed gone) — nothing left
running or costing money beyond the one production stack.

## Deferred / later

- [ ] Failure alerting (e.g. a CloudWatch alarm on Lambda errors) — right now there's no plan to
      notice a silent weekly failure other than checking the site itself
- [ ] Step Functions / per-report parallel Lambdas — only if the single-Lambda approach stops
      fitting in 15 minutes
