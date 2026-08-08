"""Regenerate every data file for the league configured in league_config.json.

Run from anywhere:  python scripts/run_all.py

Each step is independent - one ESPN hiccup (e.g. a season with no box score
data) shouldn't stop the rest from running, so failures are reported and
the script moves on.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

STEPS = [
    ("League history", "get_history"),
    ("Head-to-head records", "head_to_head"),
    ("Advanced team metrics", "advanced_history"),
    ("Draft habits", "owner_habits"),
    ("All-time records", "records"),
    ("Weekly summaries / payouts / survivor pool", "weekly_summary"),
]


def main():
    failures = []

    for label, module_name in STEPS:
        print(f"\n=== {label} ({module_name}.py) ===")
        try:
            module = __import__(module_name)
            module.main()
        except Exception as e:
            print(f"❌ {label} failed: {e}")
            failures.append(label)

    print("\n=== Done ===")
    if failures:
        print(f"{len(failures)} step(s) failed: {', '.join(failures)}")
        sys.exit(1)
    print("All data files regenerated.")


if __name__ == "__main__":
    main()
