from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import random
import secrets
import shutil
import subprocess
from typing import Protocol
import uuid

from .language import load_language_package
from .model import Document, TranslationUnit, content_hash
from .qa import validate_qa
from .state import read_json, write_json


STRATA = ("paragraph", "table", "figure", "formula", "caption_note")
TARGET_CONFIDENCE = 0.80
DEFECT_RATE = 0.10
REVIEWERS = ("reviewer_1", "reviewer_2")
DEFAULT_AGENT_SPOTCHECK_MODEL = "gpt-6-luna-max"
QUALITY_FAMILY_CATEGORIES = {
    "proper_name", "polysemy", "terminology", "source_syntax",
    "mechanical_literalism", "dialogue_stiffness",
}
FINDING_CATEGORIES = QUALITY_FAMILY_CATEGORIES | {
    "fidelity", "omission", "addition", "style", "other",
}
SPOTCHECK_EVIDENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "task_id", "round_id", "reviewer_id", "scores", "findings", "style_debt_count"],
    "properties": {
        "schema_version": {"const": 1}, "task_id": {"type": "string"},
        "round_id": {"type": "string"}, "reviewer_id": {"type": "string"},
        "scores": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100}},
        "style_debt_count": {"type": "integer", "minimum": 0},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["unit_id", "family_id", "category", "description", "reusable_rule"],
            "properties": {
                "unit_id": {"type": "string"}, "family_id": {"type": "string"},
                "category": {"enum": sorted(FINDING_CATEGORIES)},
                "description": {"type": "string"}, "reusable_rule": {"type": "string"},
            },
        }},
    },
}


class SpotcheckExecutor(Protocol):
    worker_id: str

    def execute(self, task: dict, *, model: str) -> dict: ...


def _stratum(unit: TranslationUnit) -> str | None:
    if unit.type in {"paragraph", "heading"}:
        return "paragraph"
    return unit.type if unit.type in STRATA else None


def _eligible_units(document: Document) -> dict[str, list[TranslationUnit]]:
    result = {name: [] for name in STRATA}
    for unit in document.units:
        stratum = _stratum(unit)
        figure_ready = stratum == "figure" and unit.metadata.get("asset_path") and unit.metadata.get("asset_hash")
        if stratum and unit.target_text is not None and (unit.translatable or stratum == "formula" or figure_ready):
            result[stratum].append(unit)
    return result


def _sample_mapping(unit: TranslationUnit) -> dict:
    mapping = {
        "unit_id": unit.id, "section_id": unit.section_id, "type": unit.type,
        "stratum": _stratum(unit), "source_text": unit.source_text,
        "source_hash": unit.source_hash, "locator": unit.locator,
    }
    if unit.type == "figure":
        mapping.update({key: unit.metadata.get(key, []) for key in (
            "asset_path", "asset_hash", "alt", "caption_unit_ids", "surrounding_unit_ids", "surrounding_text",
        )})
    return mapping


def _budget(stratum: str, population: int) -> int:
    if not population:
        return 0
    if stratum == "paragraph":
        return min(population, 120)
    full_scan_limit = {"table": 80, "figure": 80, "formula": 100, "caption_note": 120}[stratum]
    return population if population <= full_scan_limit else 20


def _confidence(population: int, sample_size: int) -> float | None:
    if not population:
        return None
    if sample_size == population:
        return 1.0
    return 1 - (1 - DEFECT_RATE) ** sample_size


def _selection(document: Document, seed: int):
    rng = random.Random(seed)
    strata = _eligible_units(document)
    selection = {reviewer: set() for reviewer in REVIEWERS}
    record = {}
    for name in STRATA:
        population = sorted(strata[name], key=lambda unit: unit.id)
        size = _budget(name, len(population))
        by_reviewer = {}
        if name == "paragraph":
            for reviewer in REVIEWERS:
                ids = [unit.id for unit in rng.sample(population, size)]
                by_reviewer[reviewer] = ids
                selection[reviewer].update(ids)
        else:
            ids = [unit.id for unit in rng.sample(population, size)]
            for reviewer in REVIEWERS:
                by_reviewer[reviewer] = ids
                selection[reviewer].update(ids)
        record[name] = {
            "population": len(population), "sample_size_per_reviewer": size,
            "samples_by_reviewer": by_reviewer, "confidence": _confidence(len(population), size),
        }
    return record, {reviewer: sorted(ids) for reviewer, ids in selection.items()}, strata


