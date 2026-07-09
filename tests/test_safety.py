from __future__ import annotations

import subprocess
import sys

from app.engine.reducer import apply_definitive_filter
from app.rules_loader import load_rules


def test_definitive_filter_rewrites_consultation_phrases() -> None:
    rules = load_rules()
    text = "この土地は適用できます。名義預金です。"
    filtered = apply_definitive_filter(text, rules)

    assert "適用できます" not in filtered
    assert "名義預金です" not in filtered
    assert "税理士が確認する論点です" in filtered


def test_definitive_filter_handles_normalized_spacing_and_fails_closed() -> None:
    rules = load_rules()
    text = "名 義 預 金 です。 この土地は 適 用 で き ま す。"
    filtered = apply_definitive_filter(text, rules)

    assert "名義預金です" not in filtered.replace(" ", "")
    assert "適用できます" not in filtered.replace(" ", "")
    assert "税理士が確認する論点です" in filtered


def test_verify_no_secrets_script_is_green() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_no_secrets.py"],
        cwd=".",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
