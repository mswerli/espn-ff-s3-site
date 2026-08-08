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

## Infrastructure (`template.yaml` — bucket-side resources; Lambda-side resources are in TODO-backend.md)

- [ ] S3 bucket resource: static `WebsiteConfiguration` (index document `index.html`; an error
      document isn't decided yet — worth picking one so a missing page doesn't just show S3's raw
      XML error)
- [ ] Bucket policy: public `s3:GetObject` on the whole bucket (no CloudFront/OAC needed, per
      decision #1)
- [ ] Explicitly disable the relevant Block Public Access settings for this bucket in the template —
      S3 defaults block public bucket policies, so the bucket policy above won't take effect without this
- [ ] SAM template `Outputs`: website endpoint URL, bucket name (so the deploy script has something
      to target)

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
