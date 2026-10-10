"""Weekly payout rules, against hand-built box scores (no ESPN, no S3).

The "existing types" tests go through weekly_summary_v2's
compute_weekly_payout_week - the entry point every builder calls - and were
written against the pre-refactor if/elif chain, so they pin its behavior.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from league_reports.reports import weekly_summary_v2
from league_reports.reports.weekly_payouts import (
    PAYOUT_TYPES,
    RESULT_FILTERS,
    build_team_weeks,
    matchup_result,
    pick_payout_winner,
)

FLEX_SLOT = "RB/WR/TE"


def player(player_id, position, points, slot=None):
    return {
        "player_id": player_id,
        "name": f"P{player_id}",
        "position": position,
        "slot_position": slot or position,
        "points": points,
    }


def team(team_id, first, last):
    return SimpleNamespace(
        team_id=team_id,
        team_name=f"Team {team_id}",
        owners=[{"firstName": first, "lastName": last}],
    )


TEAMS = [
    team(1, "Al", "Ames"), team(2, "Bo", "Burke"), team(3, "Cy", "Cole"),
    team(4, "Di", "Dunn"), team(5, "Ed", "Eads"), team(6, "Fi", "Finn"),
]

# Starter totals: T1 50, T2 59, T3 58, T4 57, T5 40, T6 30.
LINEUPS = {
    1: [player(11, "QB", 20), player(12, "RB", 10), player(13, "WR", 12), player(14, "WR", 8, FLEX_SLOT),
        player(15, "RB", 50, "BE"), player(16, "TE", None)],
    2: [player(21, "QB", 15), player(22, "RB", 30), player(23, "WR", 5), player(24, "RB", 9, FLEX_SLOT)],
    3: [player(31, "QB", 25), player(32, "RB", 8), player(33, "WR", 22), player(34, "TE", 3, FLEX_SLOT)],
    4: [player(41, "QB", 10), player(42, "RB", 12), player(43, "WR", 14), player(44, "WR", 21, FLEX_SLOT)],
    5: [player(51, "QB", 18), player(52, "RB", 11), player(53, "WR", 9), player(54, "RB", 2, FLEX_SLOT)],
    6: [player(61, "QB", 12), player(62, "RB", 6), player(63, "WR", 7), player(64, "WR", 5, FLEX_SLOT)],
}


def box(home_id, home_score, away_id, away_score):
    return {
        "home_team_id": home_id, "home_score": home_score,
        "away_team_id": away_id, "away_score": away_score,
        "home_lineup": LINEUPS[home_id], "away_lineup": LINEUPS[away_id],
    }


# T2 beats T1 by 9, T3 beats T4 by 1, T5 beats T6 by 10.
BOX_SCORES = [box(1, 50.0, 2, 59.0), box(3, 58.0, 4, 57.0), box(5, 40.0, 6, 30.0)]


def run_week(monkeypatch, rule, box_scores=BOX_SCORES):
    monkeypatch.setattr(weekly_summary_v2, "get_box_scores", lambda *args, **kwargs: box_scores)
    league = SimpleNamespace(teams=TEAMS)
    payout_config = {"weekly_payouts": {"3": rule}}
    return weekly_summary_v2.compute_weekly_payout_week(
        league, league_id=1, year=2025, week=3, payout_config=payout_config, bucket=None, is_closed=False,
    )


# --- existing types -------------------------------------------------------

def test_highest_total_points(monkeypatch):
    assert run_week(monkeypatch, {"type": "highest_total_points"}) == {
        "week": 3,
        "payout_text": "Highest Scoring Team",
        "team": "Team 2",
        "owner": "BB",
        "points": 59,
        "players": ["P21", "P22", "P23", "P24"],
    }


def test_highest_total_points_ignores_bench_and_unscored(monkeypatch):
    winner = run_week(monkeypatch, {"type": "highest_total_points"}, [box(1, 50.0, 6, 30.0)])
    assert winner["team"] == "Team 1"
    assert winner["points"] == 50
    assert winner["players"] == ["P11", "P12", "P13", "P14"]


def test_top_player_overall(monkeypatch):
    winner = run_week(monkeypatch, {"type": "top_player_overall"})
    assert winner["payout_text"] == "Top Individual Player Score"
    assert (winner["owner"], winner["points"], winner["players"]) == ("BB", 30, ["P22"])


def test_top_slot_single_position(monkeypatch):
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"QB": 1}})
    assert winner["payout_text"] == "Top 1×QB Score"
    assert (winner["owner"], winner["points"], winner["players"]) == ("CC", 25, ["P31"])


def test_top_slot_needs_enough_players_at_position(monkeypatch):
    # Only T1, T4 and T6 start two WRs.
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"WR": 2}})
    assert winner["payout_text"] == "Top 2×WR Score"
    assert (winner["owner"], winner["points"], winner["players"]) == ("DD", 35, ["P44", "P43"])


def test_top_slot_multiple_positions_is_best_of_any_one(monkeypatch):
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"QB": 1, "RB": 1}})
    assert winner["payout_text"] == "Top 1×QB, 1×RB Score"
    assert (winner["owner"], winner["points"], winner["players"]) == ("BB", 30, ["P22"])


def test_top_slot_combo(monkeypatch):
    winner = run_week(monkeypatch, {"type": "top_slot_combo", "slots": {"QB": 1, "WR": 1}})
    assert winner["payout_text"] == "Top Combo: 1×QB, 1×WR"
    assert (winner["owner"], winner["points"], winner["players"]) == ("CC", 47, ["P31", "P33"])


def test_top_slot_combo_skips_teams_missing_a_slot(monkeypatch):
    assert run_week(monkeypatch, {"type": "top_slot_combo", "slots": {"QB": 1, "K": 1}}) is None


def test_label_overrides_default_text(monkeypatch):
    winner = run_week(monkeypatch, {"type": "highest_total_points", "label": "Big Dog"})
    assert winner["payout_text"] == "Big Dog"


def test_equal_points_pays_first_team_in_box_order(monkeypatch):
    # T1 and T6 both start a 20-point QB here; T6 is listed first (home).
    lineup_6 = [player(61, "QB", 20), player(62, "RB", 6)]
    boxes = [{**box(6, 26.0, 1, 50.0), "home_lineup": lineup_6}]
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"QB": 1}}, boxes)
    assert (winner["team"], winner["points"]) == ("Team 6", 20)


def test_week_without_a_rule_pays_nobody(monkeypatch):
    monkeypatch.setattr(weekly_summary_v2, "get_box_scores", lambda *args, **kwargs: BOX_SCORES)
    assert weekly_summary_v2.compute_weekly_payout_week(
        SimpleNamespace(teams=TEAMS), 1, 2025, 4, {"weekly_payouts": {"3": {"type": "highest_total_points"}}},
        bucket=None, is_closed=False,
    ) is None


def test_points_are_rounded_to_two_places(monkeypatch):
    lineups = dict(LINEUPS)
    lineups[1] = [player(11, "QB", 20.123), player(12, "RB", 10.004)]
    boxes = [{**box(1, 30.13, 6, 30.0), "home_lineup": lineups[1]}]
    assert run_week(monkeypatch, {"type": "highest_total_points"}, boxes)["points"] == 30.13


# --- matchup facts --------------------------------------------------------

def test_matchup_result():
    assert matchup_result(10, 9.99) == "W"
    assert matchup_result(9.99, 10) == "L"
    assert matchup_result(10, 10) == "T"


def test_build_team_weeks():
    team_lookup = {t.team_id: t for t in TEAMS}
    home, away = build_team_weeks([box(1, 50.0, 2, 59.0)], team_lookup)
    assert (home["team"], home["owner"], home["score"], home["opponent_score"]) == ("Team 1", "AA", 50.0, 59.0)
    assert (home["margin"], home["result"]) == (-9.0, "L")
    assert (away["margin"], away["result"]) == (9.0, "W")
    assert [p["name"] for p in home["starters"]] == ["P11", "P12", "P13", "P14"]


def test_build_team_weeks_skips_unknown_teams():
    team_lookup = {t.team_id: t for t in TEAMS if t.team_id != 2}
    assert [tw["team"] for tw in build_team_weeks([box(1, 50.0, 2, 59.0)], team_lookup)] == ["Team 1"]


# --- "result" modifier ----------------------------------------------------

def test_highest_scoring_losing_team(monkeypatch):
    winner = run_week(monkeypatch, {"type": "highest_total_points", "result": "loser"})
    assert winner == {
        "week": 3,
        "payout_text": "Highest Scoring Losing Team",
        "team": "Team 4",
        "owner": "DD",
        "points": 57,
        "players": ["P41", "P42", "P43", "P44"],
    }


def test_highest_scoring_winning_team(monkeypatch):
    # T4 loses to T5 here with the week's top score, so the plain rule and
    # the winner-only rule pay different teams.
    boxes = [box(4, 39.0, 5, 40.0)]
    assert run_week(monkeypatch, {"type": "highest_total_points"}, boxes)["team"] == "Team 4"
    winner = run_week(monkeypatch, {"type": "highest_total_points", "result": "winner"}, boxes)
    assert (winner["payout_text"], winner["team"]) == ("Highest Scoring Winning Team", "Team 5")


def test_result_modifier_works_with_other_types(monkeypatch):
    # Losers are T1 (QB 20), T4 (QB 10) and T6 (QB 12).
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"QB": 1}, "result": "loser"})
    assert winner["payout_text"] == "Top 1×QB Score (Losing Team)"
    assert (winner["owner"], winner["points"]) == ("AA", 20)


def test_tied_matchup_has_no_winner_or_loser(monkeypatch):
    tied = [box(1, 50.0, 2, 50.0)]
    assert run_week(monkeypatch, {"type": "highest_total_points", "result": "loser"}, tied) is None
    assert run_week(monkeypatch, {"type": "highest_total_points", "result": "winner"}, tied) is None
    assert run_week(monkeypatch, {"type": "highest_total_points"}, tied)["team"] == "Team 2"


def test_unknown_result_pays_nobody_and_warns(monkeypatch, capsys):
    assert run_week(monkeypatch, {"type": "highest_total_points", "result": "losers"}) is None
    assert "'losers'" in capsys.readouterr().out


# --- margin payouts -------------------------------------------------------

def test_biggest_blowout(monkeypatch):
    assert run_week(monkeypatch, {"type": "biggest_blowout"}) == {
        "week": 3,
        "payout_text": "Biggest Blow Out",
        "team": "Team 5",
        "owner": "EE",
        "points": 10.0,
        "players": [],
    }


def test_smallest_margin_of_victory(monkeypatch):
    winner = run_week(monkeypatch, {"type": "smallest_margin_of_victory"})
    assert winner["payout_text"] == "Smallest Margin of Victory"
    assert (winner["team"], winner["owner"], winner["points"]) == ("Team 3", "CC", 1.0)


def test_margin_is_rounded(monkeypatch):
    winner = run_week(monkeypatch, {"type": "smallest_margin_of_victory"}, [box(1, 101.34, 2, 101.12)])
    assert (winner["team"], winner["points"]) == ("Team 1", 0.22)


def test_margin_payouts_skip_tied_matchups(monkeypatch):
    boxes = [box(1, 50.0, 2, 50.0), box(5, 40.0, 6, 30.0)]
    assert run_week(monkeypatch, {"type": "smallest_margin_of_victory"}, boxes)["team"] == "Team 5"
    assert run_week(monkeypatch, {"type": "biggest_blowout"}, boxes[:1]) is None


# --- FLEX, unknown types --------------------------------------------------

def test_flex_means_the_flex_lineup_slot(monkeypatch):
    winner = run_week(monkeypatch, {"type": "top_slot", "slots": {"FLEX": 1}})
    assert winner["payout_text"] == "Top 1×FLEX Score"
    assert (winner["owner"], winner["points"], winner["players"]) == ("DD", 21, ["P44"])


def test_flex_in_a_combo_does_not_reuse_a_player(monkeypatch):
    # T4's best WR is its flex player, who then can't also fill FLEX, so T4
    # can't complete the combo. T1's best WR is in a WR slot, so it can.
    boxes = [box(1, 50.0, 4, 57.0)]
    winner = run_week(monkeypatch, {"type": "top_slot_combo", "slots": {"WR": 1, "FLEX": 1}}, boxes)
    assert (winner["team"], winner["players"]) == ("Team 1", ["P13", "P14"])


def test_unknown_type_pays_nobody_and_warns(capsys):
    assert pick_payout_winner(13, {"type": "margin_of_victory"}, []) is None
    assert "'margin_of_victory'" in capsys.readouterr().out


def test_top_player_overall_with_empty_lineup(monkeypatch):
    boxes = [{**box(1, 0.0, 2, 0.0), "home_lineup": [], "away_lineup": []}]
    assert run_week(monkeypatch, {"type": "top_player_overall"}, boxes) is None


# --- committed league configs ---------------------------------------------

CONFIG_PATHS = sorted(Path(__file__).resolve().parent.parent.glob("leagues/*/weekly_payouts_config.json"))


def _all_rules(config):
    yield from config.get("weekly_payouts", {}).items()
    for year_rules in config.get("weekly_payouts_by_year", {}).values():
        yield from year_rules.items()


@pytest.mark.parametrize("path", CONFIG_PATHS, ids=lambda p: p.parent.name)
def test_league_config_only_uses_known_rules(path):
    for week, rule in _all_rules(json.loads(path.read_text())):
        assert week.isdigit(), f"week {week!r}"
        assert rule["type"] in PAYOUT_TYPES, f"week {week}: unknown type {rule['type']!r}"
        if "result" in rule:
            assert rule["result"] in RESULT_FILTERS, f"week {week}: unknown result {rule['result']!r}"
        if rule["type"] in ("top_slot", "top_slot_combo"):
            assert rule.get("slots"), f"week {week}: {rule['type']} needs slots"
