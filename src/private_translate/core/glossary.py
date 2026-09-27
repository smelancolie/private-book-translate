from __future__ import annotations

import csv
from pathlib import Path
import re

from .model import TranslationUnit


def load_glossary(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return [
        {key: value.strip() for key, value in row.items() if key and value}
        for row in rows
        if row.get("source") and row.get("preferred_translation") and row.get("approved", "").lower() in {"true", "yes", "1"}
    ]


def consistency_issues(units: list[TranslationUnit], glossary: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    for unit in units:
        if not unit.translatable or unit.target_text is None:
            continue
        for term in glossary:
            source = term["source"]
            target = term["preferred_translation"]
            if not re.search(re.escape(source), unit.source_text, flags=re.IGNORECASE):
                continue
            forbidden = [value.strip() for value in term.get("forbidden_translations", "").split("|") if value.strip()]
            if target not in unit.target_text or any(value in unit.target_text for value in forbidden):
                issues.append({
                    "unit_id": unit.id,
                    "severity": "P1",
                    "description": f"Glossary consistency: translate {source!r} as {target!r}.",
                })
    return issues
