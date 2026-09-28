from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Protocol

from .batching import review_batches, translation_batches
from .execution import (
    apply_translation_patch,
    create_review_task,
    create_translation_task,
    load_translation_task,
    review_target_digest,
    validate_review_evidence_current,
    validate_translation_patch,
)
from .glossary import consistency_issues
from .model import Document, TranslationUnit, content_hash
from .providers import TranslationProvider
from .state import read_json, write_json


REVIEWS_PER_INVOCATION = 3


class ReviewExecutor(Protocol):
    def execute(self, task: dict, *, model: str) -> dict: ...


def _target_hash(units: list[TranslationUnit]) -> str:
    return content_hash(json.dumps([(unit.id, unit.target_text) for unit in units], ensure_ascii=False, separators=(",", ":")))


def _save_document(project: Path, document: Document) -> None:
    write_json(project / "work" / "document.json", document.to_dict())


def _review_text(units: list[TranslationUnit], field: str) -> str:
    return "\n\n".join(f"[{unit.id}] {getattr(unit, field) or ''}" for unit in units if unit.translatable)


def _review_issues(units: list[TranslationUnit], provider: TranslationProvider, language: dict, context: str, rubric: str) -> tuple[list[dict], int]:
    issues = []
    batches = review_batches(units, provider, language, context, rubric)
    for batch, batch_context in batches:
        targets = {unit.id: unit.target_text for unit in batch}
        issues.extend(provider.review(
            _review_text(batch, "source_text"), json.dumps(targets, ensure_ascii=False),
            batch_context, rubric, language,
        ))
    return issues, len(batches)


def _translate_fixes(units: list[TranslationUnit], provider: TranslationProvider, language: dict,
                     glossary: list[dict[str, str]], issues: list[dict]) -> dict[str, str]:
    translations = {}
    context = "Fix these review findings: " + json.dumps(issues, ensure_ascii=False)
    for batch, batch_context, batch_glossary in translation_batches(units, provider, language, context, glossary):
        result = provider.translate(batch, batch_context, batch_glossary, language)
        if set(result) != {unit.id for unit in batch}:
            raise ValueError("Provider response must contain exactly one translation per requested unit")
        translations.update(result)
    return translations


def _append_round(path: Path, rounds: list[dict], record: dict) -> None:
    rounds.append(record)
    write_json(path, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})


def run_chapter_qa(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]], section_ids: set[str] | None = None) -> None:
    units_by_section = {}
    for unit in document.units:
        units_by_section.setdefault(unit.section_id, []).append(unit)
    for section in document.sections:
        if section_ids is not None and section.section_id not in section_ids:
            continue
        units = units_by_section.get(section.section_id, [])
        translated = [unit for unit in units if unit.translatable]
        if not translated:
            continue
        if all(unit.qa_status == "passed" and unit.target_text is not None and unit.last_qa_target_hash == content_hash(unit.target_text) for unit in translated):
            continue
        control = project / "qa" / "chapter-controls" / f"{section.section_id}.json"
        rounds = read_json(control).get("rounds", []) if control.exists() else []
        for _ in range(REVIEWS_PER_INVOCATION):
            current_round = len(rounds) + 1
            context = json.dumps({"section_title": section.title, "glossary": glossary}, ensure_ascii=False)
            issues, review_batch_count = _review_issues(translated, provider, language, context, language["review_rubric"])
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
                    "review_batch_count": review_batch_count,
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
            fixes = _translate_fixes(selected, provider, language, glossary, issues)
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


