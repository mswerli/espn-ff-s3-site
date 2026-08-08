# who-dat-infra

Fantasy football league history site, mid-migration to AWS. See
[DESIGN.md](DESIGN.md) for the target architecture (S3 static hosting + a
scheduled Lambda that regenerates the data files) and the rollout plan.

This repo is currently at **Phase 1 (Prep)**: the data-generation code has
been ported from the original [who_dat_history](https://github.com/) repo
into an importable `who_dat/` package, so the same report-building
functions can run from a local script today and from a Lambda handler once
that's built (Phase 3+ in DESIGN.md). Nothing is deployed to AWS yet -
`template.yaml` and `lambda_function.py` don't exist.

## Local development

1. Install dependencies (Python 3.9+):

   ```
   pip install -r requirements.txt
   ```

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
who_dat/                     # shared report-building code
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
scripts/                     # thin local CLI wrappers around who_dat/
  run_all.py                    # regenerates every data file
site/                        # index.html / style.css - the site itself
```

## What's not here yet

Per DESIGN.md's rollout phases, still to build: `template.yaml` (SAM:
bucket, Lambda, layer, schedule, role), `lambda_function.py` (the handler
that calls the same `who_dat/reports/*` functions and uploads to S3), the
Secrets Manager/S3 config wiring, and the season-cache optimization.
