from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid
from typing import Protocol

from .batching import review_batches, translation_batches
from .execution import (
    PatchConflict,
    SCHEMAS,
    apply_translation_patch,
    create_review_task,
    create_translation_task,
    load_translation_task,
    load_review_task_state,
    review_target_digest,
    set_review_task_status,
    validate_review_evidence_current,
    validate_review_task_current,
    validate_translation_patch,
)
from .glossary import consistency_issues, load_glossary
from .model import Document, TranslationUnit, content_hash
from .providers import ProviderCapabilities, TranslationProvider
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
    batches = review_batches(units, provider.capabilities, language, context, rubric)
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
    for batch, batch_context, batch_glossary in translation_batches(units, provider.capabilities, language, context, glossary):
        result = provider.translate(batch, batch_context, batch_glossary, language)
        if set(result) != {unit.id for unit in batch}:
            raise ValueError("Provider response must contain exactly one translation per requested unit")
        translations.update(result)
    return translations


def _append_round(path: Path, rounds: list[dict], record: dict) -> dict:
    task_ids = set(record.get("evidence_task_ids", []))
    if task_ids:
        project = path.parents[2]
        document = Document.from_dict(read_json(project / "work" / "document.json"))
        digest = review_target_digest([
            {"unit_id": unit.id, "target_text": unit.target_text or ""}
            for unit in document.units if unit.section_id == path.stem and unit.translatable
        ])
        if digest != record["target_digest"]:
            raise PatchConflict("STALE_REVIEW_ROUND: chapter target changed")
        for task_id in task_ids:
            state_path = project / "work" / "task-state" / "review" / f"{task_id}.json"
            if state_path.exists() and read_json(state_path)["status"] in {"superseded", "rejected"}:
                raise PatchConflict("REVIEW_ROUND_CONFLICT: task is superseded or rejected")
    for previous in reversed(rounds):
        if task_ids & set(previous.get("evidence_task_ids", [])):
            if (task_ids != set(previous["evidence_task_ids"])
                    or record["target_digest"] != previous["target_digest"]
                    or record.get("issues", []) != previous.get("issues", [])):
                raise PatchConflict("REVIEW_ROUND_CONFLICT: evidence already belongs to another round")
            return previous
    rounds.append(record)
    write_json(path, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})
    return record