def _agent_review_fixes(
    project: Path, document: Document, chapter_id: str, issues: list[dict],
    provider: TranslationProvider, language: dict, glossary: list[dict[str, str]],
    executor: ReviewExecutor, owner_id: str, model: str,
) -> tuple[Document, int]:
    issue_ids = {issue["unit_id"] for issue in issues}
    selected = [unit for unit in document.units if unit.section_id == chapter_id and unit.id in issue_ids]
    context = json.dumps({"purpose": "fix these independent review findings", "issues": issues}, ensure_ascii=False)
    applied = 0
    for batch, batch_context, _ in translation_batches(selected, provider, language, context, glossary):
        batch_ids = {unit.id for unit in batch}
        task_path = create_translation_task(
            project, chapter_id, owner_id,
            unit_ids=[unit.id for unit in batch],
            batch_context={"review_issues": [issue for issue in issues if issue["unit_id"] in batch_ids],
                           "translation_batch": json.loads(batch_context)},
        )
        task = load_translation_task(project, task_path.stem)
        patch = executor.execute(task, model=model)
        if not isinstance(patch, dict):
            raise ValueError("Agent executor must return a TranslationPatch object")
        patch.setdefault("model", model)
        before = {unit.id: unit.target_text for unit in batch}
        validate_translation_patch(project, patch)
        document = apply_translation_patch(project, patch)
        after = {unit.id: unit.target_text for unit in document.units if unit.id in batch_ids}
        applied += sum(before[unit_id] != after[unit_id] for unit_id in batch_ids)
        chapter_targets = [
            {"unit_id": unit.id, "target_text": unit.target_text or ""}
            for unit in document.units if unit.section_id == chapter_id and unit.translatable
        ]
        write_json(project / "work" / "patches" / "translation" / f"{task['task_id']}.json", {
            "task_id": task["task_id"], "patch_id": patch["patch_id"],
            "chapter_id": task["chapter_id"], "owner_id": task["owner_id"],
            "requested_backend": 1, "actual_backend": 1, "model": patch.get("model", model),
            "patch": patch, "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_chapter_digest": task["base_chapter_digest"],
            "target_digest_after_merge": review_target_digest(chapter_targets),
        })
    return document, applied


