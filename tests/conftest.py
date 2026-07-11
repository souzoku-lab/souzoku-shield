import pytest

import app.main as main_module


@pytest.fixture(autouse=True)
def clear_gemini_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main_module, "RUN_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(main_module, "RUN_LIMIT_PER_SESSION", 1000)
