from __future__ import annotations

import copy

from app.engine.reducer import build_counterfactuals, reduce_case
from app.rules_loader import default_case_state, load_rules


def test_reducer_is_deterministic() -> None:
    rules = load_rules()
    state = default_case_state()

    first = reduce_case(copy.deepcopy(state), rules)
    second = reduce_case(copy.deepcopy(state), rules)

    assert first == second


def test_spouse_branch_does_not_require_continuation_docs() -> None:
    rules = load_rules()
    state = default_case_state()
    state["acquirer_type"] = "spouse"

    reduced = reduce_case(state, rules)
    required = set(reduced["acquirer"]["required_document_ids"])

    assert "co_resident_continuation" not in required
    assert "house_lost_no_home_docs" not in required


def test_spouse_branch_surfaces_secondary_inheritance_alert() -> None:
    rules = load_rules()
    state = default_case_state()
    state["acquirer_type"] = "spouse"

    reduced = reduce_case(state, rules)

    alert = reduced["secondary_inheritance_alert"]
    assert alert["id"] == "spouse_secondary_inheritance_review"
    assert alert["title"] == "二次相続の検討はされましたか？"
    assert any("二次相続論点" in action for action in reduced["next_actions"])


def test_co_resident_branch_requires_continuation_docs() -> None:
    rules = load_rules()
    state = default_case_state()
    state["acquirer_type"] = "co_resident"

    reduced = reduce_case(state, rules)
    required = set(reduced["acquirer"]["required_document_ids"])

    assert "co_resident_continuation" in required
    assert any("居住継続" in line for line in reduced["draft"]["section_3_land_review"])
    assert reduced["secondary_inheritance_alert"] is None


def test_house_lost_branch_uses_2018_conditions() -> None:
    rules = load_rules()
    state = default_case_state()
    state["acquirer_type"] = "house_lost"

    reduced = reduce_case(state, rules)
    required = set(reduced["acquirer"]["required_document_ids"])
    text = "\n".join(reduced["draft"]["section_3_land_review"])

    assert "house_lost_no_home_docs" in required
    assert "2018年改正" in text


def test_nonresident_acquirer_alerts_when_co_resident_heir_exists() -> None:
    rules = load_rules()
    state = default_case_state()
    state["home_acquirer_id"] = "second_son"
    state["acquirer_type"] = "house_lost"

    reduced = reduce_case(state, rules)

    assert reduced["home_acquirer"]["name"] == "次男"
    assert reduced["eligibility_alerts"]
    assert reduced["eligibility_alerts"][0]["impact_yen"] == 56000000
    assert any("適用不可アラート" in action for action in reduced["next_actions"])


def test_house_lost_alerts_when_spouse_exists_even_without_co_resident_heir() -> None:
    rules = load_rules()
    state = default_case_state()
    state["home_acquirer_id"] = "second_son"
    state["acquirer_type"] = "house_lost"
    for heir in state["heirs"]:
        if heir["relation"] == "child":
            heir["co_resident"] = False

    reduced = reduce_case(state, rules)

    alerts = reduced["eligibility_alerts"]
    assert len(alerts) == 1
    assert alerts[0]["id"] == "spouse_blocks_house_lost_acquirer"
    assert "被相続人に配偶者（母）" in alerts[0]["message"]
    assert "可能性があります" in alerts[0]["message"]
    assert alerts[0]["impact_yen"] == 56000000


def test_counterfactual_branches_change_documents_and_draft() -> None:
    rules = load_rules()
    state = default_case_state()
    state["acquirer_type"] = "spouse"

    branches = build_counterfactuals(state, rules)
    house_lost = next(branch for branch in branches if branch["to"] == "house_lost")

    assert "house_lost_no_home_docs" in house_lost["required_document_ids"]
    assert house_lost["land_review"] != reduce_case(state, rules)["draft"]["section_3_land_review"]


def test_overall_opinion_stays_blank() -> None:
    rules = load_rules()
    state = default_case_state()

    for acquirer_type in rules["expert"]["acquirer_types"]:
        state["acquirer_type"] = acquirer_type
        reduced = reduce_case(state, rules)
        assert reduced["draft"]["section_5_overall_opinion"] == ""


def test_draft_avoids_definitive_phrases() -> None:
    rules = load_rules()
    state = default_case_state()
    forbidden = rules["expert"]["safety_rules"]["forbidden_definitive_phrases"]

    for acquirer_type in rules["expert"]["acquirer_types"]:
        state["acquirer_type"] = acquirer_type
        reduced = reduce_case(state, rules)
        text = "\n".join(reduced["draft"]["section_3_land_review"])
        assert not any(phrase in text for phrase in forbidden)