def _agent_review_evidence(project: Path, document: Document, candidate: dict,
                           executor: ReviewExecutor, model: str) -> dict:
    task = candidate
    control = project / "qa" / "chapter-controls" / f"{task['chapter_id']}.json"
    rounds = read_json(control).get("rounds", []) if control.exists() else []
    for path in sorted((project / "work" / "tasks" / "review").glob("*.json")):
        saved = read_json(path)
        if saved["chapter_id"] != candidate["chapter_id"]:
            continue
        state_path = project / "work" / "task-state" / "review" / f"{saved['task_id']}.json"
        try:
            validate_review_task_current(document, saved)
        except PatchConflict as error:
            if not state_path.exists() or read_json(state_path)["status"] not in {"superseded", "rejected"}:
                set_review_task_status(project, saved, "superseded", reason=str(error))
            continue
        if any(saved[key] != candidate[key] for key in
               ("reviewer_id", "translation_owner_id", "source_units", "target_units", "rubric")):
            continue
        full_digest = saved["context_capsule"].get("chapter_target_digest")
        if full_digest is None:
            full_digest = next((row.get("target_digest") for row in reversed(rounds)
                                if saved["task_id"] in row.get("evidence_task_ids", [])), None)
        if full_digest is not None and full_digest != candidate["context_capsule"]["chapter_target_digest"]:
            if not state_path.exists() or read_json(state_path)["status"] not in {"superseded", "rejected"}:
                set_review_task_status(project, saved, "superseded", reason="chapter target changed")
            continue
        state = load_review_task_state(project, saved)
        if state["status"] in {"superseded", "rejected"}:
            raise PatchConflict(f"REVIEW_TASK_{state['status'].upper()}: {saved['task_id']}")
        task = saved
        break
    else:
        write_json(project / "work" / "tasks" / "review" / f"{task['task_id']}.json", task)
        set_review_task_status(project, task, "pending")
        state = {"status": "pending"}
    evidence_path = project / "qa" / "review-evidence" / f"{task['task_id']}.json"
    if state["status"] == "completed" or evidence_path.exists():
        # Missing completed output is an error, never permission to redispatch.
        evidence = read_json(evidence_path)
    else:
        evidence = executor.execute(task, model=model)
    try:
        validate_review_evidence_current(project, evidence, task)
    except ValueError as error:
        status = "superseded" if str(error).startswith("STALE_REVIEW") else "rejected"
        set_review_task_status(project, task, status, reason=str(error))
        raise
    if state["status"] != "completed":
        write_json(evidence_path, evidence)
        set_review_task_status(project, task, "completed", evidence=str(evidence_path.relative_to(project)))
    return evidence


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
    capabilities: ProviderCapabilities, language: dict, glossary: list[dict[str, str]],
    executor: ReviewExecutor, owner_id: str, model: str, review_round: dict, rounds: list[dict],
    resume_task_id: str | None = None,
) -> tuple[Document, int]:
    issue_ids = {issue["unit_id"] for issue in issues}
    selected = [unit for unit in document.units if unit.section_id == chapter_id and unit.id in issue_ids]
    context = json.dumps({"purpose": "fix these independent review findings", "issues": issues}, ensure_ascii=False)
    applied = review_round["fixes_applied"]
    batches = []
    if resume_task_id is not None:
        completed_ids = set()
        for task_id in review_round["fix_task_ids"]:
            patch_path = project / "work" / "patches" / "translation" / f"{task_id}.json"
            if patch_path.exists():
                completed_ids.update(row["unit_id"] for row in read_json(patch_path)["patch"]["translations"])
        selected = [unit for unit in selected if unit.id not in completed_ids]
        saved = load_translation_task(project, resume_task_id)
        pending_ids = set(saved["unit_ids"])
        batch = [unit for unit in selected if unit.id in pending_ids]
        selected = [unit for unit in selected if unit.id not in pending_ids]
        batches.append((batch, saved["context_capsule"]["batch"]))
    for batch, batch_context, _ in translation_batches(selected, capabilities, language, context, glossary):
        batch_ids = {unit.id for unit in batch}
        batches.append((batch, {"review_issues": [issue for issue in issues if issue["unit_id"] in batch_ids],
                               "translation_batch": json.loads(batch_context)}))
    for batch, batch_context in batches:
        batch_ids = {unit.id for unit in batch}
        task_path = create_translation_task(
            project, chapter_id, owner_id,
            unit_ids=[unit.id for unit in batch],
            batch_context=batch_context,
            resume_task_id=resume_task_id,
        )
        resume_task_id = None
        task = load_translation_task(project, task_path.stem)
        if task["task_id"] not in review_round.setdefault("fix_task_ids", []):
            review_round["fix_task_ids"].append(task["task_id"])
        control = project / "qa" / "chapter-controls" / f"{chapter_id}.json"
        write_json(control, {"rounds": rounds, "latest_round_status": review_round["latest_round_status"]})
        patch = executor.execute(task, model=model)
        if not isinstance(patch, dict):
            raise ValueError("Agent executor must return a TranslationPatch object")
        patch.setdefault("model", model)
        before = {unit.id: unit.target_text for unit in batch}
        validate_translation_patch(project, patch)
        document = apply_translation_patch(project, patch)
        after = {unit.id: unit.target_text for unit in document.units if unit.id in batch_ids}
        applied += sum(before[unit_id] != after[unit_id] for unit_id in batch_ids)
        review_round["fixes_applied"] += sum(before[unit_id] != after[unit_id] for unit_id in batch_ids)
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
            "review_round": review_round["round"], "review_task_ids": review_round["evidence_task_ids"],
        })
        review_round["target_digest_after_fixes"] = review_target_digest(chapter_targets)
        write_json(control, {"rounds": rounds, "latest_round_status": review_round["latest_round_status"]})
    return document, applied


