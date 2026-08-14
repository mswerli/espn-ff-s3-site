# Incremental ESPN data pipeline — shared fetch, raw box-score cache, weekly partitioning

Status: parts A–C implemented, validated, and **cut over to production** — `DEFAULT_STEPS` and the
weekly schedule run the `_v2` steps as of the `cutover-v2-weekly-run` branch (see "Cutover criteria"
below for why the originally-planned observation window was skipped). Part D (frontend partitioning)
not started. Built across `docs/incremental-espn-pipeline` (merged to `main`) and
`cutover-v2-weekly-run`.

This is decision #13 for [DESIGN.md](DESIGN.md) — kept as its own document rather than another
DESIGN.md section because it's implementation-level enough (specific S3 keys, specific new modules,
a specific rollout mechanism) to want its own space, not because it's unrelated. DESIGN.md and
[TODO-backend.md](TODO-backend.md) both link here rather than duplicating this content. Everything
here builds on, and doesn't replace, decisions #10/#11/#12 (per-year season cache, weekly-summary
backfill/retention, multi-league `leagues/<league_id>/` partitioning) — the per-year cache for closed
years stays exactly as designed; this document is about the *current* year, and about the ESPN
call-volume problem underneath it turning out to be different from what decision #10 assumed.

## Why this supersedes part of decision #10's plan

Decision #10 assumed the fix for "current year gets fully recomputed every run" was per-**year**
caching extended down to per-**week** partial-aggregates-plus-merge, for every report that reduces
across weeks. Reading `espn_api`'s actual source (not just its public method docstrings) turned up
something more specific: most of the redundant cost isn't "the current year's weeks get re-walked
every run" so much as "the same already-fetched data gets re-fetched by every report function
independently, including data that was never per-week to begin with." Two concrete findings:

1. **`League()` construction is called 5 times per invocation for the same year, and each call does 4
   HTTP requests** (`league.py` → `get_league()`, `get_pro_players()`, `get_pro_schedule()`,
   `get_league_draft()`) — `lambda_function.py`'s five step functions
   (`_step_history`, `_step_head_to_head`, `_step_advanced_history`, and `_step_weekly_summary`'s two
   separate builder calls) each construct their own `League`. One of those four calls
   (`get_pro_players()`) fetches the entire active NFL player pool and is read by none of the four
   reports in scope here.
