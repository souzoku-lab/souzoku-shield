from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "rules"


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    """M1の決定的ルール表を読み込む。"""
    expert = json.loads((RULES_DIR / "expert_knowledge.json").read_text(encoding="utf-8"))
    template = json.loads((RULES_DIR / "shomen_template.json").read_text(encoding="utf-8"))
    return {"expert": expert, "template": template}


def default_case_state() -> dict[str, Any]:
    rules = load_rules()
    expert = rules["expert"]
    documents = {
        doc["id"]: doc.get("default_status", "not_requested")
        for doc in expert["documents"]
    }
    return {
        "case_id": expert["demo_case"]["id"],
        "acquirer_type": expert["demo_case"]["default_acquirer_type"],
        "home_acquirer_id": "eldest_son",
        "heirs": [
            {
                "id": "mother",
                "name": "母",
                "relation": "spouse",
                "co_resident": True,
            },
            {
                "id": "eldest_son",
                "name": "長男",
                "relation": "child",
                "co_resident": True,
            },
            {
                "id": "second_son",
                "name": "次男",
                "relation": "child",
                "co_resident": False,
            },
        ],
        "partition_status": expert["demo_case"]["default_partition_status"],
        "documents": documents,
        "manual_inputs": {
            "overall_opinion": "",
        },
    }