def _validate_sample(sample: dict, document: Document, reported_issue_ids: set[str]) -> None:
    seed = sample.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("Spot-check sample seed must be an integer")
    expected_strata, expected_reviewers, strata = _selection(document, seed)
    if sample.get("strata") != expected_strata or sample.get("reviewer_unit_ids") != expected_reviewers:
        raise ValueError("Spot-check sample strata or reviewer assignments do not match the deterministic plan")
    selected = set.union(*(set(ids) for ids in expected_reviewers.values()))
    by_id = {unit.id: unit for units in strata.values() for unit in units}
    expected_items = {unit_id: _sample_mapping(by_id[unit_id]) for unit_id in selected}
    observed = {item.get("unit_id"): item for item in sample.get("samples", [])}
    if set(observed) != selected or len(observed) != len(sample.get("samples", [])):
        raise ValueError("Spot-check sample content is stale, incomplete, or modified")
    for unit_id, expected in expected_items.items():
        item = observed[unit_id]
        if any(item.get(key) != value for key, value in expected.items()):
            raise ValueError("Spot-check sample source mapping is stale or modified")
        sampled_hash = content_hash(item.get("target_text") or "")
        if item.get("target_hash") != sampled_hash:
            raise ValueError("Spot-check sample target hash does not match its target text")
        if unit_id not in reported_issue_ids and content_hash(by_id[unit_id].target_text or "") != sampled_hash:
            raise ValueError("Spot-check target changed after sampling without a reported defect")


def _round_dirs(root: Path) -> list[Path]:
    return sorted((path for path in root.glob("round-*")), key=lambda path: path.name)


def _run_directory(root: Path, active: dict) -> Path:
    run_id = active.get("run_id")
    try:
        if not isinstance(run_id, str) or str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except ValueError as error:
        raise ValueError("Spot-check run_id is invalid") from error
    return root / "runs" / run_id


def _family_closure(directory: Path, family_ids: set[str], document: Document, sample_by_id: dict[str, dict]) -> None:
    path = directory / "closure.json"
    if not path.exists():
        raise ValueError(f"Defect family closure is missing for {directory.name}")
    closure = read_json(path)
    sample = read_json(directory / "sample.json")
    _validate_family_closure_record(closure, sample, family_ids, document, sample_by_id)


def _validate_family_closure_record(
    closure: dict, sample: dict, family_ids: set[str], document: Document,
    sample_by_id: dict[str, dict],
) -> None:
    if closure.get("run_id") != sample.get("run_id") or closure.get("round") != sample.get("round"):
        raise ValueError("Defect closure evidence does not match its spot-check run and round")
    if closure.get("families") is None:
        raise ValueError("closure.json must contain a families array")
    units_by_id = {unit.id: unit for unit in document.units}
    for family_id in family_ids:
        matches = [family for family in closure["families"] if family.get("family_id") == family_id]
        if len(matches) != 1:
            raise ValueError(f"Defect family {family_id!r} needs exactly one closure record")
        family = matches[0]
        if not family.get("description", "").strip() or not family.get("audit_method", "").strip():
            raise ValueError(f"Defect family {family_id!r} needs a definition and book-wide audit method")
        scope = family.get("scope_strata")
        if not scope or any(name not in STRATA for name in scope):
            raise ValueError(f"Defect family {family_id!r} must declare valid scope_strata")
        audited = {unit.id for unit in document.units if _stratum(unit) in scope}
        results = family.get("audit_results", {})
        if set(results) != audited:
            raise ValueError(f"Defect family {family_id!r} audit_results must cover every matching unit in the book")
        if any(value not in {"clear", "fixed", "exception"} for value in results.values()):
            raise ValueError("Defect family audit results must be clear, fixed, or exception")
        fixes = family.get("fixes", {})
        exceptions = family.get("exceptions", {})
        if set(fixes) != {unit_id for unit_id, result in results.items() if result == "fixed"}:
            raise ValueError(f"Defect family {family_id!r} fix records must match every fixed audit result")
        if set(exceptions) != {unit_id for unit_id, result in results.items() if result == "exception"}:
            raise ValueError(f"Defect family {family_id!r} exception records must match every exception result")
        issue_units = set(family.get("reported_issue_unit_ids", []))
        if not issue_units or not issue_units.issubset(audited):
            raise ValueError(f"Defect family {family_id!r} must link the reported sample findings to its full-book audit")
        for unit_id in audited:
            result = results[unit_id]
            if result == "fixed":
                fix = fixes.get(unit_id, {})
                current = units_by_id[unit_id].target_text or ""
                current_hash = content_hash(current)
                if not fix.get("resolution") or fix.get("target_hash") != current_hash:
                    raise ValueError(f"Defect family {family_id!r} has an unverified fix for unit {unit_id}")
                if fix.get("baseline_target_hash") == current_hash:
                    raise ValueError(f"Defect family {family_id!r} did not change the target for unit {unit_id}")
                if unit_id in sample_by_id and fix.get("baseline_target_hash") != sample_by_id[unit_id]["target_hash"]:
                    raise ValueError(f"Defect family {family_id!r} baseline does not match the sampled target for unit {unit_id}")
            elif result == "exception" and not exceptions.get(unit_id, "").strip():
                raise ValueError(f"Defect family {family_id!r} needs a reason for exception {unit_id}")
        if any(results[unit_id] != "fixed" for unit_id in issue_units):
            raise ValueError(f"Defect family {family_id!r} must fix every reported sample defect")
        if family.get("closure_status") != "CLOSED":
            raise ValueError(f"Defect family {family_id!r} is not closed")