def run_chapter_qa_agent(
    project: Path, document: Document, provider: TranslationProvider | None, language: dict,
    glossary: list[dict[str, str]], executor: ReviewExecutor, *, reviewer_id: str,
    translation_owner_id: str, review_model: str, translation_backend: int,
    translation_executor: ReviewExecutor | None = None, translation_model: str = "gpt-6-luna-max",
    resume_fix_task_id: str | None = None,
    capabilities: ProviderCapabilities | None = None,
) -> Document:
    active_capabilities = capabilities if capabilities is not None else provider.capabilities
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
        passed = next((row for row in reversed(rounds) if
                       row.get("scope") == "FULL_CHAPTER"
                       and row.get("latest_round_status") == "PASS"
                       and row.get("issues_found") == 0
                       and row.get("coverage_status") == "FULL_CHAPTER_COVERAGE"
                       and row.get("target_digest") == target_digest
                       and row.get("evidence_task_ids")
                       and all(not (project / "work" / "task-state" / "review" / f"{task_id}.json").exists()
                               or read_json(project / "work" / "task-state" / "review" / f"{task_id}.json")["status"] == "completed"
                               for task_id in row["evidence_task_ids"])), None)
        if passed is not None:
            for unit in translated:
                unit.qa_status = "passed"
                unit.last_qa_target_hash = content_hash(unit.target_text)
                unit.last_error = None
            control_record["latest_round_status"] = "PASS"
            write_json(control, control_record)
            _save_document(project, document)
            continue
        if control_record.get("latest_round_status") == "PASS":
            control_record["latest_round_status"] = "STALE"
            write_json(control, control_record)
        if resume_fix_task_id is not None and load_translation_task(project, resume_fix_task_id)["chapter_id"] == section.section_id:
            record = next(row for row in rounds if resume_fix_task_id in row.get("fix_task_ids", []))
            document, applied = _agent_review_fixes(
                project, document, section.section_id, record["issues"], active_capabilities, language, glossary,
                translation_executor, translation_owner_id, translation_model, record, rounds,
                resume_task_id=resume_fix_task_id,
            )
            record["fixes_applied"] = applied
            record["fixes_completed"] = True
            write_json(control, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})
            resume_fix_task_id = None
            translated = [unit for unit in document.units if unit.section_id == section.section_id and unit.translatable]
        for _ in range(REVIEWS_PER_INVOCATION):
            context = json.dumps({"section_title": section.title}, ensure_ascii=False)
            batches = review_batches(translated, active_capabilities, language, context, language["review_rubric"])
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
                evidence = _agent_review_evidence(project, document, task, executor, review_model)
                evidence_task_ids.append(evidence["task_id"])
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
                record = _append_round(control, rounds, record)
                record.update(latest_round_status="PASS", allow_next_chapter=True, unresolved_blocking_issues=0)
                write_json(control, {"rounds": rounds, "latest_round_status": "PASS"})
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
            record = _append_round(control, rounds, record)
            if record.get("fixes_completed") or record.get("fixes_applied", 0):
                raise RuntimeError("H3_FIX_NO_TARGET_CHANGE: completed fixes require a changed target before recheck")
            if translation_backend == 1:
                if translation_executor is None:
                    raise RuntimeError("AGENT_FIX_BACKEND_UNAVAILABLE: translation owner is unavailable")
                document, applied = _agent_review_fixes(
                    project, document, section.section_id, issues, active_capabilities, language, glossary,
                    translation_executor, translation_owner_id, translation_model, record, rounds,
                    resume_task_id=resume_fix_task_id,
                )
                resume_fix_task_id = None
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
            record["fixes_applied"] = applied
            record["fixes_completed"] = True
            record["target_digest_after_fixes"] = review_target_digest([
                {"unit_id": unit.id, "target_text": unit.target_text or ""}
                for unit in document.units if unit.section_id == section.section_id and unit.translatable
            ])
            write_json(control, {"rounds": rounds, "latest_round_status": record["latest_round_status"]})
            document = Document.from_dict(read_json(project / "work" / "document.json"))
            translated = [unit for unit in document.units if unit.section_id == section.section_id and unit.translatable]
        else:
            for unit in translated:
                unit.qa_status = "failed"
            _save_document(project, document)
            raise RuntimeError(f"Chapter {section.section_id} still needs a zero-issue full recheck; resume to continue")
    return document


