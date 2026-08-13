# Incremental ESPN data pipeline — shared fetch, raw box-score cache, weekly partitioning

Status: design only, nothing implemented yet. Branch: `docs/incremental-espn-pipeline`.

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

**Cutover criteria**: only after N consecutive clean live-shadow comparisons (a specific N to be
agreed before starting — a handful of weekly cycles across live game days seems like the right order
of magnitude, not a single run) does `lambda_function.py`'s `DEFAULT_STEPS` (decision #10b) and the
production schedules' `Input` switch from the legacy step names to the `_v2` ones, and the legacy
modules + `shadow/` prefix get deleted in a follow-up cleanup change. **Rollback**: since the legacy
steps/modules are untouched throughout validation, rollback at any point before cutover is "stop
invoking the `_v2` steps" — there is no migration to undo. Rollback after cutover is "point the
schedules' `Input` back at the legacy step names," which only works for as long as the legacy modules
haven't been deleted yet — that's the reason the cleanup deletion is a separate, later change and not
bundled into the cutover itself.

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

`leagues/*` (decision #12's existing grant) already covers the new `raw/` and `shadow/` sub-prefixes —
no new IAM statement needed, same reasoning as decision #10a's cache prefix.

## Testing checklist

- [ ] `scripts/compare_v2.py`: legacy vs `_v2` output identical (or intentionally different, per the
      one called-out head-to-head fix) for at least one fully-closed historical season
- [ ] `box_score_cache.py`: a closed week produces zero ESPN calls on a second read; a week's cache
      entry is fully overwritten (not appended to) on a re-run while it's still the current week
- [ ] Invocation-local `League()` sharing: assert on call count/log output that a multi-step invocation
      for one league/year constructs `League` exactly once, not once per step
- [ ] Live shadow-vs-production diff clean for the agreed N cycles before cutover (criteria above)
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