def _evaluate_round(directory: Path, expected_run_id: str, expected_number: int, document: Document) -> dict:
    sample_path = directory / "sample.json"
    review_path = directory / "reviews.json"
    if not sample_path.exists() or not review_path.exists():
        raise ValueError(f"Spot-check review evidence is incomplete in {directory}")
    sample = read_json(sample_path)
    reviews = read_json(review_path)
    if sample.get("run_id") != expected_run_id or reviews.get("run_id") != expected_run_id:
        raise ValueError("Spot-check evidence does not belong to the active current run")
    if sample.get("round") != expected_number or reviews.get("round") != expected_number or sample.get("seed") != reviews.get("seed"):
        raise ValueError("Spot-check round number or seed does not match its review evidence")
    if sample.get("target_confidence") != TARGET_CONFIDENCE or sample.get("defect_rate_threshold") != DEFECT_RATE:
        raise ValueError("Spot-check confidence assumptions do not match the active quality policy")
    if directory.name != f"round-{expected_number:04d}":
        raise ValueError("Spot-check round directory sequence is invalid")
    sample_by_id = {item["unit_id"]: item for item in sample["samples"]}
    reviewers = reviews.get("reviewers", [])
    slots = {reviewer.get("slot") for reviewer in reviewers}
    identities = [reviewer.get("reviewer_id", "").strip().casefold() for reviewer in reviewers]
    if len(reviewers) != len(REVIEWERS) or slots != set(REVIEWERS) or any(not identity for identity in identities) or len(set(identities)) != len(identities):
        raise ValueError("Spot-check evidence requires two distinct independent reviewers")
    issue_family_ids: set[str] = set()
    issue_units_by_family: dict[str, set[str]] = {}
    scores_summary = {}
    scoring_pass = True
    for reviewer in reviewers:
        slot = reviewer["slot"]
        expected_ids = set(sample.get("reviewer_unit_ids", {}).get(slot, []))
        scores = reviewer.get("scores", {})
        if set(scores) != expected_ids or not expected_ids:
            raise ValueError(f"Reviewer {slot} must score every assigned sample unit exactly once")
        numbers = list(scores.values())
        if any(not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 100 for score in numbers):
            raise ValueError(f"Reviewer {slot} scores must be numbers from 0 through 100")
        style_debt = reviewer.get("style_debt_count")
        if not isinstance(style_debt, int) or style_debt < 0:
            raise ValueError(f"Reviewer {slot} must report a non-negative style_debt_count")
        findings = reviewer.get("findings")
        if not isinstance(findings, list):
            raise ValueError(f"Reviewer {slot} findings must be an array")
        for finding in findings:
            unit_id = finding.get("unit_id")
            family_id = finding.get("family_id", "").strip()
            if unit_id not in expected_ids or not family_id or not finding.get("description", "").strip():
                raise ValueError(f"Reviewer {slot} findings must identify a sampled unit and defect family")
            issue_family_ids.add(family_id)
            issue_units_by_family.setdefault(family_id, set()).add(unit_id)
        average = sum(numbers) / len(numbers)
        lowest = min(numbers)
        scores_summary[slot] = {"reviewer_id": reviewer["reviewer_id"], "average": average, "lowest": lowest, "style_debt_count": style_debt}
        scoring_pass &= average >= 92 and lowest >= 88 and style_debt == 0
    reported_issue_ids = set().union(*issue_units_by_family.values()) if issue_units_by_family else set()
    if issue_family_ids:
        closure = read_json(directory / "closure.json")
        closure_ids = [family.get("family_id") for family in closure.get("families", [])]
        if len(closure_ids) != len(set(closure_ids)) or set(closure_ids) != issue_family_ids:
            raise ValueError("Defect closure must cover exactly the families reported by reviewers")
        for family_id in issue_family_ids:
            _family_closure(directory, {family_id}, document, sample_by_id)
        for family in closure["families"]:
            family_id = family.get("family_id")
            if family_id in issue_family_ids and set(family.get("reported_issue_unit_ids", [])) != issue_units_by_family[family_id]:
                raise ValueError(f"Defect family {family_id!r} reported_issue_unit_ids do not match reviewer findings")
        allowed_changes = reported_issue_ids | {
            unit_id for family in closure["families"] for unit_id, result in family.get("audit_results", {}).items() if result == "fixed"
        }
    else:
        allowed_changes = set()
    _validate_sample(sample, document, allowed_changes)
    status = "PASS" if scoring_pass and not issue_family_ids else "DEFECT_FOUND" if issue_family_ids else "FAIL"
    return {"round": expected_number, "seed": sample["seed"], "status": status, "issue_families": sorted(issue_family_ids), "scores": scores_summary}


