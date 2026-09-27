from __future__ import annotations

import json
from pathlib import Path


def load_language_package(source_language: str, target_language: str) -> dict:
    root = Path(__file__).resolve().parents[1] / "languages"
    for config_path in root.glob("*/config.json"):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if (config["source_language"], config["target_language"]) == (source_language, target_language):
            config["package_path"] = str(config_path.parent)
            return config
    raise ValueError(f"No language package for {source_language} → {target_language}")