def _consistency_candidates(project: Path, translated: list, glossary: list[dict]) -> tuple[list, list]:
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
            term_occurrences.append({"unit_id": unit.id, "section_id": unit.section_id, "count": len(found),
                                     "source_text": unit.source_text, "target_text": target})
        if not term_occurrences:
            continue
        row = {"term": term["source"], "preferred_translation": term["preferred_translation"],
               "source_occurrences": term_occurrences, "target_variants": sorted(target_forms),
               "section_ids": sorted({item["section_id"] for item in term_occurrences}),
               "unit_ids": [item["unit_id"] for item in term_occurrences]}
        occurrences.append(row)
        if sum(item["count"] for item in term_occurrences) > 1:
            candidates.append(row)
    write_json(project / "qa/consistency/occurrences.json", {"terms": occurrences})
    write_json(project / "qa/consistency/candidates.json", {
        "candidates": [{key: row[key] for key in
                        ("term", "preferred_translation", "target_variants", "section_ids", "unit_ids")}
                       for row in candidates], "deterministic_issues": issues,
    })
    return issues, candidates


def run_book_consistency(project: Path, document: Document, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]]) -> None:
    report_path = project / "qa" / "book-consistency.json"
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    translated_by_id = {unit.id: unit for unit in translated}
    current_hash = _target_hash(translated)
    glossary_hash = content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True))
    previous = read_json(report_path) if report_path.exists() else {}
    rounds = previous.get("rounds", [])
    for _ in range(REVIEWS_PER_INVOCATION):
        issues, candidates = _consistency_candidates(project, translated, glossary)
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


BOOK_CONSISTENCY_RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "task_id", "reviewer_id", "target_hash", "glossary_hash", "issues", "summary"],
    "properties": {
        "schema_version": {"const": 1}, "task_id": {"type": "string", "format": "uuid"},
        "reviewer_id": {"type": "string"}, "target_hash": {"type": "string"},
        "glossary_hash": {"type": "string"}, "issues": SCHEMAS["ReviewEvidence"]["properties"]["issues"],
        "summary": {"type": "string"},
    },
}


def validate_book_consistency_task_current(project: Path, task: dict) -> None:
    document = Document.from_dict(read_json(project / "work/document.json"))
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    glossary = load_glossary(project / "glossary/terms.csv")
    if (_target_hash(translated) != task["target_hash"]
            or content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True)) != task["glossary_hash"]):
        raise PatchConflict("STALE_BOOK_CONSISTENCY: targets or approved glossary changed")
    by_id = {unit.id: unit for unit in translated}
    if any(row["unit_id"] not in by_id or by_id[row["unit_id"]].source_hash != row["source_hash"]
           for row in task["source_units"]):
        raise PatchConflict("STALE_BOOK_CONSISTENCY: source units changed")


def validate_book_consistency_result(project: Path, result: dict, task: dict) -> None:
    if not isinstance(result, dict) or set(result) != set(BOOK_CONSISTENCY_RESULT_SCHEMA["required"]):
        raise ValueError("Book consistency result contains missing or unknown fields")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("Unsupported book consistency result schema")
    for key in ("task_id", "reviewer_id", "target_hash", "glossary_hash"):
        if result[key] != task[key]:
            raise ValueError(f"Book consistency result {key} does not match task")
    if not isinstance(result["issues"], list) or not isinstance(result["summary"], str):
        raise ValueError("Book consistency issues/summary have invalid types")
    properties = SCHEMAS["ReviewEvidence"]["properties"]["issues"]["items"]["properties"]
    unit_ids = {row["unit_id"] for row in task["source_units"]}
    for issue in result["issues"]:
        if not isinstance(issue, dict) or set(issue) != set(properties):
            raise ValueError("Book consistency issue contains missing or unknown fields")
        if not isinstance(issue["unit_id"], str) or issue["unit_id"] not in unit_ids:
            raise ValueError("Book consistency issue refers to an unknown or unassigned unit")
        if issue["category"] not in properties["category"]["enum"] or issue["severity"] not in properties["severity"]["enum"]:
            raise ValueError("Book consistency issue category/severity is invalid")
        if not isinstance(issue["description"], str) or not issue["description"].strip() or not isinstance(issue["suggested_fix"], str):
            raise ValueError("Book consistency issue description/suggested_fix is invalid")
    validate_book_consistency_task_current(project, task)


