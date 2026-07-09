import pytest


@pytest.fixture(autouse=True)
def clear_gemini_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