def run_chapter_qa_agent(
    project: Path, document: Document, provider: TranslationProvider, language: dict,
    glossary: list[dict[str, str]], executor: ReviewExecutor, *, reviewer_id: str,
    translation_owner_id: str, review_model: str, translation_backend: int,
    translation_executor: ReviewExecutor | None = None, translation_model: str = "gpt-6-luna-max",
) -> Document:
    for section in document.sections:
        translated = [unit for unit in document.units if unit.section_id == section.section_id and unit.translatable]
        if not translated:
            continue
        control = project / "qa" / "chapter-controls" / f"{section.section_id}.json"
        control_record = read_json(control) if control.exists() else {}
        rounds = control_record.get("rounds", [])
        target_digest = review_target_digest([
            {"unit_id": unit.id, "target_text": unit.target_text or ""} for unit in translated
        ])
        latest = rounds[-1] if rounds else {}
        if (
            all(unit.qa_status == "passed" and unit.target_text is not None and unit.last_qa_target_hash == content_hash(unit.target_text) for unit in translated)
            and control_record.get("latest_round_status") == "PASS"
            and latest.get("reviewer_id") == reviewer_id
            and latest.get("translation_owner_id") == translation_owner_id
            and latest.get("coverage_status") == "FULL_CHAPTER_COVERAGE"
            and latest.get("target_digest") == target_digest
        ):
            continue
        for _ in range(REVIEWS_PER_INVOCATION):
            context = json.dumps({"section_title": section.title}, ensure_ascii=False)
            batches = review_batches(translated, provider, language, context, language["review_rubric"])
            expected_ids = [unit.id for unit in translated]
            batch_ids = [unit.id for batch, _ in batches for unit in batch]
            if batch_ids != expected_ids:
                raise ValueError("Agent review batches must cover the complete chapter exactly once")
            evidence_task_ids = []
            issues = []
            for index, (batch, batch_context) in enumerate(batches):
                source_text = " ".join(unit.source_text.casefold() for unit in batch)
                task = create_review_task(
                    document, section.section_id, reviewer_id, translation_owner_id,
                    language["review_rubric"],
                    context_capsule={
                        "section_title": section.title,
                        "batch_context": json.loads(batch_context),
                        "glossary_slice": [term for term in glossary if term["source"].casefold() in source_text],
                        "review_batch": {"index": index, "count": len(batches), "unit_ids": [unit.id for unit in batch]},
                    },
                    unit_ids=[unit.id for unit in batch],
                )
                task_path = project / "work" / "tasks" / "review" / f"{task['task_id']}.json"
                write_json(task_path, task)
                evidence = executor.execute(task, model=review_model)
                validate_review_evidence_current(project, evidence, task)
                evidence_path = project / "qa" / "review-evidence" / f"{task['task_id']}.json"
                write_json(evidence_path, evidence)
                evidence_task_ids.append(task["task_id"])
                issues.extend(evidence["issues"])

            full_target_digest = review_target_digest([
                {"unit_id": unit.id, "target_text": unit.target_text or ""} for unit in translated
            ])
            if not issues:
                target_hashes = {unit.id: content_hash(unit.target_text or "") for unit in translated}
                record = {
                    "round": len(rounds) + 1, "scope": "FULL_CHAPTER",
                    "coverage_status": "FULL_CHAPTER_COVERAGE", "review_batch_count": len(batches),
                    "evidence_task_ids": evidence_task_ids, "reviewer_id": reviewer_id,
                    "translation_owner_id": translation_owner_id, "review_model": review_model,
                    "target_digest": full_target_digest, "issues_found": 0, "fixes_applied": 0,
                    "unresolved_blocking_issues": 0, "latest_round_status": "PASS", "allow_next_chapter": True,
                }
                _append_round(control, rounds, record)
                for unit in translated:
                    unit.qa_status = "passed"
                    unit.last_qa_target_hash = target_hashes[unit.id]
                    unit.last_error = None
                _save_document(project, document)
                break

            record = {
                "round": len(rounds) + 1, "scope": "FULL_CHAPTER",
                "coverage_status": "FULL_CHAPTER_COVERAGE", "review_batch_count": len(batches),
                "evidence_task_ids": evidence_task_ids, "reviewer_id": reviewer_id,
                "translation_owner_id": translation_owner_id, "review_model": review_model,
                "target_digest": full_target_digest, "issues_found": len(issues),
                "fixes_applied": 0, "unresolved_blocking_issues": len(issues),
                "latest_round_status": "FIXED_RECHECK_REQUIRED", "allow_next_chapter": False,
                "issues": issues,
            }
            _append_round(control, rounds, record)
            if translation_backend == 1:
                if translation_executor is None:
                    raise RuntimeError("AGENT_FIX_BACKEND_UNAVAILABLE: translation owner is unavailable")
                document, applied = _agent_review_fixes(
                    project, document, section.section_id, issues, provider, language, glossary,
                    translation_executor, translation_owner_id, translation_model,
                )
            else:
                issue_ids = {issue["unit_id"] for issue in issues}
                selected = [unit for unit in translated if unit.id in issue_ids]
                fixes = _translate_fixes(selected, provider, language, glossary, issues)
                applied = 0
                for unit in selected:
                    applied += fixes[unit.id] != unit.target_text
                    unit.target_text = fixes[unit.id]
                    unit.qa_status = "pending"
                    unit.last_qa_target_hash = None
                _save_document(project, document)
            rounds[-1]["fixes_applied"] = applied
            _append_round(control, rounds[:-1], rounds[-1])
            document = Document.from_dict(read_json(project / "work" / "document.json"))
            translated = [unit for unit in document.units if unit.section_id == section.section_id and unit.translatable]
        else:
            for unit in translated:
                unit.qa_status = "failed"
            _save_document(project, document)
            raise RuntimeError(f"Chapter {section.section_id} still needs a zero-issue full recheck; resume to continue")
    return document


