from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx


DEFAULT_TEXT = (
    "父が亡くなりました。母と長男は父の家で暮らしていました。自宅は次男が引き継ぐ予定です。"
    "次男は就職後ずっと会社近くの賃貸マンションで生活しており、住宅を購入したことはありません。"
)

ROUTE_CASES = [
    ("spouse_1", "父の死後、子どもたちは預金を受け取り、母が家を引き継ぐ予定です。", "spouse"),
    ("spouse_2", "亡くなった方の奥様が、これまで暮らした居宅敷地を取得する分割案です。", "spouse"),
    ("spouse_3", "夫の遺した自宅は妻が受け継ぎ、子どもは他の財産を取得します。", "spouse"),
    ("co_resident_1", "亡くなるまで父と同じ屋根の下で生活した長女が、実家を引き継ぎます。", "co_resident"),
    ("co_resident_2", "母の介護をしながら一緒に暮らしていた長男が、その家を受け継ぐ予定です。", "co_resident"),
    ("co_resident_3", "故人と生活を共にしてきた次男が、居宅敷地を取得する話になっています。", "co_resident"),
    ("house_lost_1", DEFAULT_TEXT, "house_lost"),
    (
        "house_lost_2",
        "長女は結婚後ずっと借りているマンションで生活し、自分名義の住宅はありません。父の家を受け継ぐ予定です。",
        "house_lost",
    ),
    (
        "house_lost_3",
        "実家を離れて社宅で暮らす次男が父の居宅敷地を取得します。次男夫婦は住宅を所有していません。",
        "house_lost",
    ),
]

CLARIFICATION_CASES = [
    {
        "id": "clarify_residence",
        "input": "父が亡くなり、次男が実家を引き継ぐ話になっています。必要な確認を進めてください。",
        "missing_fact": "residence_and_home_ownership",
        "answer": "次男は父とは別に賃貸住宅で暮らし、本人も配偶者も住宅を所有していません。",
        "expected_route": "house_lost",
    },
    {
        "id": "clarify_acquirer",
        "input": "父が亡くなりました。母と長男が父の家で暮らしています。自宅の確認を進めてください。",
        "missing_fact": "home_acquirer",
        "answer": "自宅を取得する予定なのは母です。",
        "expected_route": "spouse",
    },
]


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": "souzoku-shield-production-evidence/1.0"},
    )


def _run_case(base_url: str, case_id: str, text: str, expected: str) -> dict[str, Any]:
    with _client(base_url) as client:
        client.post("/api/demo/seed").raise_for_status()
        client.post("/api/demo/clear-heirs").raise_for_status()
        response = client.post("/api/run", json={"text": text})
        response.raise_for_status()
        payload = response.json()

    run = payload["run"]
    gemini = run["gemini"]
    actual = payload["case"]["analysis"]["acquirer"]["id"]
    gemini_route = str(gemini.get("arguments", {}).get("acquirer_type", ""))
    state = payload["case"]["state"]
    cards_inferred = bool(state.get("heirs") and state.get("home_acquirer_id"))
    fallback = not bool(gemini.get("used"))
    passed = (
        run.get("mode") == "gemini_function_calling"
        and gemini.get("tool_name") == "select_taker_branch"
        and gemini_route == expected
        and actual == expected
        and cards_inferred
        and not fallback
    )
    return {
        "id": case_id,
        "input": text,
        "expected_route": expected,
        "gemini_route": gemini_route,
        "actual_route": actual,
        "cards_inferred": cards_inferred,
        "mode": run.get("mode"),
        "function": gemini.get("tool_name"),
        "fallback": fallback,
        "fallback_reason": gemini.get("fallback_reason", ""),
        "latency_ms": gemini.get("latency_ms"),
        "passed": passed,
    }


def _run_clarification_case(base_url: str, case: dict[str, str]) -> dict[str, Any]:
    with _client(base_url) as client:
        client.post("/api/demo/seed").raise_for_status()
        before_response = client.post("/api/demo/clear-heirs")
        before_response.raise_for_status()
        before = before_response.json()["case"]
        first_response = client.post("/api/run", json={"text": case["input"]})
        first_response.raise_for_status()
        first = first_response.json()
        blocked_approval = client.post("/api/approve")
        blocked_word = client.get("/api/export/word")
        resumed_response = client.post("/api/run/continue", json={"answer": case["answer"]})
        resumed_response.raise_for_status()
        resumed = resumed_response.json()

    first_run = first["run"]
    resumed_run = resumed["run"]
    first_state_unchanged = first["case"]["state"] == before["state"]
    history_tools = [event.get("tool") for event in resumed_run.get("decision_history", [])]
    actual_route = resumed["case"]["analysis"]["acquirer"]["id"]
    passed = (
        first_run.get("status") == "AWAITING_CLARIFICATION"
        and first_run.get("state_mutated") is False
        and first_run.get("gemini", {}).get("used") is True
        and first_run.get("gemini", {}).get("tool_name") == "request_clarification"
        and first_run.get("clarification", {}).get("missing_fact") == case["missing_fact"]
        and first_state_unchanged
        and first["case"]["approval"]["review_ready"] is False
        and blocked_approval.status_code == 409
        and blocked_word.status_code == 409
        and resumed_run.get("status") == "REVIEW_PENDING"
        and resumed_run.get("gemini", {}).get("used") is True
        and resumed_run.get("gemini", {}).get("tool_name") == "select_taker_branch"
        and actual_route == case["expected_route"]
        and history_tools == ["request_clarification", "select_taker_branch"]
        and resumed_run.get("continuation_of") == first_run.get("id")
        and resumed["case"]["approval"]["review_ready"] is True
    )
    return {
        "id": case["id"],
        "input": case["input"],
        "expected_missing_fact": case["missing_fact"],
        "actual_missing_fact": first_run.get("clarification", {}).get("missing_fact", ""),
        "first_tool": first_run.get("gemini", {}).get("tool_name", ""),
        "first_state_unchanged": first_state_unchanged,
        "approval_blocked_status": blocked_approval.status_code,
        "word_blocked_status": blocked_word.status_code,
        "answer": case["answer"],
        "expected_route": case["expected_route"],
        "actual_route": actual_route,
        "resume_tool": resumed_run.get("gemini", {}).get("tool_name", ""),
        "decision_history": history_tools,
        "first_latency_ms": first_run.get("gemini", {}).get("latency_ms"),
        "resume_latency_ms": resumed_run.get("gemini", {}).get("latency_ms"),
        "fallback": not (
            first_run.get("gemini", {}).get("used") and resumed_run.get("gemini", {}).get("used")
        ),
        "passed": passed,
    }


