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
    "父が亡くなり、母と長男は同居していました。持ち家のない別居の次男が、"
    "自宅を相続する予定です。次男は賃貸住宅に住んでいます。"
)

ROUTE_CASES = [
    ("spouse_1", "父が亡くなり、母が自宅を相続する予定です。", "spouse"),
    ("spouse_2", "被相続人の配偶者が居宅敷地を取得する分割案です。", "spouse"),
    ("spouse_3", "妻が自宅を相続し、子どもは預金を取得する予定です。", "spouse"),
    ("co_resident_1", "長男が被相続人と同居しており、自宅を相続する予定です。", "co_resident"),
    ("co_resident_2", "同居していた長女が居宅敷地を取得します。", "co_resident"),
    ("co_resident_3", "被相続人と暮らしていた次男が自宅を引き継ぐ予定です。", "co_resident"),
    ("house_lost_1", DEFAULT_TEXT, "house_lost"),
    ("house_lost_2", "別居して賃貸住宅に住む長女が自宅を相続する予定です。", "house_lost"),
    ("house_lost_3", "持ち家のない非同居の次男が居宅敷地を取得します。", "house_lost"),
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
        response = client.post("/api/run", json={"text": text})
        response.raise_for_status()
        payload = response.json()

    run = payload["run"]
    gemini = run["gemini"]
    actual = payload["case"]["analysis"]["acquirer"]["id"]
    fallback = not bool(gemini.get("used"))
    passed = (
        run.get("mode") == "gemini_function_calling"
        and gemini.get("tool_name") == "select_taker_branch"
        and actual == expected
        and not fallback
    )
    return {
        "id": case_id,
        "input": text,
        "expected_route": expected,
        "actual_route": actual,
        "mode": run.get("mode"),
        "function": gemini.get("tool_name"),
        "fallback": fallback,
        "fallback_reason": gemini.get("fallback_reason", ""),
        "latency_ms": gemini.get("latency_ms"),
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

        run_response = judge_a.post("/api/run", json={"text": DEFAULT_TEXT})
        run_response.raise_for_status()
        before_word = judge_a.get("/api/export/word")
        approval = judge_a.post("/api/approve")
        word = judge_a.get("/api/export/word")
        if word_output and word.status_code == 200:
            word_output.write_bytes(word.content)

        judge_a.post("/api/demo/seed").raise_for_status()
        state_b_after = judge_b.get("/api/case").json()
        isolated_after = state_b_after["last_run"] is None

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
) -> dict[str, Any]:
    health = httpx.get(base_url.rstrip("/") + "/api/health", timeout=30.0)
    health.raise_for_status()
    result: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "health": health.json(),
        "default_five": [],
        "route_nine": [],
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
    result["public_e2e"] = _public_e2e(base_url, word_output=word_output)
    gemini_rows = result["default_five"] + result["route_nine"]
    result["ok"] = result["public_e2e"]["passed"] and all(row["passed"] for row in gemini_rows)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Souzoku Shieldの公開Gemini・E2E証拠を収集します。")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--word-output", type=Path)
    parser.add_argument("--skip-gemini", action="store_true")
    args = parser.parse_args()

    try:
        evidence = collect_evidence(
            args.base_url,
            skip_gemini=args.skip_gemini,
            word_output=args.word_output,
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