def run_book_consistency(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]]) -> None:
    report_path = project / "qa" / "book-consistency.json"
    occurrence_path = project / "qa" / "consistency" / "occurrences.json"
    candidate_path = project / "qa" / "consistency" / "candidates.json"
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    translated_by_id = {unit.id: unit for unit in translated}
    current_hash = _target_hash(translated)
    glossary_hash = content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True))
    previous = read_json(report_path) if report_path.exists() else {}
    rounds = previous.get("rounds", [])
    for _ in range(REVIEWS_PER_INVOCATION):
        issues = consistency_issues(translated, glossary)
        occurrences = []
        candidates = []
        for term in glossary:
            pattern = re.compile(re.escape(term["source"]), re.IGNORECASE)
            term_occurrences = []
            target_forms = set()
            for unit in translated:
                found = list(pattern.finditer(unit.source_text))
                if not found:
                    continue
                target = unit.target_text or ""
                expected = term["preferred_translation"]
                variants = [value for value in [expected, *term.get("forbidden_translations", "").split("|")] if value and value in target]
                if not variants:
                    variants = ["<preferred translation missing>"]
                target_forms.update(variants)
                term_occurrences.append({
                    "unit_id": unit.id, "section_id": unit.section_id, "count": len(found),
                    "source_text": unit.source_text, "target_text": target,
                })
            if not term_occurrences:
                continue
            row = {
                "term": term["source"], "preferred_translation": term["preferred_translation"],
                "source_occurrences": term_occurrences, "target_variants": sorted(target_forms),
                "section_ids": sorted({item["section_id"] for item in term_occurrences}),
                "unit_ids": [item["unit_id"] for item in term_occurrences],
            }
            occurrences.append(row)
            if sum(item["count"] for item in term_occurrences) > 1:
                candidates.append(row)
        candidate_payload = {
            "candidates": [
                {key: row[key] for key in ("term", "preferred_translation", "target_variants", "section_ids", "unit_ids")}
                for row in candidates
            ],
            "deterministic_issues": issues,
        }
        write_json(occurrence_path, {"terms": occurrences})
        write_json(candidate_path, candidate_payload)
        if previous.get("status") == "PASS" and previous.get("target_hash") == current_hash and previous.get("glossary_hash") == glossary_hash:
            return
        for candidate in candidates:
            candidate_units = [translated_by_id[unit_id] for unit_id in candidate["unit_ids"]]
            context = json.dumps({
                "candidate_term": candidate["term"],
                "preferred_translation": candidate["preferred_translation"],
                "target_variants": candidate["target_variants"],
                "occurrence_count": sum(item["count"] for item in candidate["source_occurrences"]),
            }, ensure_ascii=False)
            candidate_issues, _ = _review_issues(candidate_units, provider, language, context, language["consistency_rubric"])
            issues.extend(candidate_issues)
        current_round = len(rounds) + 1
        if not issues:
            rounds.append({"round": current_round, "issues_found": 0, "fixes_applied": 0, "status": "PASS"})
            write_json(report_path, {"status": "PASS", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
            return
        ids = {issue["unit_id"] for issue in issues if issue.get("unit_id")}
        known_ids = set(translated_by_id)
        if ids - known_ids:
            raise ValueError("Book consistency review returned an unknown translation-unit id")
        selected_ids = ids or {unit_id for candidate in candidates for unit_id in candidate["unit_ids"]}
        selected = [unit for unit in translated if unit.id in selected_ids]
        if not selected:
            raise ValueError("Consistency findings must identify candidate units")
        fixes = _translate_fixes(selected, provider, language, glossary, issues)
        applied = 0
        affected_sections = set()
        for unit in selected:
            applied += fixes[unit.id] != unit.target_text
            unit.target_text = fixes[unit.id]
            unit.qa_status = "pending"
            unit.last_qa_target_hash = None
            affected_sections.add(unit.section_id)
        rounds.append({"round": current_round, "issues_found": len(issues), "fixes_applied": applied, "status": "FIXED_RECHECK_REQUIRED", "issues": issues})
        write_json(report_path, {"status": "RECHECK_REQUIRED", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
        _save_document(project, document)
        run_chapter_qa(project, document, provider, language, glossary, affected_sections)
        translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
        translated_by_id = {unit.id: unit for unit in translated}
    write_json(report_path, {"status": "FAILED", "target_hash": _target_hash(translated), "glossary_hash": glossary_hash, "rounds": rounds})
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
