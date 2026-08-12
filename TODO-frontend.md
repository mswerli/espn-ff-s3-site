# Frontend TODO — Serving the S3 Site

Execution checklist for getting `site/` (and the data the Lambda produces) actually served from S3.
Rationale lives in [DESIGN.md](DESIGN.md); this is just the ordered work. Companion:
[TODO-backend.md](TODO-backend.md).

## Done

- [x] `site/index.html`, `site/style.css` committed — diverged from `who_dat_history`'s version:
      config-driven title/subtitle via `fetch("league_config.json")` (DESIGN.md decision #7)
- [x] Front-end/Lambda data-path mismatch fixed in the design (flat bucket root, not a `data/`
      prefix — decision #7)
- [x] `league_config.json`'s public-readability requirement identified and resolved in the design
      (decision #4) — canonical file stays at the repo root (matching `who_dat/config.py`); the
      deploy step copies it to the bucket root explicitly rather than moving it into `site/`
- [x] ~~Sample-data banner + temporary data source~~ (2026-08-11, superseded same day): was a
      temporary `DATA_BASE_URL` pointed at the scratch bucket + a visible banner while the real site
      bucket didn't exist yet. Now that the real bucket is deployed and seeded (see below),
      `DATA_BASE_URL` is back to `""` (same-origin relative fetches, decision #7) and the banner + its
      `.sample-data-banner`/`--warn-*` CSS are removed from `site/index.html`/`site/style.css`.
- [x] Real site bucket deployed and seeded (2026-08-11): `who-dat-league-217412666418` (us-west-2),
      stack `who-dat-infra`, website endpoint
      `http://who-dat-league-217412666418.s3-website-us-west-2.amazonaws.com`. Seeded via
      `aws s3 sync site/` + `aws s3 cp league_config.json` + one-time `aws s3 cp` of the 8 data files
      from the scratch bucket (`who-dat-league-scratch-217412666418`) as this first deploy's manual
      seed data, per the "Verification" section below. Confirmed all 11 objects (`index.html`,
      `style.css`, `league_config.json`, the 8 data files) 200 with correct `Content-Type`s from the
      website endpoint.

## Infrastructure (`template.yaml` — bucket-side resources; Lambda-side resources are in TODO-backend.md)

- [x] S3 bucket resource: static `WebsiteConfiguration` (index document `index.html`; error
      document decided as reusing `index.html` too — no dedicated error page exists, this is a
      one-page app, see DESIGN.md decision #1)
- [x] Bucket policy: public `s3:GetObject` on the whole bucket (no CloudFront/OAC needed, per
      decision #1)
- [x] Explicitly disable the relevant Block Public Access settings for this bucket in the template —
      S3 defaults block public bucket policies, so the bucket policy above won't take effect without this
      (`BlockPublicPolicy`/`RestrictPublicBuckets` set `false`; `BlockPublicAcls`/`IgnorePublicAcls`
      left `true` since this bucket never uses ACLs, only the bucket policy)
- [x] SAM template `Outputs`: website endpoint URL, bucket name (so the deploy script has something
      to target) — `SiteBucketWebsiteURL` / `SiteBucketNameOutput`
- [x] Once the real site bucket is deployed and synced: reset `site/index.html`'s `DATA_BASE_URL` to
      `""` (back to same-origin relative fetches, decision #7) and remove/repurpose the sample-data
      banner — done, see "Done" above.
- [ ] **Not done, needs a human call**: `who-dat-league-scratch-217412666418`'s public bucket
      policy/CORS (added for the sample-data banner, now unused now that the real bucket is live) —
      probably worth taking back down since nothing references it anymore, but it's Morrie's call
      whether to tear it down now or leave it (it's also still the backend's scratch-test bucket, so
      there may be other reasons to keep it around).

## Deploy tooling

- [x] `Makefile` targets (`sam build && sam deploy` is still run directly, not wrapped — it's a
      single well-known command and backend/frontend share the one template.yaml/stack, so wrapping
      it here didn't seem worth it):
  - [x] `make sync-site` → `aws s3 sync site/ s3://$BUCKET/` — static assets (no excludes needed —
        `site/` never contains a Lambda-written file, see decision #7)
  - [x] `make sync-config` → `aws s3 cp league_config.json s3://$BUCKET/league_config.json` — the one
        repo-root file the front-end also needs (decision #4)
  - [x] `make publish-site` — both of the above together
- [x] Decide how `$BUCKET` gets sourced for these commands — went with `sam list stack-outputs`-style
      lookup: a `make site-bucket` target runs
      `aws cloudformation describe-stacks --query "...SiteBucketNameOutput..."` and the sync targets
      call it via `$(MAKE) -s site-bucket`. `STACK_NAME`/`AWS_REGION` are overridable `?=` variables,
      defaulted to the real deployed stack (`who-dat-infra`, `us-west-2`).

## Local dev fix (currently broken)

- [ ] `site/index.html` lives in `site/`, but `who_dat/config.py`'s `output_path()` writes
      generated CSV/JSON to the **repo root**, so relative `fetch()`s 404 in local preview —
      README's current preview instructions don't actually work as written (DESIGN.md decision #8
      area). Pick one:
  - [ ] point local `output_path()` at `site/` instead of the repo root, and update the `.gitignore`
        patterns (currently anchored `/league_history.csv` etc.) to match, or
  - [ ] have the local preview step copy/symlink the root-level CSVs into `site/` before serving
- [ ] Update `README.md`'s local-preview instructions once one of the above is picked

## Verification

- [x] After first deploy: confirm the site renders — verified via `curl` against the website
      endpoint: `index.html`/`style.css`/`league_config.json` and all 8 data files return `200`
      with parseable content (real ESPN sample data copied from the scratch bucket, see "Done"
      above). Not verified in an actual browser/headless-Chrome (none available in this environment)
      — worth a human eyeball pass to confirm sort behavior etc. actually works, though the JS is
      unchanged from what already rendered correctly against the scratch bucket during the
      sample-data-banner period.
- [x] Confirm `Content-Type` headers land correctly via `aws s3 sync` — checked via
      `aws s3api head-object`: `index.html` → `text/html`, `style.css` → `text/css`,
      `league_config.json` → `application/json`, `league_history.csv` → `text/csv` (confirmed the
      less-common CSV mapping specifically, as called out below), `survivor_results.json` →
      `application/json`.
- [x] Confirm filenames match **exactly**, case included, between Lambda output and what
      `index.html` fetches (see the table in DESIGN.md decision #7) — S3 keys are case-sensitive.
      Confirmed by directly `curl`ing each of the 8 exact filenames from decision #7's table plus
      `league_config.json` and getting `200`s.

## Deferred / later (DESIGN.md "Open items")

- [ ] Custom domain + HTTPS (CloudFront + ACM + Route 53)
- [ ] CI (GitHub Actions) to auto-sync `site/` on push
- [ ] Front-end error-handling gap (decision #8): 8 of the 9 `fetch()` calls have no `.catch()` — a
      missing/failed file just silently leaves a table empty. Worth a "data last updated" indicator
      or a visible error state once the pipeline is live and a failure becomes an unattended-Lambda
      risk rather than a "did I forget to copy the file up" one.
- [ ] **Breaking, flagged by the backend's multi-league work (DESIGN.md decision #12, TODO-backend.md
      "Multi-league support"):** once the Lambda partitions its outputs under `leagues/<league_id>/`,
      `index.html`'s bare-filename `fetch("league_history.csv")`-style calls (decision #7's table) stop
      resolving. Needs a front-end answer — a league picker, a query-string/subpath league selector, a
      per-league `site/` deploy, etc. — not designed by the backend side; not urgent until decision #12
      actually ships (still single-league today).