def create_spotcheck_round(project: Path, document: Document, seed: int | None = None, new_run: bool = False) -> Path:
    validate_qa(project, document)
    root = project / "qa" / "spotcheck"
    active_path = root / "active.json"
    if active_path.exists() and not new_run:
        active = read_json(active_path)
    else:
        active = {"run_id": str(uuid.uuid4()), "planned_rounds": 4, "created_at": datetime.now(timezone.utc).isoformat()}
        write_json(active_path, active)
    run_dir = _run_directory(root, active)
    rounds = _round_dirs(run_dir)
    planned = active.get("planned_rounds", 4)
    if not isinstance(planned, int) or isinstance(planned, bool) or planned < 1 or len(rounds) >= planned:
        raise ValueError(f"Current spot-check run has reached its {planned}-round plan; start a new run to continue")
    previous = None
    used_seeds = set()
    for number, directory in enumerate(rounds, 1):
        previous = _evaluate_round(directory, active["run_id"], number, document)
        used_seeds.add(previous["seed"])
    if previous and previous["status"] == "DEFECT_FOUND":
        closure = read_json(rounds[-1] / "closure.json")
        if not closure.get("families"):
            raise ValueError("Close every detected defect family before sampling a new round")
    selected_seed = seed if seed is not None else secrets.randbits(32)
    if selected_seed in used_seeds:
        raise ValueError("Each spot-check round in the current run must use a new seed")
    strata_record, reviewer_unit_ids, strata = _selection(document, selected_seed)
    selected_ids = set.union(*(set(ids) for ids in reviewer_unit_ids.values()))
    by_id = {unit.id: unit for units in strata.values() for unit in units}
    sample_items = []
    for unit_id in sorted(selected_ids):
        unit = by_id[unit_id]
        sample_items.append({
            **_sample_mapping(unit), "target_text": unit.target_text,
            "target_hash": content_hash(unit.target_text or ""),
        })
    number = len(rounds) + 1
    directory = run_dir / f"round-{number:04d}"
    directory.mkdir(parents=True)
    round_id = str(uuid.uuid4())
    write_json(directory / "sample.json", {
        "run_id": active["run_id"], "round_id": round_id, "round": number, "seed": selected_seed,
        "target_confidence": TARGET_CONFIDENCE, "defect_rate_threshold": DEFECT_RATE,
        "strata": strata_record, "reviewer_unit_ids": reviewer_unit_ids, "samples": sample_items,
    })
    write_json(directory / "review-template.json", {
        "run_id": active["run_id"], "round_id": round_id, "round": number, "seed": selected_seed,
        "reviewers": [
            {"slot": reviewer, "reviewer_id": "", "scores": {unit_id: None for unit_id in reviewer_unit_ids[reviewer]}, "style_debt_count": None, "findings": None}
            for reviewer in REVIEWERS
        ],
    })
    write_json(directory / "closure-template.json", {"run_id": active["run_id"], "round_id": round_id, "round": number, "families": []})
    return directory


def _defect_knowledge_path(project: Path) -> Path:
    return project.resolve(strict=True).parent / ".knowledge" / "en_zh_hans" / "defect_families.md"


