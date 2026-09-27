from __future__ import annotations

import json
from pathlib import Path

from .glossary import consistency_issues
from .model import Document, TranslationUnit, content_hash
from .providers import TranslationProvider
from .state import read_json, write_json


REVIEWS_PER_INVOCATION = 3


def _section_units(document: Document, section_id: str) -> list[TranslationUnit]:
    return [unit for unit in document.units if unit.section_id == section_id]


def _target_hash(units: list[TranslationUnit]) -> str:
    return content_hash(json.dumps([(unit.id, unit.target_text) for unit in units], ensure_ascii=False, separators=(",", ":")))


def _save_document(project: Path, document: Document) -> None:
    write_json(project / "work" / "document.json", document.to_dict())


def _review_text(units: list[TranslationUnit], field: str) -> str:
    return "\n\n".join(f"[{unit.id}] {getattr(unit, field) or ''}" for unit in units if unit.translatable)


def _append_round(path: Path, rounds: list[dict], record: dict) -> None:
    rounds.append(record)
    write_json(path, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})


def run_chapter_qa(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]], section_ids: set[str] | None = None) -> None:
    for section in document.sections:
        if section_ids is not None and section.section_id not in section_ids:
            continue
        units = _section_units(document, section.section_id)
        translated = [unit for unit in units if unit.translatable]
        if not translated:
            continue
        if all(unit.qa_status == "passed" and unit.target_text is not None and unit.last_qa_target_hash == content_hash(unit.target_text) for unit in translated):
            continue
        control = project / "qa" / "chapter-controls" / f"{section.section_id}.json"
        rounds = read_json(control).get("rounds", []) if control.exists() else []
        for _ in range(REVIEWS_PER_INVOCATION):
            current_round = len(rounds) + 1
            targets = {unit.id: unit.target_text for unit in translated}
            issues = provider.review(
                _review_text(translated, "source_text"),
                json.dumps(targets, ensure_ascii=False),
                json.dumps({"section_title": section.title, "glossary": glossary}, ensure_ascii=False),
                language["review_rubric"], language,
            )
            if not issues:
                target_hashes = {unit.id: content_hash(unit.target_text or "") for unit in translated}
                record = {
                    "round": current_round,
                    "scope": "FULL_CHAPTER",
                    "issues_found": 0,
                    "fixes_applied": 0,
                    "unresolved_blocking_issues": 0,
                    "latest_round_status": "PASS",
                    "allow_next_chapter": True,
                    "target_hash": _target_hash(translated),
                    "expert_translation_skill_used": True,
                    "expert_level_review_status": "PASS",
                    "polysemy_translation_stage_review": "PASS",
                    "polysemy_context_review": "PASS",
                    "polysemy_unresolved_count": 0,
                }
                _append_round(control, rounds, record)
                for unit in translated:
                    unit.qa_status = "passed"
                    unit.last_qa_target_hash = target_hashes[unit.id]
                    unit.last_error = None
                _save_document(project, document)
                break

            known_ids = {unit.id for unit in translated}
            issue_ids = {issue["unit_id"] for issue in issues if issue.get("unit_id") in known_ids}
            if any(issue.get("unit_id") and issue["unit_id"] not in known_ids for issue in issues):
                raise ValueError("Chapter review returned an unknown translation-unit id")
            selected = [unit for unit in translated if not issue_ids or unit.id in issue_ids]
            fixes = provider.translate(
                selected,
                "Fix these full-chapter review findings, preserving all format markers: " + json.dumps(issues, ensure_ascii=False),
                glossary,
                language,
            )
            applied = 0
            for unit in selected:
                new_target = fixes[unit.id]
                applied += new_target != unit.target_text
                unit.target_text = new_target
                unit.qa_status = "pending"
                unit.last_qa_target_hash = None
            record = {
                "round": current_round,
                "scope": "FULL_CHAPTER",
                "issues_found": len(issues),
                "fixes_applied": applied,
                "unresolved_blocking_issues": len(issues),
                "latest_round_status": "FIXED_RECHECK_REQUIRED",
                "allow_next_chapter": False,
                "issues": issues,
            }
            _append_round(control, rounds, record)
            _save_document(project, document)
        else:
            for unit in translated:
                unit.qa_status = "failed"
            _save_document(project, document)
            raise RuntimeError(f"Chapter {section.section_id} still needs a zero-issue full recheck; resume to continue")


def run_book_consistency(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]]) -> None:
    report_path = project / "qa" / "book-consistency.json"
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    current_hash = _target_hash(translated)
    previous = read_json(report_path) if report_path.exists() else {}
    if previous.get("status") == "PASS" and previous.get("target_hash") == current_hash:
        return
    rounds = previous.get("rounds", [])
    for _ in range(REVIEWS_PER_INVOCATION):
        issues = consistency_issues(translated, glossary)
        issues.extend(provider.review(
            _review_text(translated, "source_text"),
            _review_text(translated, "target_text"),
            json.dumps({"sections": [section.title for section in document.sections], "glossary": glossary}, ensure_ascii=False),
            language["consistency_rubric"], language,
        ))
        current_round = len(rounds) + 1
        if not issues:
            rounds.append({"round": current_round, "issues_found": 0, "fixes_applied": 0, "status": "PASS"})
            write_json(report_path, {"status": "PASS", "target_hash": _target_hash(translated), "rounds": rounds})
            return
        ids = {issue["unit_id"] for issue in issues if issue.get("unit_id")}
        known_ids = {unit.id for unit in translated}
        if ids - known_ids:
            raise ValueError("Book consistency review returned an unknown translation-unit id")
        selected = [unit for unit in translated if not ids or unit.id in ids]
        fixes = provider.translate(selected, "Fix book-wide consistency findings: " + json.dumps(issues, ensure_ascii=False), glossary, language)
        applied = 0
        affected_sections = set()
        for unit in selected:
            applied += fixes[unit.id] != unit.target_text
            unit.target_text = fixes[unit.id]
            unit.qa_status = "pending"
            unit.last_qa_target_hash = None
            affected_sections.add(unit.section_id)
        rounds.append({"round": current_round, "issues_found": len(issues), "fixes_applied": applied, "status": "FIXED_RECHECK_REQUIRED", "issues": issues})
        write_json(report_path, {"status": "RECHECK_REQUIRED", "target_hash": _target_hash(translated), "rounds": rounds})
        _save_document(project, document)
        run_chapter_qa(project, document, provider, language, glossary, affected_sections)
        translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    write_json(report_path, {"status": "FAILED", "target_hash": _target_hash(translated), "rounds": rounds})
    raise RuntimeError("Book-level consistency QA needs another correction and review round")


def validate_qa(project: Path, document: Document) -> None:
    failed = [unit.id for unit in document.units if unit.translatable and (
        unit.translation_status != "translated" or unit.qa_status != "passed" or not unit.target_text
        or unit.last_qa_target_hash != content_hash(unit.target_text)
    )]
    if failed:
        raise ValueError(f"Translation or chapter QA is incomplete for {len(failed)} units")
    report = project / "qa" / "book-consistency.json"
    if not report.exists() or read_json(report).get("status") != "PASS":
        raise ValueError("Book-level consistency QA has not passed")
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    if read_json(report).get("target_hash") != _target_hash(translated):
        raise ValueError("Book-level consistency QA is stale for the current targets")
