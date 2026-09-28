from __future__ import annotations

import json
from pathlib import Path


def load_language_package(source_language: str, target_language: str) -> dict:
    root = Path(__file__).resolve().parents[1] / "languages"
    for config_path in root.glob("*/config.json"):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if (config["source_language"], config["target_language"]) == (source_language, target_language):
            config["package_path"] = str(config_path.parent)
            rule_names = ("translation", "review", "terminology", "proper_names", "style", "typography", "polysemy")
            rules = {
                name: (config_path.parent / f"{name}.md").read_text(encoding="utf-8").strip()
                for name in rule_names
            }
            config["language_rules"] = rules
            config["translation_prompt"] = "\n\n".join(rules[name] for name in rule_names if name != "review")
            config["review_rubric"] = "\n\n".join(rules[name] for name in rule_names if name != "translation")
            config["consistency_rubric"] = "\n\n".join(rules[name] for name in ("terminology", "proper_names", "style", "typography"))
            return config
    raise ValueError(f"No language package for {source_language} → {target_language}")
