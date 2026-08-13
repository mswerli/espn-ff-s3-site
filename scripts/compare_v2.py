"""Local diff harness: legacy report builder vs its _v2 replacement, run
back-to-back against the same config/credentials, no S3 involved.

.claude/DESIGN-incremental-espn-pipeline.md's rollout order, step 6: this is
meant to catch logic bugs against historical (closed) seasons fast, before
anything touches S3 or a real Lambda invocation. The live-shadow comparison
(deployed _v2 steps vs production, diffed via S3) is a separate, later check
that this doesn't replace - this only exercises whatever season(s) you point
it at locally, so it can't see current-week/in-progress-game behavior.

Usage:
    python scripts/compare_v2.py head_to_head                # full configured year range
    python scripts/compare_v2.py head_to_head --years 2023    # one year, fast iteration
    python scripts/compare_v2.py head_to_head --years 2023-2024
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from league_reports.config import get_credentials, get_league_config, get_owner_map, year_range
from league_reports.reports.advanced_history import build_advanced_history
from league_reports.reports.advanced_history_v2 import build_advanced_history_v2
from league_reports.reports.head_to_head import build_head_to_head
from league_reports.reports.head_to_head_v2 import build_head_to_head_v2

FLOAT_TOLERANCE = 0.01  # both reports round to 2 decimals; this just absorbs summation-order noise


# box_scores() (and everything built on it, per DESIGN.md decision #3's
# scripts/advanced_history.py note) isn't reliable before 2019 - matches
# lambda_function.py's year_range(config, span="box_score") for this step.
DEFAULT_SPAN = {"head_to_head": "full", "advanced_history": "box_score"}


def _parse_years(spec, config, span="full"):
    if spec is None:
        return year_range(config, span=span)
    if "-" in spec:
        start, end = spec.split("-", 1)
        return range(int(start), int(end) + 1)
    return [int(spec)]


def _diff_frames(key_cols, v1_df, v2_df):
    """Outer-merge on key_cols and report: keys only in one side, and any
    differing values (beyond FLOAT_TOLERANCE for numeric columns) for keys
    in both. Returns True if the two frames match (within tolerance)."""
    merged = v1_df.merge(v2_df, on=key_cols, how="outer", suffixes=("_v1", "_v2"), indicator=True)

    only_v1 = merged[merged["_merge"] == "left_only"]
    only_v2 = merged[merged["_merge"] == "right_only"]
    both = merged[merged["_merge"] == "both"]

    clean = True

    if not only_v1.empty:
        clean = False
        print(f"\n-- {len(only_v1)} row(s) only in v1 (legacy) --")
        print(only_v1[key_cols].to_string(index=False))

    if not only_v2.empty:
        clean = False
        print(f"\n-- {len(only_v2)} row(s) only in v2 --")
        print(only_v2[key_cols].to_string(index=False))

    value_cols = [c for c in v1_df.columns if c not in key_cols]
    row_diffs = []
    for _, row in both.iterrows():
        diffs = {}
        for col in value_cols:
            a, b = row.get(f"{col}_v1"), row.get(f"{col}_v2")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(a - b) > FLOAT_TOLERANCE:
                    diffs[col] = (a, b)
            elif a != b:
                diffs[col] = (a, b)
        if diffs:
            row_diffs.append((tuple(row[k] for k in key_cols), diffs))

    if row_diffs:
        clean = False
        print(f"\n-- {len(row_diffs)} row(s) differ (v1 -> v2) --")
        for key, diffs in row_diffs:
            print(f"  {dict(zip(key_cols, key))}:")
            for col, (a, b) in diffs.items():
                print(f"    {col}: {a!r} -> {b!r}")

    if clean:
        print(f"\nClean: {len(both)} row(s) match within tolerance, no rows only on one side.")

    return clean


def compare_head_to_head(config, creds, owner_map, years):
    print(f"Building v1 (legacy) head_to_head for {list(years)}...")
    v1 = build_head_to_head(
        league_id=config["league_id"], years=years,
        swid=creds["swid"], espn_s2=creds["espn_s2"],
    )
    print(f"Building v2 head_to_head for {list(years)}...")
    v2 = build_head_to_head_v2(
        league_id=config["league_id"], years=years,
        swid=creds["swid"], espn_s2=creds["espn_s2"],
    )
    return _diff_frames(["Owner ID", "Opponent ID"], v1, v2)


def compare_advanced_history(config, creds, owner_map, years):
    print(f"Building v1 (legacy) advanced_history for {list(years)}...")
    v1 = build_advanced_history(
        league_id=config["league_id"], years=years, owner_map=owner_map,
        swid=creds["swid"], espn_s2=creds["espn_s2"],
    )
    print(f"Building v2 advanced_history for {list(years)}...")
    v2 = build_advanced_history_v2(
        league_id=config["league_id"], years=years, owner_map=owner_map,
        swid=creds["swid"], espn_s2=creds["espn_s2"],
    )
    return _diff_frames(["Year", "Owner ID"], v1, v2)


REPORTS = {
    "head_to_head": compare_head_to_head,
    "advanced_history": compare_advanced_history,
    # weekly_summary added here once its _v2 module exists
    # (rollout order step 3 in DESIGN-incremental-espn-pipeline.md)
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", choices=sorted(REPORTS.keys()))
    parser.add_argument("--years", help="single year (2023) or range (2023-2024); default: full configured range")
    args = parser.parse_args()

    config = get_league_config()
    creds = get_credentials()
    owner_map = get_owner_map()
    years = _parse_years(args.years, config, span=DEFAULT_SPAN.get(args.report, "full"))

    clean = REPORTS[args.report](config, creds, owner_map, years)

    if not clean:
        print(f"\n{args.report}: DIFFERENCES FOUND (see above) - expected for head_to_head's "
              "unplayed-week fix on an in-progress season only; unexpected on a fully closed year")
        sys.exit(1)
    print(f"\n{args.report}: v1 and v2 match.")


if __name__ == "__main__":
    main()
