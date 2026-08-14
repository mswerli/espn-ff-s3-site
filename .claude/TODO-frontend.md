# Frontend TODO — Serving the S3 Site

Execution checklist for getting `site/` (and the data the Lambda produces) actually served from S3.
Rationale lives in [DESIGN.md](DESIGN.md); this is just the ordered work. Companion:
[TODO-backend.md](TODO-backend.md).

`site/`, the bucket-side `template.yaml` infrastructure, and the `Makefile` deploy tooling are done
and deployed — see DESIGN.md decisions #1/#4/#7 for the rationale, `git log` for when, and
[[espn-ff-live-infra]] memory for what's actually live today.

## Local dev fix (currently broken)

- [ ] `site/index.html` lives in `site/`, but `league_reports/config.py`'s `output_path()` writes
      generated CSV/JSON to the **repo root**, so relative `fetch()`s 404 in local preview —
      README's current preview instructions don't actually work as written (DESIGN.md decision #8
      area). Pick one:
  - [ ] point local `output_path()` at `site/` instead of the repo root, and update the `.gitignore`
        patterns (currently anchored `/league_history.csv` etc.) to match, or
  - [ ] have the local preview step copy/symlink the root-level CSVs into `site/` before serving
- [ ] Update `README.md`'s local-preview instructions once one of the above is picked
- [ ] Still worth a human eyeball pass in an actual browser (sort behavior etc.) once the above is
      fixed — every check so far has only been `curl`/`head-object`, no headless-Chrome available in
      this environment

## Deferred / later (DESIGN.md "Open items")

- [ ] Custom domain + HTTPS (CloudFront + ACM + Route 53)
- [ ] CI (GitHub Actions) to auto-sync `site/` on push
- [ ] Front-end error-handling gap (decision #8): 8 of the 9 `fetch()` calls have no `.catch()` — a
      missing/failed file just silently leaves a table empty. Worth a "data last updated" indicator
      or a visible error state once the pipeline is live and a failure becomes an unattended-Lambda
      risk rather than a "did I forget to copy the file up" one.
- ~~Breaking, flagged by the backend's multi-league work~~ — resolved by DESIGN.md decision #15
      (supersedes decision #12): multi-league support landed as **separate per-league sites** (their
      own bucket, deployed via the same `site/`), not one shared bucket with a `leagues/<league_id>/`
      prefix — so `index.html`'s bare-filename `fetch()` calls never needed to change at all. Two
      leagues live today (`who-dat`, `all-for-the-shiva`); `site/`/`style.css` are unmodified, deployed
      identically to both via `make publish-site LEAGUE=<slug>`.