2. **`head_to_head.py`'s per-week loop needs zero ESPN calls beyond the one `League()` construction
   above.** `league.scoreboard(week)` takes no server-side week filter — it fetches the whole season's
   schedule every time and discards everything but the requested week client-side — and that same
   per-week W/L/T/PF/PA data is *already* sitting on `team.scores[w]` / `team.schedule[w]` /
   `team.outcomes[w]` (`team.py`'s `_fetch_schedule()`) the moment `League()` returns, populated from
   the one `get_league()` call every construction already makes.

Only `league.box_scores(week)` (used by `advanced_history.py`, and by both
`build_weekly_efficiency()`/`build_weekly_payouts()` in `weekly_summary.py`) is genuinely server-side
filtered per week (`scoringPeriodId` + `filterMatchupPeriodIds`) — that's the one resource actually
worth caching/partitioning per week. It's called 3x redundantly per week today (once per consumer),
for identical data each time.

## Architecture

### A. One shared `League()` per `(league_id, year)` per invocation

New: a construction cache scoped to a single Lambda invocation (a plain dict keyed by
`(league_id, year)`, not persisted to S3 — this is invocation-local, not cross-run). Every step
function takes the already-constructed `League` instead of calling `get_league()` itself.
`lambda_function.py`'s per-league loop (decision #12) constructs it once per league/year combination
actually needed, before running that league's requested steps.

### B. `head_to_head` derived from `Team` arrays, no per-week fetch, no cache

New `build_head_to_head_v2()`: walks `league.teams[*].scores/schedule/outcomes` directly instead of
calling `scoreboard()` in a loop. Filters `outcome == 'U'` (undecided/unplayed) — which, as a side
effect, fixes what looks like an existing bug in the current implementation: an in-progress current
season's future weeks get counted as 0–0 results in the lifetime aggregate today, since
`head_to_head.py`'s loop doesn't skip unplayed weeks. This report needs no S3 cache at all — it's as
cheap as `history.py` once this lands.

### C. One shared raw box-score cache, not per-report derived caches

Supersedes the `cache/head_to_head/<year>/<week>.json` / `cache/advanced_history/<year>/<week>.json`
partial-plus-merge design sketched earlier for these two reports. Instead:

```
leagues/<league_id>/raw/<year>/box_scores/week_<n>.json
```

New `league_reports/box_score_cache.py`: `get_box_scores(league, league_id, year, week, bucket)` reads
this key if present, else calls `league.box_scores(week)`, trims the result to only the fields any
consumer actually reads (`home_team_id`, `home_score`, `away_team_id`, `away_score`, and per player in
each lineup: `playerId`, `name`, `position`, `slot_position`, `points` — not the full `BoxPlayer`
object, which carries `pro_opponent`/`pro_pos_rank`/`game_played`/`on_bye_week` that nothing here
reads), writes it to that key, and returns it. A week counts as closed the same way decision #10a
already defines for years: `week < current_week` (or the season's `year < years.current`, for a
fully-closed season) is read-cached-or-compute-once; the current week is always (re)fetched and the
cache entry overwritten.

`advanced_history.py` and both `weekly_summary.py` builders switch their `league.box_scores(week)`
calls to `get_box_scores(...)`. **Their existing aggregation logic — the running SOS average, the
optimal-lineup calc, everything — does not need to be restructured into a partial-plus-merge shape.**
It keeps reducing over "however many weeks of data `get_box_scores` hands it," which is now a mix of
cached JSON reads (closed weeks) and one live fetch (current week), instead of N live fetches. This is
a much smaller code change than decision #10a's original plan for these two reports.

*(Optional, more aggressive, not part of the initial rollout: `box_scores()` itself makes 3 HTTP calls
— the lineup fetch plus `_get_positional_ratings()`/`_get_pro_schedule()`, neither of which feeds any
field these reports read. Bypassing the public method to skip those two — and similarly skipping
`_fetch_draft()`/`_fetch_players()` for any step but `owner_habits` — would cut cost further but means
reaching into `espn_api` internals not part of its public contract. Deferred; revisit only if the
above isn't enough.)*

### D. `weekly_summary` publish-side partitioning (frontend-facing, separate from A–C)

This part is about what the *browser* fetches, not ESPN call volume — same design as proposed
previously, included here for completeness since it's still part of "the site transitions to
partitioned files":

```
leagues/<league_id>/weekly/<year>/
  week_<n>.json     # that week's efficiency rows + award + payout + survivor delta
  manifest.json     # {"weeks": [...], "current_week": N,
                     #  "summary": {<owner>: {payouts, regression, crawlspace, clipboard}},
                     #  "survivor": {"eliminated": {...}, "remaining": [...]}}
```
`week_<n>.json` is written/overwritten only for the current week each run; `manifest.json`'s cumulative
`summary`/`survivor` fields are merged forward (this week's contribution folded into the previous
manifest), not recomputed from scratch. Frontend: `site/index.html`'s weekly-summary tab fetches
`manifest.json` for the dropdown + Summary Overview table, then lazy-fetches `week_<n>.json` only when
a week is selected. This is a real, if contained, rewrite of that one script block
(`site/index.html:727-874` today) — tracked separately in [TODO-frontend.md](TODO-frontend.md) once
backend parts A–C are validated, not bundled into the same PR.

## Validation strategy: run alongside the existing pipeline

Nothing above may touch the code paths currently in production until it's been checked against real
data, including real in-progress-week data (which a local historical replay can't fully exercise —
`box_scores()`'s current-week behavior, live scoring edge cases, etc.). The mechanism:

**New code lives in new modules; nothing existing is edited.**

```
league_reports/
  box_score_cache.py          # new — part C
  reports/
    head_to_head.py            # untouched — stays the production path
    head_to_head_v2.py         # new — part B
    advanced_history.py        # untouched — stays the production path
    advanced_history_v2.py     # new — part C, same output shape
    weekly_summary.py          # untouched — stays the production path
    weekly_summary_v2.py       # new — part C + D, same efficiency/survivor/payout shape
                                #        plus the new partitioned weekly/manifest output
```

`lambda_function.py` gets new step entries — `head_to_head_v2`, `advanced_history_v2`,
`weekly_summary_v2` — registered in the `STEPS` registry decision #10b already defines, alongside (not
replacing) `head_to_head`, `advanced_history`, `weekly_summary`. Critically, **the `_v2` steps publish
to a separate prefix**, never the real published keys:

```
leagues/<league_id>/shadow/head_to_head_lifetime.csv
leagues/<league_id>/shadow/advanced_team_metrics.csv
leagues/<league_id>/shadow/weekly_efficiency_awards.csv
leagues/<league_id>/shadow/survivor_results.json
leagues/<league_id>/shadow/weekly_payout_winners.json
leagues/<league_id>/shadow/weekly/<year>/...          # part D's partitioned output, also shadow-only
```

Because the `_v2` steps are pure additions to the `STEPS` registry, the existing `AAA::Scheduler`
resources (decision #10's live/weekly tiers) are **not changed at all** and keep invoking exactly the
production steps they do today, on the same schedule, writing to the same keys — the legacy pipeline
runs completely unmodified, in parallel, for as long as validation takes. The `_v2` steps are invoked
separately (manually, or via a new low-frequency schedule added purely for validation, e.g. once a
day) with a payload like `{"league_ids": [...], "steps": ["head_to_head_v2", "advanced_history_v2",
"weekly_summary_v2"]}`.

**Comparison, two tiers:**

1. **Local, fast, pre-deploy**: a new `scripts/compare_v2.py` calls both the legacy and `_v2` builder
   functions directly against the same config/credentials and diffs the resulting DataFrames/dicts
   in-process — no S3, no Lambda, catches most logic bugs against historical (closed) seasons in
   seconds. This is the first check, before anything touches S3 or a real invocation.
2. **Live shadow, post-deploy**: after `_v2` steps are deployed and invoked (manually or on the
   validation schedule) against the real current season, diff `leagues/<league_id>/shadow/*` against
   the production `leagues/<league_id>/*` for the same run. Expected result is byte-identical **except**
   for the head-to-head unplayed-week fix (part B) — that specific, called-out difference should be the
   only one, confirmed by eyeballing the diff rather than assuming. This tier is what actually exercises
   current-week/in-progress-game behavior the local replay can't.
3. **Live render, branch-only**: eyeballing CSV diffs doesn't catch everything a real page render
   would (e.g. anything downstream in `site/index.html`'s own parsing/rendering). `Makefile`'s
   `deploy-branch` target stands up a **full second stack** (own bucket, Lambda, IAM roles, secret,
   schedule — the schedule deployed `DISABLED`, so it never runs unattended) from the current git
   branch, named/parameterized so it can never collide with or be mistaken for production. `render-branch`
   then pushes `site/` + config to that stack's bucket and invokes every step needed for a fully
   working page — including publishing the `_v2` steps' output to that stack's bucket **root**, not
   just `shadow/`, via a new `AllowV2RootPublish` template parameter (default `"false"`, only ever
   `"true"` on a branch stack — see template.yaml's parameter description for the exact safety
   argument for why this can never leak into a production redeploy). The result: an actual public
   HTTP URL serving the `_v2` pipeline's real output, confirmed end-to-end (`curl`ing the site and its
   data files, not just checking upload succeeded) — not a substitute for tier 2's byte-diff, since
   this tier only proves the data renders, not that it matches production, but it's the tier that
   caught a real bug tier 1/2 couldn't have: the deployed Lambda's IAM role was missing `leagues/*`
   read/write entirely (decision #12's grant was never actually built — see "New IAM surface" below),
   and separately lacked a properly-scoped `s3:ListBucket`, which meant every *closed* year/week's
   cache-miss came back as `403 AccessDenied` instead of `404 NoSuchKey` and got silently skipped
   rather than computed — both invisible to local testing under a broader IAM user, both fixed in
   `template.yaml` once a real deploy surfaced them.

**Cutover criteria — decided, not what was originally planned above:** the N-consecutive-clean-cycles
observation window described above was never actually run — zero live-shadow cycles happened before
cutover. Explicit call: this is a hobby project, and the staged-rollout ceremony that plan was written
for wasn't judged worth the wait, given how much of it (local diffs across many seasons, a live render
on a real parallel stack, a full week-by-week replay of a real season) was already exercised through
other means before this decision. `lambda_function.py`'s `DEFAULT_STEPS` and `WeeklyDataRefreshSchedule`'s
`Input` switched straight to the `_v2` step names, and `template.yaml`'s `AllowV2RootPublish` default
flipped from `"false"` to `"true"` in the same change, since a `_v2` step now needs to publish to the
real bucket-root keys to be production's actual data path at all — without that flip, cutting over
`DEFAULT_STEPS` alone would have made the site's data silently stop updating (worse than a visible
break). The legacy modules and the `shadow/` prefix were deliberately **not** deleted as part of this —
that cleanup is still a separate, later, optional change (the legacy `head_to_head`/`advanced_history`/
`weekly_summary` steps stay fully callable by name), not bundled into the cutover. **Rollback**: point
`DEFAULT_STEPS`/the schedule's `Input` back at the legacy step names — the legacy code was never
touched or removed, so this is a plain revert, not a migration to undo.

## Rollout order

1. `league_reports/box_score_cache.py` (part C's cache primitive) — no dependents yet, testable in
   isolation against a scratch bucket.
2. `reports/head_to_head_v2.py` (part B) — the simplest, since it needs no cache at all; a pure
   refactor against `Team` arrays. `scripts/compare_v2.py`'s first target.
3. `reports/advanced_history_v2.py` and `reports/weekly_summary_v2.py` (part C, consuming step 1's
   cache) — larger, since these still contain the real aggregation math, just re-sourced.
4. `lambda_function.py`: invocation-local `League()` sharing (part A) — applies to *all* steps
   (legacy and `_v2` alike), so this can land any time after step 1 without waiting on 2/3, but should
   be validated with the same local-diff harness since it changes what every step receives.
5. `lambda_function.py`: `_v2` step registry entries, `shadow/` prefix upload paths.
6. Local diff pass (`scripts/compare_v2.py` against historical closed years) — must be clean before
   step 7.
7. Deploy; invoke `_v2` steps against the real league, live-shadow diff for N cycles per the
   cutover criteria above.
8. Cutover: flip `DEFAULT_STEPS`/schedule `Input` to the `_v2` names.
9. Cleanup: delete legacy `head_to_head.py`/`advanced_history.py`/`weekly_summary.py`'s superseded
   code paths (or the files, if nothing else references them) and the `shadow/` prefix; rename `_v2`
   modules to drop the suffix now that they're the only path.
10. Part D's frontend work (partitioned `weekly/`/`manifest.json` consumption in `site/index.html`) —
    separate PR, tracked in TODO-frontend.md, not blocking 1–9.

## New IAM surface

`leagues/*` (decision #12's grant) covers the new `raw/`, `cache/`, and `shadow/` sub-prefixes — same
reasoning as decision #10a's cache prefix, no separate statement needed per sub-prefix.

**Correction**: this was originally written assuming decision #12's `leagues/*` grant already existed
in `template.yaml`, since DESIGN.md had long since designed it. It hadn't actually been built —
`TODO-backend.md`'s "Multi-league support" checklist was (and still is) unchecked. Discovered only once
a real parallel stack was deployed and invoked (`Makefile`'s `deploy-branch`/`invoke-branch`): every
`_v2` step failed against the real IAM role. Fixed directly in `template.yaml` (a new
`ReadWriteLeaguesPrefix` policy on `DataGeneratorExecutionRole`) rather than left as a known gap, since
nothing in this document works against a real deployment without it. Also required a properly-scoped
`s3:ListBucket` (bucket ARN, `s3:prefix` condition limited to `leagues/*`) alongside `GetObject`/
`PutObject` — without it, a `GetObject` against a not-yet-cached key comes back `403 AccessDenied`
instead of `404 NoSuchKey` (S3 won't confirm-or-deny a key's existence to a caller with no `ListBucket`
permission at all), which `league_reports/cache.py`/`box_score_cache.py` only special-case as
`NoSuchKey` — an `AccessDenied` instead propagates as a real error, and every `_v2` step's per-year/
per-week `try`/`except` quietly swallowed it as "skip this year/week," silently dropping data instead
of computing it fresh. This is exactly the class of bug the "Live render, branch-only" validation tier
above exists to catch — no amount of local scratch-bucket testing under a broader IAM user surfaced it.

## Testing checklist

- [x] `scripts/compare_v2.py`: legacy vs `_v2` output identical (or intentionally different, per the
      one called-out head-to-head fix) for at least one fully-closed historical season — done for all
      three `_v2` reports across multiple closed seasons plus the current season
- [x] `box_score_cache.py`: a closed week produces zero ESPN calls on a second read — confirmed (3
      calls cold, 0 warm), including cross-*report* sharing (a week cached by `advanced_history_v2`
      read by `weekly_summary_v2` with zero further ESPN calls), not just cross-run for one report
- [x] Live render against a real deployed parallel stack (`deploy-branch`/`render-branch`): site
      actually serves `_v2`-computed data over public HTTP, `curl`-confirmed, not just upload-confirmed
- [x] `box_score_cache.py`: a week's cache entry is fully overwritten (not appended to) on a re-run
      while it's still the current week — the real league's current configured season is already fully
      complete, so this couldn't be exercised against genuinely live ESPN data; instead validated via
      `scripts/replay_2025.py`, a week-by-week replay of that real completed season using a new
      `max_week` parameter (added to `compute_advanced_history_year`/both `weekly_summary_v2` cached
      builders, default `None` = today's unchanged behavior) that stands in for `league.current_week` -
      box_scores(week) for a real past week always returns that week's true final result regardless of
      what "today" the caller pretends it is, so this replays exactly what a live weekly run would have
      seen without needing ESPN to cooperate. 14 simulated weekly runs against the scratch bucket:
      each week's data, once superseded by a later "closed" run, was fetched exactly once more (to
      populate its cache for the first time) and never again; the still-"current" week was refetched
      every run and never cached, exactly as designed; cross-report sharing held during the replay too
      (advanced_history_v2 paid the cost of catching up the newly-closed week each run,
      weekly_efficiency_v2 got it free moments later in the same run); final replay-converged output
      matched an independent, fully live, zero-cache computation over the same 14-week window
- [ ] Invocation-local `League()` sharing (part A): still not implemented even for the `_v2`-only scope
      decided in the lambda_function.py wiring pass — each `_v2` step still constructs its own `League()`
      per year
- [ ] Live shadow-vs-production diff clean for the agreed N cycles before cutover (criteria above) —
      the mechanism is built and proven to work end-to-end; the actual N-cycle observation window against
      the real production schedule hasn't started
- [ ] Post-cutover: confirm the production schedules' `succeeded`/`failed` reporting still keys
      correctly (decision #12's `f"{league_id}:{label}"` format) with the renamed step labels
- [ ] Post-cleanup: confirm `shadow/` prefix and legacy-only code are actually gone, not just unused

## Open items / explicitly deferred

- The optional aggressive tier in part C (bypassing `box_scores()`'s internal
  positional-ratings/pro-schedule calls, skipping `_fetch_draft()`/`_fetch_players()` for non-habits
  steps) — not part of this rollout; revisit only if steps 1–9 don't reduce runtime/call-volume enough.
- `records.py` (`all_time_records.csv`) — structurally similar to `advanced_history.py` (reduces across
  weeks) and would benefit from the same `box_score_cache.py`, but wasn't in scope for this pass;
  natural follow-up once this pipeline is proven.
- Exact N for the cutover-criteria cycle count isn't picked yet — needs a number agreed on before step
  7, not left as "whenever it feels safe."
