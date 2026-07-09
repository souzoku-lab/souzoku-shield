from __future__ import annotations

from typing import Any, Callable

from .reducer import reduce_case


def evaluate_suite(rules: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """現在のreducerとReview surfaceを、否認インパクト付きで検証する。"""
    results = []
    for case in rules["expert"]["harness_cases"]:
        passed, detail = _run_check(case["check"], rules, state)
        potential_damage_yen = int(case.get("damage_yen", 0))
        monetary = potential_damage_yen > 0
        results.append(
            {
                "id": case["id"],
                "label": case["label"],
                "passed": passed,
                "damage_yen": 0 if passed else potential_damage_yen,
                "potential_damage_yen": potential_damage_yen,
                "monetary": monetary,
                "impact_label": case.get("impact_label", "否認インパクト" if monetary else "○×確認"),
                "detail": detail,
            }
        )
    total_damage = sum(item["damage_yen"] for item in results)
    return {
        "ok": all(item["passed"] for item in results),
        "total_damage_yen": total_damage,
        "impact_note": rules["expert"]["small_residence_denial_impact"]["summary"],
        "tax_formula_note": rules["expert"]["small_residence_denial_impact"]["tax_formula_note"],
        "results": results,
    }


def evaluate_bad_demo_fixture(rules: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """UIで赤を見せるための、誤実装サンプルに対するハーネス結果。"""
    actual = evaluate_suite(rules, state)
    bad_results = []
    for item in actual["results"]:
        clone = dict(item)
        clone["passed"] = False
        clone["damage_yen"] = clone["potential_damage_yen"] if clone["monetary"] else 0
        clone["detail"] = "誤実装サンプル: 分岐ミス、断定表現、または専門家確認漏れを検出"
        bad_results.append(clone)
    total_damage = sum(item["damage_yen"] for item in bad_results)
    return {
        "ok": False,
        "total_damage_yen": total_damage,
        "impact_note": actual["impact_note"],
        "tax_formula_note": actual["tax_formula_note"],
        "results": bad_results,
    }


def _run_check(check: str, rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    checks: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[bool, str]]] = {
        "spouse_must_not_require_continuation": _check_spouse_no_continuation,
        "co_resident_must_require_continuation": _check_co_resident_continuation,
        "house_lost_must_require_2018_docs": _check_house_lost_2018,
        "draft_must_not_use_definitive_phrases": _check_no_definitive,
        "overall_opinion_must_be_blank": _check_overall_blank,
        "spouse_must_prompt_secondary_inheritance": _check_spouse_secondary_prompt,
    }
    return checks[check](rules, state)


def _reduce_for(acquirer_type: str, rules: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    branch = dict(state)
    branch["acquirer_type"] = acquirer_type
    return reduce_case(branch, rules)


def _check_spouse_no_continuation(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    reduced = _reduce_for("spouse", rules, state)
    required = set(reduced["acquirer"]["required_document_ids"])
    forbidden = {"co_resident_continuation", "house_lost_no_home_docs"}
    passed = not (required & forbidden)
    return passed, "配偶者分岐の要求資料を確認"


def _check_co_resident_continuation(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    reduced = _reduce_for("co_resident", rules, state)
    required = set(reduced["acquirer"]["required_document_ids"])
    passed = "co_resident_continuation" in required
    return passed, "同居親族の継続確認資料を確認"


def _check_house_lost_2018(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    reduced = _reduce_for("house_lost", rules, state)
    required = set(reduced["acquirer"]["required_document_ids"])
    text = "\n".join(reduced["draft"]["section_3_land_review"])
    passed = "house_lost_no_home_docs" in required and "2018年改正" in text
    return passed, "家なき子の2018年改正条件を確認"


def _check_no_definitive(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    definitive = rules["expert"]["safety_rules"]["forbidden_definitive_phrases"]
    for acquirer_type in rules["expert"]["acquirer_types"]:
        reduced = _reduce_for(acquirer_type, rules, state)
        text = "\n".join(_flatten_draft(reduced["draft"]))
        for phrase in definitive:
            if phrase in text:
                return False, f"断定表現を検出: {phrase}"
    return True, "断定表現なし"


def _check_overall_blank(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    for acquirer_type in rules["expert"]["acquirer_types"]:
        reduced = _reduce_for(acquirer_type, rules, state)
        if reduced["draft"]["section_5_overall_opinion"] != "":
            return False, "総合所見が自動入力されています"
    return True, "総合所見は空欄"


def _check_spouse_secondary_prompt(rules: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    from ..agent_run import build_agent_run

    prompt = rules["expert"]["secondary_inheritance_prompt"]
    branch = dict(state)
    branch["acquirer_type"] = "spouse"
    _, run = build_agent_run(
        consultation_text="配偶者が取得します。遺産分割は進行中です。",
        state=branch,
        rules=rules,
        gemini_configured=False,
    )
    review = next(step for step in run["steps"] if step["id"] == "review")
    actions = review["actions"]
    passed = any(
        action["type"] == "ask_secondary_inheritance_review"
        and action["value"] == prompt["question"]
        and "56,000,000" not in action.get("why", "")
        and "5,600" not in action.get("why", "")
        for action in actions
    )
    return passed, "配偶者分岐のReviewで二次相続の問いを確認"


def _flatten_draft(draft: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for value in draft.values():
        if isinstance(value, list):
            lines.extend(str(item) for item in value)
        else:
            lines.append(str(value))
    return lines