def _require_current_document(project: Path, document: Document) -> None:
    path = project / "work" / "document.json"
    if not path.is_file() or Document.from_dict(read_json(path)).to_dict() != document.to_dict():
        raise ValueError("Spot-check document is stale; reload the canonical document before continuing")


def _require_git_ignored(project: Path, path: Path) -> None:
    if not shutil.which("git"):
        return
    root = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if root.returncode:
        return
    relative = path.relative_to(Path(root.stdout.strip()))
    ignored = subprocess.run(
        ["git", "-C", root.stdout.strip(), "check-ignore", "--quiet", "--no-index", "--", relative.as_posix()]
    )
    if ignored.returncode == 1:
        raise ValueError(f"Private defect-family knowledge is not ignored by Git: {path}")
    ignored.check_returncode()


def _quality_rules(project: Path) -> list[str]:
    path = _defect_knowledge_path(project)
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]


def create_spotcheck_tasks(
    project: Path, document: Document, directory: Path, reviewer_a_id: str,
    reviewer_b_id: str, *, model: str = DEFAULT_AGENT_SPOTCHECK_MODEL,
) -> dict[str, dict]:
    if not reviewer_a_id.strip() or not reviewer_b_id.strip() or reviewer_a_id.casefold() == reviewer_b_id.casefold():
        raise ValueError("reviewer_a_id and reviewer_b_id must identify two distinct reviewers")
    sample = read_json(directory / "sample.json")
    _require_current_document(project, document)
    _validate_sample(sample, document, set())
    language = load_language_package(document.source_language, document.target_language)
    reviewer_ids = {REVIEWERS[0]: reviewer_a_id, REVIEWERS[1]: reviewer_b_id}
    tasks = {}
    for slot, reviewer_id in reviewer_ids.items():
        assigned_ids = sample["reviewer_unit_ids"][slot]
        assigned_id_set = set(assigned_ids)
        assigned_samples = [item for item in sample["samples"] if item["unit_id"] in assigned_id_set]
        task = {
            "schema_version": 1, "task_type": "spotcheck", "task_id": str(uuid.uuid4()),
            "run_id": sample["run_id"], "round_id": sample["round_id"],
            "round": sample["round"], "seed": sample["seed"], "reviewer_id": reviewer_id,
            "reviewer_slot": slot, "requested_backend": 1, "actual_backend": 1, "model": model,
            "assigned_samples": assigned_samples,
            "source_hashes": {item["unit_id"]: item["source_hash"] for item in assigned_samples},
            "target_hashes": {item["unit_id"]: item["target_hash"] for item in assigned_samples},
            "rubric": language["review_rubric"] + (
                "\n\nScore every assigned target from 0 to 100. Report style_debt_count and concrete findings. "
                "Each finding must name a reusable family and category; quality categories require a general reusable_rule. "
                "Do not edit targets or any project state. Return only SpotcheckEvidence."
            ),
            "quality_rules": _quality_rules(project),
            "score_schema": {
                "score_min": 0, "score_max": 100, "average_min_for_pass": 92,
                "lowest_min_for_pass": 88, "style_debt_count_for_pass": 0,
                "finding_categories": sorted(FINDING_CATEGORIES),
            },
            "required_evidence_schema": SPOTCHECK_EVIDENCE_SCHEMA,
        }
        tasks[slot] = task
    return tasks


def _validate_spotcheck_task(task: dict, sample: dict, document: Document) -> None:
    expected = set(sample["reviewer_unit_ids"].get(task.get("reviewer_slot"), []))
    assigned = task.get("assigned_samples")
    if (
        task.get("schema_version") != 1 or task.get("task_type") != "spotcheck"
        or not isinstance(task.get("task_id"), str) or not task["task_id"].strip()
        or task.get("run_id") != sample["run_id"] or task.get("round_id") != sample["round_id"]
        or task.get("round") != sample["round"] or task.get("seed") != sample["seed"]
        or task.get("reviewer_slot") not in REVIEWERS or not task.get("reviewer_id")
        or not isinstance(assigned, list) or {item.get("unit_id") for item in assigned} != expected
        or len(assigned) != len(expected) or not expected
    ):
        raise ValueError("SpotcheckTask identity or assigned sample set is invalid")
    sample_by_id = {item["unit_id"]: item for item in sample["samples"]}
    if any(item != sample_by_id.get(item.get("unit_id")) for item in assigned):
        raise ValueError("SpotcheckTask sample mapping does not match the canonical sample")
    if task.get("source_hashes") != {item["unit_id"]: item["source_hash"] for item in assigned}:
        raise ValueError("SpotcheckTask source hashes do not match assigned samples")
    if task.get("target_hashes") != {item["unit_id"]: item["target_hash"] for item in assigned}:
        raise ValueError("SpotcheckTask target hashes do not match assigned samples")
    if (
        not isinstance(task.get("rubric"), str) or not isinstance(task.get("quality_rules"), list)
        or task.get("required_evidence_schema") != SPOTCHECK_EVIDENCE_SCHEMA
    ):
        raise ValueError("SpotcheckTask rubric or quality rules are invalid")
    if task.get("score_schema", {}).get("score_min") != 0 or task["score_schema"].get("score_max") != 100:
        raise ValueError("SpotcheckTask score schema is invalid")
    _validate_sample(sample, document, set())


