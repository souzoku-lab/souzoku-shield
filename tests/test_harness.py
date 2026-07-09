from __future__ import annotations

from app.engine.harness import evaluate_bad_demo_fixture, evaluate_suite
from app.rules_loader import default_case_state, load_rules


def test_harness_current_engine_is_green() -> None:
    rules = load_rules()
    result = evaluate_suite(rules, default_case_state())

    assert result["ok"] is True
    assert result["total_damage_yen"] == 0
    assert result["impact_note"] == "課税価格 +5,600万円（失われる評価減）"
    assert result["tax_formula_note"] == "本税＝評価減×相続税率（税率はケースにより変動）"
    assert all(item["passed"] for item in result["results"])
    monetary = [item for item in result["results"] if item["monetary"]]
    assert {item["id"] for item in monetary} == {
        "co_resident_requires_continuation",
        "house_lost_2018_conditions",
    }
    assert all(item["potential_damage_yen"] == 56000000 for item in monetary)
    assert any(item["id"] == "spouse_secondary_inheritance_prompt" for item in result["results"])


def test_harness_bad_fixture_is_red_with_damage_total() -> None:
    rules = load_rules()
    result = evaluate_bad_demo_fixture(rules, default_case_state())

    assert result["ok"] is False
    assert result["total_damage_yen"] == 112000000
    assert any(not item["passed"] for item in result["results"])
    assert all(item["damage_yen"] == 0 for item in result["results"] if not item["monetary"])
