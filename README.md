# espn-ff-s3-site

ESPN fantasy football league history site, mid-migration to AWS. See
[DESIGN.md](DESIGN.md) for the target architecture (S3 static hosting + a
scheduled Lambda that regenerates the data files) and the rollout plan.

This repo has completed **Phase 1 (Prep)** and **Phase 2 (static hosting)**: the data-generation
code has been ported into an importable `league_reports/` package, and the S3 site bucket is
deployed and live at `http://espn-ff-site-217412666418.s3-website-us-west-2.amazonaws.com` (stack
`espn-ff-s3-site`, region `us-west-2`), seeded with
`site/` plus one-time manually-copied sample data (see TODO-frontend.md). `lambda_function.py` and
its `template.yaml` resources are also built and deployed (Phase 3+, see TODO-backend.md for that
work's status) - the weekly schedule exists, but its season-cache/multi-league refinements are
still in progress there.

## Local development

1. Install dependencies (Python 3.9+):

   ```
   pip install -r requirements-dev.txt
   ```

   (`requirements.txt` alone is what `sam build` packages into the Lambda - deliberately just
   `espn-api`, no `pandas`, since the deployed Lambda gets pandas from the `AWSSDKPandas-Python311`
   layer instead, see DESIGN.md decision #3. `requirements-dev.txt` adds `pandas` on top, for
   running `scripts/*.py` locally.)

2. Credentials and owner map (gitignored, not secret-free):

   ```
   cp config/espn_creds.example.json ignore/espn_creds.json    # fill in swid/espn_s2
   cp config/owner_map.example.json ignore/owner_map.json      # team_id -> initials
   ```

3. League settings live in `league_config.json` (league id, year range,
   site title, award names, lineup slots, survivor cutoff) - not secret,
   safe to commit. `config/weekly_payouts_config.json` holds this season's
   weekly side-bet rules.

4. Generate the data:

   ```
   python scripts/run_all.py
   ```

   This writes `league_history.csv`, `head_to_head_lifetime.csv`,
   `advanced_team_metrics.csv`, `all_time_records.csv`,
   `most_drafted_players.csv`, `weekly_efficiency_awards.csv`,
   `survivor_results.json`, and `weekly_payout_winners.json` to the project
   root. These are gitignored here (see DESIGN.md - in the target
   architecture only the Lambda writes them, into S3, never into git).

5. Preview the site: `site/index.html` fetches those data files with
   relative paths, so serve the **project root** (not `site/`) to view it
   locally, e.g.:

   ```
   python -m http.server 8000
   # then open http://localhost:8000/site/
   ```

   (This mirrors how Phase 2 of the rollout syncs both `site/` and the
   generated data files into the same S3 bucket root.)

## Project layout

```
DESIGN.md                    # AWS architecture design + rollout plan
league_config.json           # per-league settings (not secret)
config/
  weekly_payouts_config.json   # payout rules per week
  espn_creds.example.json      # template - copy to ignore/espn_creds.json
  owner_map.example.json       # template - copy to ignore/owner_map.json
ignore/                      # gitignored: real creds + owner map
league_reports/              # shared report-building code
  espn_client.py                # retry-wrapped League() construction
  config.py                     # local-file config/credential loading
  reports/
    history.py                   # -> league_history.csv
    head_to_head.py               # -> head_to_head_lifetime.csv
    advanced_history.py           # -> advanced_team_metrics.csv
    owner_habits.py               # -> most_drafted_players.csv
    records.py                    # -> all_time_records.csv
    weekly_summary.py             # -> weekly_efficiency_awards.csv,
                                   #    survivor_results.json,
                                   #    weekly_payout_winners.json
scripts/                     # thin local CLI wrappers around league_reports/
  run_all.py                    # regenerates every data file
site/                        # index.html / style.css - the site itself
```

## What's not here yet

Per DESIGN.md's rollout phases, still open: the local-preview path is broken (see
TODO-frontend.md's "Local dev fix" - step 5 above doesn't actually work as written yet, since
`league_reports/config.py`'s `output_path()` writes to the repo root, not `site/`), a custom
domain/HTTPS, CI to auto-sync `site/` on push, and (per TODO-backend.md) the season-cache
optimization and multi-league partitioning.