def validate_book_consistency_fix_current(project: Path, task: dict) -> None:
    document = Document.from_dict(read_json(project / "work/document.json"))
    context = task["context_capsule"]["batch"]
    glossary = load_glossary(project / "glossary/terms.csv")
    if (_target_hash([unit for unit in document.units if unit.translatable and unit.target_text is not None])
            != context["book_target_hash"]
            or content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True)) != context["glossary_hash"]):
        raise PatchConflict("STALE_BOOK_CONSISTENCY_FIX: targets or approved glossary changed")


def run_book_consistency_agent(
    project: Path, document: Document, capabilities: ProviderCapabilities, language: dict,
    glossary: list[dict[str, str]], executor: ReviewExecutor, *, reviewer_id: str,
    translation_owner_id: str, review_model: str, translation_executor: ReviewExecutor,
    translation_model: str,
) -> Document:
    if reviewer_id == translation_owner_id:
        raise ValueError("Book consistency reviewer must differ from translation owner")
    report_path = project / "qa/book-consistency.json"
    translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
    target_hash = _target_hash(translated)
    glossary_hash = content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True))
    report = read_json(report_path) if report_path.exists() else {"rounds": []}
    if report.get("status") == "PASS" and report.get("actual_backend") == 1 and report["target_hash"] == target_hash and report["glossary_hash"] == glossary_hash:
        return document
    active = report.get("active_round")
    if active is None:
        issues, candidates = _consistency_candidates(project, translated, glossary)
        by_id = {unit.id: unit for unit in translated}
        active = {"round": len(report["rounds"]) + 1, "phase": "review", "task_ids": [],
                  "target_hash": target_hash, "glossary_hash": glossary_hash,
                  "issues": issues, "fix_task_ids": [], "fixes_applied": 0}
        for candidate in candidates:
            units = [by_id[unit_id] for unit_id in candidate["unit_ids"]]
            context = json.dumps({"candidate_term": candidate["term"],
                                  "preferred_translation": candidate["preferred_translation"],
                                  "target_variants": candidate["target_variants"],
                                  "candidate_section_ids": candidate["section_ids"],
                                  "candidate_unit_count": len(candidate["unit_ids"]),
                                  "occurrence_count": sum(item["count"] for item in candidate["source_occurrences"])}, ensure_ascii=False)
            batches = review_batches(units, capabilities, language, context, language["consistency_rubric"])
            for index, (batch, batch_context) in enumerate(batches):
                task = {"schema_version": 1, "task_id": str(uuid.uuid4()), "task_type": "book_consistency",
                        "reviewer_id": reviewer_id, "translation_owner_id": translation_owner_id,
                        "target_hash": target_hash, "glossary_hash": glossary_hash,
                        "source_units": [{"unit_id": unit.id, "section_id": unit.section_id,
                                          "source_hash": unit.source_hash, "source_text": unit.source_text} for unit in batch],
                        "target_units": [{"unit_id": unit.id, "target_text": unit.target_text} for unit in batch],
                        "context_capsule": {"batch": json.loads(batch_context), "batch_index": index,
                                            "batch_count": len(batches), "candidate_unit_count": len(candidate["unit_ids"]),
                                            "candidate_section_ids": candidate["section_ids"]},
                        "rubric": language["consistency_rubric"],
                        "required_result_schema": BOOK_CONSISTENCY_RESULT_SCHEMA}
                write_json(project / "work/tasks/consistency" / f"{task['task_id']}.json", task)
                active["task_ids"].append(task["task_id"])
        report.update(status="REVIEWING", target_hash=target_hash, glossary_hash=glossary_hash,
                      active_round=active, requested_backend=1, actual_backend=1)
        write_json(report_path, report)
    if active["glossary_hash"] != glossary_hash:
        raise PatchConflict("STALE_BOOK_CONSISTENCY: approved glossary changed")
    if active["phase"] == "review":
        if active["target_hash"] != target_hash:
            raise PatchConflict("STALE_BOOK_CONSISTENCY: targets changed during review")
        issues = list(active["issues"])
        for task_id in active["task_ids"]:
            task = read_json(project / "work/tasks/consistency" / f"{task_id}.json")
            evidence_path = project / "qa/consistency/evidence" / f"{task_id}.json"
            result = read_json(evidence_path) if evidence_path.exists() else executor.execute(task, model=review_model)
            validate_book_consistency_result(project, result, task)
            if not evidence_path.exists():
                write_json(evidence_path, result)
            issues.extend(result["issues"])
        if not issues:
            report["rounds"].append({"round": active["round"], "issues_found": 0, "fixes_applied": 0,
                                     "status": "PASS", "evidence_task_ids": active["task_ids"],
                                     "reviewer_id": reviewer_id, "review_model": review_model})
            report.pop("active_round")
            report.update(status="PASS", target_hash=target_hash, glossary_hash=glossary_hash)
            write_json(report_path, report)
            return document
        active.update(phase="fix", issues=issues, fix_batches=[], fix_index=0, pending_fix_task_id=None)
        selected_ids = {issue["unit_id"] for issue in issues}
        for section in document.sections:
            units = [unit for unit in translated if unit.section_id == section.section_id and unit.id in selected_ids]
            for batch, batch_context, _ in translation_batches(
                units, capabilities, language, json.dumps({"purpose": "fix book consistency findings", "issues": issues}, ensure_ascii=False), glossary,
            ):
                active["fix_batches"].append({"chapter_id": section.section_id,
                                              "unit_ids": [unit.id for unit in batch],
                                              "translation_batch": json.loads(batch_context)})
        report["status"] = "FIXING"
        write_json(report_path, report)
    while active["fix_index"] < len(active["fix_batches"]):
        batch = active["fix_batches"][active["fix_index"]]
        if active["pending_fix_task_id"] is None:
            task_path = create_translation_task(
                project, batch["chapter_id"], translation_owner_id, unit_ids=batch["unit_ids"],
                batch_context={"consistency_issues": [issue for issue in active["issues"] if issue["unit_id"] in batch["unit_ids"]],
                               "consistency_round": active["round"], "translation_batch": batch["translation_batch"],
                               "book_target_hash": _target_hash([unit for unit in document.units if unit.translatable and unit.target_text is not None]),
                               "glossary_hash": glossary_hash},
            )
            active["pending_fix_task_id"] = task_path.stem
            active["fix_task_ids"].append(task_path.stem)
            write_json(report_path, report)
        task = load_translation_task(project, active["pending_fix_task_id"])
        archive = project / "work/patches/translation" / f"{task['task_id']}.json"
        if not archive.exists():
            validate_book_consistency_fix_current(project, task)
            patch = translation_executor.execute(task, model=translation_model)
            before = {unit.id: unit.target_text for unit in document.units if unit.id in batch["unit_ids"]}
            document = apply_translation_patch(project, patch)
            changed = sum(unit.target_text != before[unit.id] for unit in document.units if unit.id in before)
            write_json(archive, {"task_id": task["task_id"], "patch_id": patch["patch_id"],
                                 "chapter_id": task["chapter_id"], "owner_id": task["owner_id"],
                                 "requested_backend": 1, "actual_backend": 1, "model": translation_model,
                                 "patch": patch, "consistency_round": active["round"],
                                 "fixes_applied": changed})
        active["fixes_applied"] += read_json(archive)["fixes_applied"]
        active["fix_index"] += 1
        active["pending_fix_task_id"] = None
        report["status"] = "FIXING"
        write_json(report_path, report)
    if not active["fixes_applied"]:
        raise RuntimeError("BOOK_CONSISTENCY_FIX_NO_TARGET_CHANGE: findings require a changed target before recheck")
    report["rounds"].append({"round": active["round"], "issues_found": len(active["issues"]),
                             "fixes_applied": active["fixes_applied"], "status": "FIXED_RECHECK_REQUIRED",
                             "issues": active["issues"], "evidence_task_ids": active["task_ids"],
                             "fix_task_ids": active["fix_task_ids"], "reviewer_id": reviewer_id,
                             "review_model": review_model})
    report.pop("active_round")
    report.update(status="RECHECK_REQUIRED", target_hash=_target_hash(
        [unit for unit in document.units if unit.translatable and unit.target_text is not None]))
    write_json(report_path, report)
    return document


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
