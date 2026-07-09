from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "exports",
    "_docx_render",
    "_reference_pdf",
}
SKIP_FILES = {"verify_no_secrets.py"}
PRIVATE_MARKERS = [
    "AI" + "結",
    "why" + "memory",
    "Graph" + "RAG",
    "conversation" + "_db",
    "knowledge" + "_db",
    "relationship" + "_private",
]
PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN " + r"(?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r'"private_key"\s*:\s*"-----BEGIN'),
    re.compile(r"GOOGLE_APPLICATION_CREDENTIALS\s*=\s*.+\.json"),
    *[re.compile(re.escape(marker), re.IGNORECASE) for marker in PRIVATE_MARKERS],
]


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.name not in SKIP_FILES:
            files.append(path)
    return files


def main() -> int:
    findings: list[str] = []
    for path in iter_files():
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in PATTERNS:
            if pattern.search(text):
                findings.append(str(path.relative_to(ROOT)))
                break
    if findings:
        print("Potential secret or private implementation marker found:")
        for item in findings:
            print(f"- {item}")
        return 1
    print("OK: no obvious secrets or private implementation markers found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
