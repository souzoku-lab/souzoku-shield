from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any


STATUS_SCORE = {
    "not_requested": 0.0,
    "requested": 0.25,
    "received": 0.7,
    "verified": 1.0,
}

PRESENTED_STATUSES = {"received", "verified"}
VERIFIED_STATUS = "verified"


def reduce_case(state: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """案件状態を、書面添付ドラフト・不足・次の一手へ決定的に畳み込む。"""
    expert = rules["expert"]
    template = rules["template"]
    heirs = normalize_heirs(state.get("heirs", []))
    home_acquirer_id = _valid_home_acquirer_id(state.get("home_acquirer_id"), heirs)
    home_acquirer = _heir_by_id(heirs, home_acquirer_id)
    acquirer_type = _valid_acquirer(state.get("acquirer_type"), expert)
    acquirer = expert["acquirer_types"][acquirer_type]
    partition_status = _valid_partition(state.get("partition_status"), expert)
    documents = _normalize_documents(state.get("documents", {}), expert)
    eligibility_alerts = _eligibility_alerts(acquirer_type, heirs, home_acquirer, expert)
    secondary_inheritance_alert = _secondary_inheritance_alert(acquirer_type, expert)

    required_ids = list(acquirer["required_document_ids"])
    missing = _missing_documents(required_ids, documents, expert)
    presented_docs = _presented_documents(documents, expert)
    completion = _completion(required_ids, documents, partition_status, expert)
    draft = _build_draft(
        acquirer_type=acquirer_type,
        acquirer=acquirer,
        partition_status=partition_status,
        documents=documents,
        presented_docs=presented_docs,
        home_acquirer=home_acquirer,
        eligibility_alerts=eligibility_alerts,
        template=template,
        expert=expert,
    )
    next_actions = _next_actions(missing, partition_status, expert, eligibility_alerts, secondary_inheritance_alert)

    case = copy.deepcopy(expert["demo_case"])
    if state.get("case_title"):
        case["title"] = str(state["case_title"])

    return {
        "case": case,
        "state": {
            "acquirer_type": acquirer_type,
            "home_acquirer_id": home_acquirer_id,
            "heirs": heirs,
            "partition_status": partition_status,
            "documents": documents,
        },
        "home_acquirer": copy.deepcopy(home_acquirer),
        "acquirer": {
            "id": acquirer_type,
            "label": acquirer["label"],
            "summary": acquirer["summary"],
            "requirement_checks": list(acquirer["requirement_checks"]),
            "required_document_ids": required_ids,
        },
        "eligibility_alerts": eligibility_alerts,
        "secondary_inheritance_alert": secondary_inheritance_alert,
        "documents": _documents_with_status(documents, expert),
        "presented_documents": presented_docs,
        "missing_documents": missing,
        "draft": draft,
        "completion": completion,
        "next_actions": next_actions,
        "responsibility_boundary": "要件確認と資料整理は自動下書き、可否判断と総合所見は税理士が行います。",
    }


def build_counterfactuals(state: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """同じ案件状態で取得者だけを切り替え、分岐差分を返す。"""
    expert = rules["expert"]
    current = _valid_acquirer(state.get("acquirer_type"), expert)
    output: list[dict[str, Any]] = []
    for acquirer_type in expert["acquirer_types"]:
        if acquirer_type == current:
            continue
        branch_state = copy.deepcopy(state)
        branch_state["acquirer_type"] = acquirer_type
        selected_id = select_home_acquirer_id_for_type(branch_state, acquirer_type)
        if selected_id:
            branch_state["home_acquirer_id"] = selected_id
        reduced = reduce_case(branch_state, rules)
        output.append(
            {
                "from": current,
                "to": acquirer_type,
                "label": reduced["acquirer"]["label"],
                "required_document_ids": reduced["acquirer"]["required_document_ids"],
                "missing_document_labels": [item["label"] for item in reduced["missing_documents"]],
                "land_review": reduced["draft"]["section_3_land_review"],
                "completion_percent": reduced["completion"]["percent"],
                "eligibility_alerts": reduced["eligibility_alerts"],
                "secondary_inheritance_alert": reduced["secondary_inheritance_alert"],
            }
        )
    return output


def apply_definitive_filter(text: str, rules: dict[str, Any]) -> str:
    """相談者向け出力の断定表現を安全な論点表現へ寄せる。"""
    safety = rules["expert"]["safety_rules"]
    replacement = safety["safe_replacement"]
    filtered = unicodedata.normalize("NFKC", text)
    for phrase in safety["forbidden_definitive_phrases"]:
        pattern = r"\s*".join(re.escape(char) for char in unicodedata.normalize("NFKC", phrase))
        filtered = filtered.replace(phrase, replacement)
        filtered = re.sub(pattern, replacement, filtered)
    compact_filtered = _compact_for_safety(filtered)
    if any(_compact_for_safety(phrase) in compact_filtered for phrase in safety["forbidden_definitive_phrases"]):
        return replacement
    return filtered


def _compact_for_safety(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", normalized)


def _valid_acquirer(value: Any, expert: dict[str, Any]) -> str:
    candidate = str(value or expert["demo_case"]["default_acquirer_type"])
    if candidate not in expert["acquirer_types"]:
        return expert["demo_case"]["default_acquirer_type"]
    return candidate


def normalize_heirs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        heir_id = str(item.get("id", "")).strip()
        if not heir_id or heir_id in seen:
            continue
        relation = str(item.get("relation", "child"))
        if relation not in {"spouse", "child"}:
            relation = "child"
        normalized.append(
            {
                "id": heir_id,
                "name": str(item.get("name") or heir_id).strip(),
                "relation": relation,
                "relation_label": "配偶者" if relation == "spouse" else "子",
                "co_resident": _boolish(item.get("co_resident", False)),
            }
        )
        seen.add(heir_id)
    return normalized


def acquirer_type_for_heir(heir: dict[str, Any] | None) -> str:
    if not heir:
        return "co_resident"
    if heir.get("relation") == "spouse":
        return "spouse"
    if heir.get("co_resident"):
        return "co_resident"
    return "house_lost"


def select_home_acquirer_id_for_type(state: dict[str, Any], acquirer_type: str) -> str:
    heirs = normalize_heirs(state.get("heirs", []))
    for heir in heirs:
        if acquirer_type_for_heir(heir) == acquirer_type:
            return str(heir["id"])
    if heirs:
        return str(heirs[0]["id"])
    return ""


def _valid_home_acquirer_id(value: Any, heirs: list[dict[str, Any]]) -> str:
    candidate = str(value or "")
    if any(heir["id"] == candidate for heir in heirs):
        return candidate
    if heirs:
        return str(heirs[0]["id"])
    return ""


def _heir_by_id(heirs: list[dict[str, Any]], heir_id: str) -> dict[str, Any] | None:
    for heir in heirs:
        if heir["id"] == heir_id:
            return heir
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "同居"}


def _eligibility_alerts(
    acquirer_type: str,
    heirs: list[dict[str, Any]],
    home_acquirer: dict[str, Any] | None,
    expert: dict[str, Any],
) -> list[dict[str, Any]]:
    if acquirer_type != "house_lost" or not home_acquirer or home_acquirer.get("co_resident"):
        return []
    co_resident_heirs = [
        heir
        for heir in heirs
        if heir["id"] != home_acquirer["id"]
        and heir.get("relation") != "spouse"
        and heir.get("co_resident")
    ]
    spouse_heirs = [
        heir
        for heir in heirs
        if heir["id"] != home_acquirer["id"] and heir.get("relation") == "spouse"
    ]
    if not co_resident_heirs and not spouse_heirs:
        return []

    messages: list[str] = []
    if co_resident_heirs:
        names = "・".join(heir["name"] for heir in co_resident_heirs)
        messages.append(
            f"同居親族（{names}）がいるため、別居親族（{home_acquirer['name']}）が自宅を取得する場合は"
            "家なき子要件の対象外となる可能性があります。"
        )
    if spouse_heirs:
        spouse_names = "・".join(heir["name"] for heir in spouse_heirs)
        messages.append(
            f"被相続人に配偶者（{spouse_names}）がいるため、別居親族（{home_acquirer['name']}）が自宅を取得する場合は"
            "家なき子要件の対象外となる可能性があります。"
        )

    impact = int(expert["small_residence_denial_impact"]["lost_reduction_yen"])
    return [
        {
            "id": _house_lost_alert_id(co_resident_heirs, spouse_heirs),
            "severity": "red",
            "title": "小規模宅地の特例 適用不可アラート",
            "message": " ".join(messages),
            "impact_yen": impact,
        }
    ]


def _house_lost_alert_id(co_resident_heirs: list[dict[str, Any]], spouse_heirs: list[dict[str, Any]]) -> str:
    if co_resident_heirs and spouse_heirs:
        return "co_resident_and_spouse_block_house_lost_acquirer"
    if spouse_heirs:
        return "spouse_blocks_house_lost_acquirer"
    return "co_resident_heir_blocks_nonresident_acquirer"


def _secondary_inheritance_alert(acquirer_type: str, expert: dict[str, Any]) -> dict[str, Any] | None:
    prompt = expert.get("secondary_inheritance_prompt", {})
    if acquirer_type != prompt.get("trigger_acquirer_type"):
        return None
    return {
        "id": "spouse_secondary_inheritance_review",
        "severity": "amber",
        "title": _safe_expert_text(str(prompt.get("question", "")), expert),
        "message": _safe_expert_text(str(prompt.get("why", "")), expert),
        "guardrail": _safe_expert_text(str(prompt.get("guardrail", "")), expert),
        "provenance": dict(prompt.get("provenance", {})),
    }


def _safe_expert_text(text: str, expert: dict[str, Any]) -> str:
    safety = expert["safety_rules"]
    compact = _compact_for_safety(text)
    if any(_compact_for_safety(phrase) in compact for phrase in safety["forbidden_definitive_phrases"]):
        return safety["safe_replacement"]
    return text


def _valid_partition(value: Any, expert: dict[str, Any]) -> str:
    candidate = str(value or expert["demo_case"]["default_partition_status"])
    if candidate not in expert["partition_statuses"]:
        return expert["demo_case"]["default_partition_status"]
    return candidate


def _normalize_documents(raw: dict[str, Any], expert: dict[str, Any]) -> dict[str, str]:
    allowed = set(expert["document_statuses"])
    defaults = {doc["id"]: doc.get("default_status", "not_requested") for doc in expert["documents"]}
    normalized: dict[str, str] = {}
    for doc_id, default_status in defaults.items():
        status = str(raw.get(doc_id, default_status))
        normalized[doc_id] = status if status in allowed else default_status
    return normalized


def _documents_with_status(documents: dict[str, str], expert: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": doc["id"],
            "label": doc["label"],
            "reason": doc["reason"],
            "status": documents[doc["id"]],
        }
        for doc in expert["documents"]
    ]


def _missing_documents(required_ids: list[str], documents: dict[str, str], expert: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {doc["id"]: doc for doc in expert["documents"]}
    missing = []
    for doc_id in required_ids:
        if documents.get(doc_id) != VERIFIED_STATUS:
            doc = by_id[doc_id]
            missing.append(
                {
                    "id": doc_id,
                    "label": doc["label"],
                    "reason": doc["reason"],
                    "status": documents.get(doc_id, "not_requested"),
                }
            )
    return missing


def _presented_documents(documents: dict[str, str], expert: dict[str, Any]) -> list[dict[str, str]]:
    presented = []
    for doc in expert["documents"]:
        status = documents[doc["id"]]
        if status in PRESENTED_STATUSES:
            presented.append({"id": doc["id"], "label": doc["label"], "status": status})
    return presented


def _completion(
    required_ids: list[str],
    documents: dict[str, str],
    partition_status: str,
    expert: dict[str, Any],
) -> dict[str, Any]:
    if not required_ids:
        doc_score = 0.0
    else:
        doc_score = sum(STATUS_SCORE[documents[doc_id]] for doc_id in required_ids) / len(required_ids)
    partition_score = expert["partition_statuses"][partition_status]["score"]
    combined = (doc_score * 0.78) + (partition_score * 0.22)
    percent = int(round(combined * 100))
    if percent >= 85:
        band = "税理士確認前の下書きがかなり整っています"
    elif percent >= 55:
        band = "主要資料を確認中です"
    else:
        band = "初期収集が必要です"
    return {
        "percent": percent,
        "document_score": round(doc_score, 3),
        "partition_score": partition_score,
        "label": band,
    }


def _build_draft(
    *,
    acquirer_type: str,
    acquirer: dict[str, Any],
    partition_status: str,
    documents: dict[str, str],
    presented_docs: list[dict[str, str]],
    home_acquirer: dict[str, Any] | None,
    eligibility_alerts: list[dict[str, Any]],
    template: dict[str, Any],
    expert: dict[str, Any],
) -> dict[str, Any]:
    section_1 = [doc["label"] for doc in presented_docs] or ["提示資料は未収集です。"]
    section_2 = _template_section(template, "section_2_prepared_documents").get("fixed_lines", [])
    partition_line = expert["partition_statuses"][partition_status]["draft_line"]
    section_3_lines = [
        f"小規模宅地等の特例について、取得者を「{acquirer['label']}」とする前提で要件を確認中。",
        "土地の名義、形状、面積、固定資産情報は収集資料と照合中。",
        partition_line,
    ]
    if home_acquirer:
        section_3_lines.append(
            f"自宅取得者は「{home_acquirer['name']}」（{_heir_status_label(home_acquirer)}）として登録済み。"
        )
    section_3_lines.extend(acquirer["requirement_checks"])
    section_3_lines.extend(_evidence_lines(acquirer["required_document_ids"], documents, expert))

    if acquirer_type == "spouse":
        section_3_lines.append("配偶者取得のため、同居親族向けの居住・保有継続資料は要求リストから外しています。")
    elif acquirer_type == "co_resident":
        section_3_lines.append("同居親族取得のため、申告期限までの居住継続と保有継続を重点確認中。")
    elif acquirer_type == "house_lost":
        section_3_lines.append("家なき子取得のため、2018年改正後の居住履歴と家屋所有関係を重点確認中。")

    for alert in eligibility_alerts:
        section_3_lines.append(f"{alert['title']}: {alert['message']}")

    section_3_lines.append("可否判断、評価額、限度面積、総合所見は税理士確認欄として未確定。")

    draft = {
        "form_name": template["form_name"],
        "section_1_presented_documents": section_1,
        "section_2_prepared_documents": section_2,
        "section_3_land_review": [apply_definitive_filter(line, {"expert": expert}) for line in section_3_lines],
        "section_5_overall_opinion": "",
        "status_label": "確認中",
    }
    return draft


def _heir_status_label(heir: dict[str, Any]) -> str:
    if heir.get("relation") == "spouse":
        return "配偶者"
    return f"{heir.get('relation_label', '相続人')}・{'同居' if heir.get('co_resident') else '別居'}"


def _evidence_lines(required_ids: list[str], documents: dict[str, str], expert: dict[str, Any]) -> list[str]:
    by_id = {doc["id"]: doc for doc in expert["documents"]}
    lines = []
    for doc_id in required_ids:
        doc = by_id[doc_id]
        status = documents[doc_id]
        if status == "verified":
            lines.append(f"確認済: {doc['label']}（{doc['reason']}）")
        elif status == "received":
            lines.append(f"受領済・内容確認待ち: {doc['label']}（{doc['reason']}）")
        elif status == "requested":
            lines.append(f"依頼済・未受領: {doc['label']}（{doc['reason']}）")
        else:
            lines.append(f"未依頼: {doc['label']}（{doc['reason']}）")
    return lines


def _next_actions(
    missing: list[dict[str, Any]],
    partition_status: str,
    expert: dict[str, Any],
    eligibility_alerts: list[dict[str, Any]],
    secondary_inheritance_alert: dict[str, Any] | None,
) -> list[str]:
    actions = []
    for alert in eligibility_alerts:
        actions.append(f"{alert['title']}を確認し、自宅取得者または分割方針を見直す")
    if secondary_inheritance_alert:
        actions.append("配偶者取得時の二次相続論点を税理士が確認する")
    for doc in missing[:4]:
        if doc["status"] == "not_requested":
            actions.append(f"{doc['label']}を依頼する")
        elif doc["status"] == "requested":
            actions.append(f"{doc['label']}の受領予定日を確認する")
        else:
            actions.append(f"{doc['label']}を税理士確認済みにする")
    if partition_status == "in_progress":
        actions.append("遺産分割協議の確定予定または分割見込書の要否を確認する")
    elif partition_status == "expected":
        actions.append("分割見込書の内容と申告後の管理予定を確認する")
    if not actions:
        actions.append("総合所見と最終判断を税理士が記入する")
    return actions


def _template_section(template: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in template["sections"]:
        if section["id"] == section_id:
            return section
    return {}
