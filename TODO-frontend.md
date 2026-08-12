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
- [x] Sample-data banner + temporary data source (2026-08-11): `site/index.html` now shows a visible
      "sample data" notice, and all 9 `fetch()` calls go through a `dataUrl()` helper pointed at
      `DATA_BASE_URL` — currently the scratch S3 bucket from backend testing
      (`who-dat-league-scratch-217412666418`, us-west-2), made public read-only for exactly the 9
      files the page fetches (`config/*` stays private) plus a permissive CORS rule so a locally
      previewed page can fetch it cross-origin. **Must be reset to `DATA_BASE_URL = ""` once the real
      site bucket exists and the Lambda is syncing it** (the checklist item below) — this is
      explicitly a temporary stand-in, not the real hosting, and the scratch bucket's public policy
      should arguably come back down once it's no longer needed for this.

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
- [ ] Once the real site bucket is deployed and synced: reset `site/index.html`'s `DATA_BASE_URL` to
      `""` (back to same-origin relative fetches, decision #7) and remove/repurpose the sample-data
      banner; also reconsider whether `who-dat-league-scratch-217412666418`'s public bucket
      policy/CORS (added for the sample-data banner, see "Done" above) should be taken back down at
      that point, since it'll no longer be needed for this

## Deploy tooling

- [ ] `Makefile` (or equivalent) targets:
  - [ ] `sam build && sam deploy` — infra + Lambda code
  - [ ] `aws s3 sync site/ s3://$BUCKET/` — static assets (no excludes needed — `site/` never
        contains a Lambda-written file, see decision #7)
  - [ ] `aws s3 cp league_config.json s3://$BUCKET/league_config.json` — the one repo-root file the
        front-end also needs (decision #4)
- [ ] Decide how `$BUCKET` gets sourced for these commands — `sam list stack-outputs` lookup vs.
      just noting it down after the first deploy

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

- [ ] After first deploy: confirm the site renders identically to `who_dat_history`'s current live
      GitHub Pages version (same tables, same sort behavior), using the Lambda-produced (or
      manually-seeded, for the very first deploy before the Lambda has run) data files
- [ ] Confirm `Content-Type` headers land correctly via `aws s3 sync` — should auto-guess
      `text/html`/`text/css`/`application/json`; specifically verify `text/csv`, it's a less common
      extension-to-mimetype mapping and worth not assuming
- [ ] Confirm filenames match **exactly**, case included, between Lambda output and what
      `index.html` fetches (see the table in DESIGN.md decision #7) — S3 keys are case-sensitive

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
