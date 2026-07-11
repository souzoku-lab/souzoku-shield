from __future__ import annotations

from typing import Any

from scripts import production_evidence


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, gemini_route: str, calls: list[str]) -> None:
        self.gemini_route = gemini_route
        self.calls = calls

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        self.calls.append(path)
        if path != "/api/run":
            return FakeResponse({"ok": True})
        return FakeResponse(
            {
                "run": {
                    "mode": "gemini_function_calling",
                    "gemini": {
                        "used": True,
                        "tool_name": "select_taker_branch",
                        "arguments": {"acquirer_type": self.gemini_route},
                        "fallback_reason": "",
                        "latency_ms": 12,
                    },
                },
                "case": {
                    "analysis": {"acquirer": {"id": "co_resident"}},
                    "state": {
                        "heirs": [{"id": "second_son"}],
                        "home_acquirer_id": "second_son",
                    },
                },
            }
        )


def test_run_case_clears_seed_cards_and_requires_matching_gemini_route(monkeypatch) -> None:
    calls: list[str] = []
    current_route = {"value": "co_resident"}

    monkeypatch.setattr(
        production_evidence,
        "_client",
        lambda base_url: FakeClient(current_route["value"], calls),
    )

    passed = production_evidence._run_case("https://example.test", "case", "相談文", "co_resident")
    assert calls == ["/api/demo/seed", "/api/demo/clear-heirs", "/api/run"]
    assert passed["gemini_route"] == "co_resident"
    assert passed["cards_inferred"] is True
    assert passed["passed"] is True

    calls.clear()
    current_route["value"] = "house_lost"
    failed = production_evidence._run_case("https://example.test", "case", "相談文", "co_resident")
    assert failed["actual_route"] == "co_resident"
    assert failed["gemini_route"] == "house_lost"
    assert failed["passed"] is False