def validate_spotcheck_evidence(task: dict, evidence: dict) -> None:
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version", "task_id", "round_id", "reviewer_id", "scores", "findings", "style_debt_count",
    }:
        raise ValueError("SpotcheckEvidence schema is invalid")
    for key in ("task_id", "round_id", "reviewer_id"):
        if evidence.get(key) != task[key]:
            raise ValueError(f"SpotcheckEvidence {key} does not match its task")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1:
        raise ValueError("Unsupported SpotcheckEvidence schema")
    expected_ids = {item["unit_id"] for item in task["assigned_samples"]}
    scores = evidence["scores"]
    if not isinstance(scores, dict) or set(scores) != expected_ids:
        raise ValueError("SpotcheckEvidence must score every assigned sample exactly once")
    if any(not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 100 for score in scores.values()):
        raise ValueError("SpotcheckEvidence scores must be finite numbers from 0 through 100")
    debt = evidence["style_debt_count"]
    if not isinstance(debt, int) or isinstance(debt, bool) or debt < 0:
        raise ValueError("SpotcheckEvidence style_debt_count must be a non-negative integer")
    if not isinstance(evidence["findings"], list):
        raise ValueError("SpotcheckEvidence findings must be an array")
    required = {"unit_id", "family_id", "category", "description", "reusable_rule"}
    for finding in evidence["findings"]:
        if (
            not isinstance(finding, dict) or set(finding) != required
            or finding.get("unit_id") not in expected_ids
            or not isinstance(finding.get("family_id"), str) or not finding["family_id"].strip()
            or not isinstance(finding.get("category"), str) or finding["category"] not in FINDING_CATEGORIES
            or not isinstance(finding.get("description"), str) or not finding["description"].strip()
            or not isinstance(finding.get("reusable_rule"), str)
        ):
            raise ValueError("SpotcheckEvidence finding is invalid or outside its assigned samples")
        if finding["category"] in QUALITY_FAMILY_CATEGORIES and not finding["reusable_rule"].strip():
            raise ValueError("Quality defect findings need a reusable_rule for family closure")


def validate_spotcheck_evidence_current(
    project: Path, document: Document, directory: Path, task: dict, evidence: dict,
) -> None:
    _require_current_document(project, document)
    sample = read_json(directory / "sample.json")
    _validate_spotcheck_task(task, sample, document)
    validate_spotcheck_evidence(task, evidence)


def accept_spotcheck_evidence_pair(
    project: Path, document: Document, directory: Path, tasks: dict[str, dict],
    evidence_by_slot: dict[str, dict],
) -> None:
    if set(tasks) != set(REVIEWERS) or set(evidence_by_slot) != set(REVIEWERS):
        raise ValueError("Spotcheck acceptance requires both independent reviewer tasks and evidence")
    if tasks[REVIEWERS[0]]["reviewer_id"].casefold() == tasks[REVIEWERS[1]]["reviewer_id"].casefold():
        raise ValueError("reviewer_a_id and reviewer_b_id must be distinct")
    for slot in REVIEWERS:
        validate_spotcheck_evidence_current(project, document, directory, tasks[slot], evidence_by_slot[slot])
    tasks_dir = directory / "tasks"
    evidence_dir = directory / "evidence"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for slot in REVIEWERS:
        write_json(tasks_dir / f"{slot}.json", tasks[slot])
        write_json(evidence_dir / f"{slot}.json", evidence_by_slot[slot])