def _docx_is_valid(content: bytes) -> bool:
    stream = io.BytesIO(content)
    if not zipfile.is_zipfile(stream):
        return False
    stream.seek(0)
    with zipfile.ZipFile(stream) as package:
        names = set(package.namelist())
    return {"[Content_Types].xml", "word/document.xml"}.issubset(names)


def _public_e2e(base_url: str, *, word_output: Path | None = None) -> dict[str, Any]:
    is_https = base_url.lower().startswith("https://")
    with _client(base_url) as judge_a, _client(base_url) as judge_b:
        first_a = judge_a.get("/api/case")
        first_b = judge_b.get("/api/case")
        first_a.raise_for_status()
        first_b.raise_for_status()
        cookie = first_a.headers.get("set-cookie", "").lower()

        judge_a.patch("/api/case", json={"home_acquirer_id": "second_son"}).raise_for_status()
        state_b_before = judge_b.get("/api/case").json()
        isolated_before = state_b_before["state"]["home_acquirer_id"] == "eldest_son"

        judge_b.patch(
            "/api/case",
            json={"home_acquirer_id": "mother", "partition_status": "finalized"},
        ).raise_for_status()
        preserved_b_state = judge_b.get("/api/case").json()["state"]

        run_response = judge_a.post("/api/run", json={"text": DEFAULT_TEXT})
        run_response.raise_for_status()
        before_word = judge_a.get("/api/export/word")
        approval = judge_a.post("/api/approve")
        word = judge_a.get("/api/export/word")
        if word_output and word.status_code == 200:
            word_output.write_bytes(word.content)

        judge_a.post("/api/demo/seed").raise_for_status()
        state_b_after = judge_b.get("/api/case").json()
        isolated_after = state_b_after["state"] == preserved_b_state

    homepage = httpx.get(base_url.rstrip("/") + "/", follow_redirects=True, timeout=30.0)
    homepage.raise_for_status()
    required_markers = ["Souzoku Shield — 相続の盾", "公開用の架空デモです。", "60秒デモを初期化"]
    marker_checks = {marker: marker in homepage.text for marker in required_markers}
    cookie_actual = {
        "httponly": "httponly" in cookie,
        "secure": "secure" in cookie,
        "samesite_lax": "samesite=lax" in cookie,
    }
    cookie_passed = (
        cookie_actual["httponly"]
        and cookie_actual["samesite_lax"]
        and (cookie_actual["secure"] if is_https else True)
    )
    checks = {
        "public_markers": all(marker_checks.values()),
        "session_isolation_before": isolated_before,
        "session_isolation_after_reset": isolated_after,
        "cookie_attributes": cookie_passed,
        "word_blocked_before_approval": before_word.status_code == 409,
        "approval_succeeded": approval.status_code == 200,
        "word_download_succeeded": word.status_code == 200,
        "word_package_valid": word.status_code == 200 and _docx_is_valid(word.content),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "cookie": cookie_actual,
        "markers": marker_checks,
        "word_content_type": word.headers.get("content-type", ""),
    }


def collect_evidence(
    base_url: str,
    *,
    skip_gemini: bool = False,
    word_output: Path | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    health = httpx.get(base_url.rstrip("/") + "/api/health", timeout=30.0)
    health.raise_for_status()
    health_payload = health.json()
    version_match = not expected_version or health_payload.get("version") == expected_version
    result: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "health": health_payload,
        "expected_version": expected_version or "",
        "version_match": version_match,
        "default_five": [],
        "route_nine": [],
        "clarification_cases": [],
    }
    if not skip_gemini:
        result["default_five"] = [
            _run_case(base_url, f"default_{index}", DEFAULT_TEXT, "house_lost")
            for index in range(1, 6)
        ]
        result["route_nine"] = [
            _run_case(base_url, case_id, text, expected)
            for case_id, text, expected in ROUTE_CASES
        ]
        result["clarification_cases"] = [
            _run_clarification_case(base_url, case) for case in CLARIFICATION_CASES
        ]
    result["public_e2e"] = _public_e2e(base_url, word_output=word_output)
    gemini_rows = result["default_five"] + result["route_nine"]
    result["ok"] = (
        version_match
        and result["public_e2e"]["passed"]
        and all(row["passed"] for row in gemini_rows)
        and all(row["passed"] for row in result["clarification_cases"])
    )
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Souzoku Shieldの公開Gemini・E2E証拠を収集します。")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--word-output", type=Path)
    parser.add_argument("--expected-version", help="/api/health のversionと一致させる公開main SHA")
    parser.add_argument("--skip-gemini", action="store_true")
    args = parser.parse_args()

    try:
        evidence = collect_evidence(
            args.base_url,
            skip_gemini=args.skip_gemini,
            word_output=args.word_output,
            expected_version=args.expected_version,
        )
    except (httpx.HTTPError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
        return 1

    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
