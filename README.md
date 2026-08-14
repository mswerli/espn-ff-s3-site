# espn-ff-s3-site

ESPN fantasy football league history site, mid-migration to AWS. See
[DESIGN.md](.claude/DESIGN.md) for the target architecture (S3 static hosting + a
scheduled Lambda that regenerates the data files) and the rollout plan.

This repo has completed **Phase 1 (Prep)** and **Phase 2 (static hosting)**: the data-generation
code has been ported into an importable `league_reports/` package, and the S3 site bucket is
deployed and live at `http://espn-ff-site-217412666418.s3-website-us-west-2.amazonaws.com` (stack
`espn-ff-s3-site`, region `us-west-2`), seeded with
`site/` plus real generated data. `lambda_function.py` and its `template.yaml` resources are also built
and deployed (Phase 3+, see .claude/TODO-backend.md for that work's status) - per-year caching, a
cadence-tiered schedule, and multi-league support (two leagues live today, each its own stack - see
DESIGN.md decision #15 and `leagues/registry.json`) are all done.

## Local development

1. Install dependencies (Python 3.9+):

   ```
   pip install -r requirements-dev.txt
   ```

   (`requirements.txt` alone is what `sam build` packages into the Lambda - deliberately just
   `espn-api`, no `pandas`, since the deployed Lambda gets pandas from the `AWSSDKPandas-Python311`
   layer instead, see .claude/DESIGN.md decision #3. `requirements-dev.txt` adds `pandas` on top, for
   running `scripts/*.py` locally.)

2. This repo drives more than one league (DESIGN.md decision #15) - which one local scripts use is
   `FF_LEAGUE` (env var, defaults to `who-dat`; see `leagues/registry.json` for every league's slug).
   Credentials are shared across every league (one ESPN login, decision #12c); the owner map is
   per-league and gitignored, not secret-free:

   ```
   cp config/espn_creds.example.json ignore/espn_creds.json               # fill in swid/espn_s2, shared
   mkdir -p ignore/leagues/who-dat
   cp leagues/owner_map.example.json ignore/leagues/who-dat/owner_map.json  # team_id -> initials
   ```

   (For a different league: `FF_LEAGUE=<slug> ...` and `ignore/leagues/<slug>/owner_map.json`.)

3. League settings live in `leagues/<slug>/league_config.json` (league id, year range, site title,
   award names, lineup slots, survivor cutoff) - not secret, safe to commit.
   `leagues/<slug>/weekly_payouts_config.json` holds that league's weekly side-bet rules (optional -
   not every league runs payouts).

4. Generate the data:

   ```
   python scripts/run_all.py
   ```

   This writes `league_history.csv`, `head_to_head_lifetime.csv`,
   `advanced_team_metrics.csv`, `all_time_records.csv`,
   `most_drafted_players.csv`, `weekly_efficiency_awards.csv`,
   `survivor_results.json`, and `weekly_payout_winners.json` to the project
   root. These are gitignored here (see .claude/DESIGN.md - in the target
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
.claude/DESIGN.md                    # AWS architecture design + rollout plan
leagues/                     # one directory per league (DESIGN.md decision #15) - not secret
  registry.json                 # slug -> deployed stack/bucket/function names
  <slug>/league_config.json        # league id, year range, site title, award names, lineup, ...
  <slug>/weekly_payouts_config.json  # that league's weekly side-bet rules (optional)
  owner_map.example.json        # template - copy to ignore/leagues/<slug>/owner_map.json
config/
  espn_creds.example.json      # template - copy to ignore/espn_creds.json (shared across leagues)
ignore/                      # gitignored: real creds (shared) + per-league owner maps
  espn_creds.json
  leagues/<slug>/owner_map.json
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