def _family_requirements(reviewers: list[dict]) -> dict[str, dict]:
    families = {}
    for reviewer in reviewers:
        for finding in reviewer["findings"]:
            entry = families.setdefault(finding["family_id"], {
                "category": finding["category"], "description": set(),
                "reported_issue_unit_ids": set(), "reusable_rules": set(),
            })
            if entry["category"] != finding["category"]:
                raise ValueError(f"Defect family {finding['family_id']!r} has inconsistent categories")
            entry["description"].add(finding["description"])
            entry["reported_issue_unit_ids"].add(finding["unit_id"])
            if finding["reusable_rule"].strip():
                entry["reusable_rules"].add(finding["reusable_rule"])
    return {
        family_id: {
            "family_id": family_id, "category": value["category"],
            "description": " ".join(sorted(value["description"])),
            "reusable_rule": sorted(value["reusable_rules"])[0] if value["reusable_rules"] else "",
            "proposed_reusable_rules": sorted(value["reusable_rules"]), "scope_strata": list(STRATA),
            "reported_issue_unit_ids": sorted(value["reported_issue_unit_ids"]),
            "audit_method": "", "audit_results": {}, "fixes": {}, "exceptions": {},
            "closure_status": "OPEN",
        }
        for family_id, value in families.items()
    }


def finalize_spotcheck_round(project: Path, document: Document, directory: Path) -> dict:
    _require_current_document(project, document)
    sample = read_json(directory / "sample.json")
    reviewers = []
    for slot in REVIEWERS:
        task = read_json(directory / "tasks" / f"{slot}.json")
        evidence = read_json(directory / "evidence" / f"{slot}.json")
        validate_spotcheck_evidence_current(project, document, directory, task, evidence)
        reviewers.append({
            "slot": slot, "reviewer_id": task["reviewer_id"], "scores": evidence["scores"],
            "style_debt_count": evidence["style_debt_count"], "findings": evidence["findings"],
        })
    review_record = {
        "run_id": sample["run_id"], "round_id": sample["round_id"],
        "round": sample["round"], "seed": sample["seed"], "reviewers": reviewers,
        "evidence_task_ids": [read_json(directory / "tasks" / f"{slot}.json")["task_id"] for slot in REVIEWERS],
    }
    write_json(directory / "reviews.json", review_record)
    families = _family_requirements(reviewers)
    if families:
        write_json(directory / "closure-required.json", {
            "run_id": sample["run_id"], "round_id": sample["round_id"],
            "round": sample["round"], "families": list(families.values()),
        })
        return {"round": sample["round"], "seed": sample["seed"], "round_status": "DEFECT_FOUND", "issue_families": sorted(families)}
    root = project / "qa" / "spotcheck"
    active = read_json(root / "active.json")
    return _evaluate_round(directory, active["run_id"], sample["round"], document)


def run_agent_spotcheck_round(
    project: Path, document: Document, seed: int | None, reviewer_a_id: str, reviewer_b_id: str,
    executor_a: SpotcheckExecutor, executor_b: SpotcheckExecutor,
    *, model: str = DEFAULT_AGENT_SPOTCHECK_MODEL, new_run: bool = False,
) -> dict:
    if reviewer_a_id.casefold() == reviewer_b_id.casefold():
        raise ValueError("reviewer_a_id and reviewer_b_id must be distinct")
    if executor_a is executor_b:
        raise ValueError("Spotcheck reviewers must use two independent executor instances")
    worker_a = getattr(executor_a, "worker_id", None)
    worker_b = getattr(executor_b, "worker_id", None)
    if (
        not isinstance(worker_a, str) or not worker_a.strip()
        or not isinstance(worker_b, str) or not worker_b.strip()
        or worker_a.casefold() == worker_b.casefold()
    ):
        raise ValueError("Spotcheck reviewers must use two independent workers")
    _require_current_document(project, document)
    directory = create_spotcheck_round(project, document, seed, new_run=new_run)
    completed = False
    try:
        tasks = create_spotcheck_tasks(project, document, directory, reviewer_a_id, reviewer_b_id, model=model)
        evidence_by_slot = {}
        for slot, executor in zip(REVIEWERS, (executor_a, executor_b), strict=True):
            task = tasks[slot]
            evidence = executor.execute(task, model=model)
            validate_spotcheck_evidence_current(project, document, directory, task, evidence)
            evidence_by_slot[slot] = evidence
        accept_spotcheck_evidence_pair(project, document, directory, tasks, evidence_by_slot)
        result = finalize_spotcheck_round(project, document, directory)
        completed = True
        return result
    finally:
        if not completed:
            shutil.rmtree(directory)


def close_spotcheck_defect_families(
    project: Path, document: Document, directory: Path, closure: dict,
) -> dict:
    _require_current_document(project, document)
    sample = read_json(directory / "sample.json")
    required_path = directory / "closure-required.json"
    if not required_path.exists():
        raise ValueError("No Agent spot-check defect family is waiting for closure")
    required = read_json(required_path)
    if closure.get("run_id") != sample["run_id"] or closure.get("round_id") != sample["round_id"] or closure.get("round") != sample["round"]:
        raise ValueError("Defect closure does not match its spot-check round")
    expected = {family["family_id"]: family for family in required["families"]}
    observed = {family.get("family_id"): family for family in closure.get("families", [])}
    if set(observed) != set(expected) or len(observed) != len(closure.get("families", [])):
        raise ValueError("Defect closure must cover every reported family exactly once")
    for family_id, value in expected.items():
        family = observed[family_id]
        if family.get("category") != value["category"]:
            raise ValueError(f"Defect family {family_id!r} category changed")
        if family["category"] in QUALITY_FAMILY_CATEGORIES and not family.get("reusable_rule", "").strip():
            raise ValueError(f"Defect family {family_id!r} requires a reusable quality rule")
        if family.get("scope_strata") != list(STRATA):
            raise ValueError(f"Defect family {family_id!r} must audit every spot-check stratum across the book")
        if set(family.get("reported_issue_unit_ids", [])) != set(value["reported_issue_unit_ids"]):
            raise ValueError(f"Defect family {family_id!r} reported issues do not match reviewer evidence")
    sample_by_id = {item["unit_id"]: item for item in sample["samples"]}
    _validate_family_closure_record(closure, sample, set(expected), document, sample_by_id)
    knowledge_path = _defect_knowledge_path(project)
    if any(value["category"] in QUALITY_FAMILY_CATEGORIES for value in expected.values()):
        _require_git_ignored(project, knowledge_path)
    closure_path = directory / "closure.json"
    write_json(closure_path, closure)
    try:
        result = _evaluate_round(directory, sample["run_id"], sample["round"], document)
    except BaseException:
        closure_path.unlink(missing_ok=True)
        raise
    if result["status"] != "DEFECT_FOUND":
        closure_path.unlink(missing_ok=True)
        raise ValueError("Closed defect family did not validate as a defect round")
    additions = []
    for family in closure["families"]:
        if family["category"] in QUALITY_FAMILY_CATEGORIES:
            additions.append(f"- **{family['category']}**: {' '.join(family['reusable_rule'].split())}")
    if additions:
        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        existing = knowledge_path.read_text(encoding="utf-8").splitlines() if knowledge_path.exists() else ["# Validated en → zh-Hans spot-check defect rules", ""]
        for line in additions:
            if line not in existing:
                existing.append(line)
        knowledge_path.write_text("\n".join(existing).rstrip() + "\n", encoding="utf-8")
    return {"closed_families": sorted(expected), "round_status": result["status"], "quality_rules_added": len(additions)}


def validate_spotcheck(project: Path, document: Document) -> dict:
    root = project / "qa" / "spotcheck"
    active_path = root / "active.json"
    if not active_path.exists():
        raise ValueError("No stratified spot-check run exists; call start_host_spotcheck first")
    active = read_json(active_path)
    run_dir = _run_directory(root, active)
    rounds = _round_dirs(run_dir)
    planned = active.get("planned_rounds", 4)
    if not isinstance(planned, int) or isinstance(planned, bool) or planned < 1 or len(rounds) > planned:
        raise ValueError("Current spot-check evidence exceeds its planned round count")
    results = []
    seeds = set()
    for number, directory in enumerate(rounds, 1):
        result = _evaluate_round(directory, active["run_id"], number, document)
        if result["seed"] in seeds:
            raise ValueError("Spot-check rounds must use distinct seeds")
        seeds.add(result["seed"])
        if result["status"] == "DEFECT_FOUND" and number == len(rounds):
            raise ValueError("A defect-family finding requires a later new-seed review round")
        results.append(result)
    if len(rounds) < 2:
        raise ValueError("Current-run validation requires two consecutive PASS rounds")
    if [result["status"] for result in results[-2:]] != ["PASS", "PASS"]:
        raise ValueError("The latest two consecutive current-run spot-check rounds must PASS")
    return {"run_id": active["run_id"], "status": "PASS", "rounds": results}
