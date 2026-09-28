from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from private_translate.core.model import Document, Section, TranslationUnit, content_hash
from private_translate.core.spotcheck import (
    DEFAULT_AGENT_SPOTCHECK_MODEL,
    STRATA,
    close_spotcheck_defect_families,
    create_spotcheck_round,
    create_spotcheck_tasks,
    run_agent_spotcheck_round,
    validate_spotcheck,
    validate_spotcheck_evidence_current,
)
from private_translate.core.qa import _target_hash
from private_translate.core.state import read_json, write_json


class FakeSpotcheckExecutor:
    def __init__(self, worker_id, *, finding=False, score=96, before_execute=None):
        self.worker_id = worker_id
        self.finding = finding
        self.score = score
        self.before_execute = before_execute
        self.tasks = []
        self.evidence = []

    def execute(self, task, *, model):
        if self.before_execute:
            self.before_execute(task)
        self.tasks.append(json.loads(json.dumps(task)))
        self.models = getattr(self, "models", []) + [model]
        findings = []
        if self.finding:
            sample = task["assigned_samples"][0]
            findings = [{
                "unit_id": sample["unit_id"], "family_id": "source-syntax-residue",
                "category": "source_syntax", "description": "The target preserves English word order.",
                "reusable_rule": "Rebuild information order as natural Simplified Chinese instead of following English syntax.",
            }]
            self.finding = False
        evidence = {
            "schema_version": 1, "task_id": task["task_id"], "round_id": task["round_id"],
            "reviewer_id": task["reviewer_id"],
            "scores": {sample["unit_id"]: self.score for sample in task["assigned_samples"]},
            "findings": findings, "style_debt_count": 0,
        }
        self.evidence.append(evidence)
        return evidence


class AgentSpotcheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.root = self.repo / ".test-tmp" / f"agent-spotcheck-{uuid.uuid4().hex}"
        self.project = self.root / "workspace"
        (self.project / "qa").mkdir(parents=True)
        (self.project / "work").mkdir()
        self.document = Document(
            "markdown", "en", "zh-Hans", {}, [Section("chapter-1", "Chapter", {})],
            [
                self._unit("The morning light spread across the quiet harbour.", "清晨的光线扩散横跨安静的港口。", 0),
                self._unit("Mara waited beside the old stone wall.", "玛拉在古老的石墙旁等候。", 1),
            ],
        )
        self._save_current()
        self.addCleanup(self._cleanup)

    def _unit(self, source, target, index):
        unit = TranslationUnit.create(
            section_id="chapter-1", type="paragraph", source_text=source, translatable=True,
            source_language="en", target_language="zh-Hans", locator={"paragraph": index},
        )
        unit.target_text = target
        unit.translation_status = "translated"
        unit.qa_status = "passed"
        unit.last_qa_target_hash = content_hash(target)
        return unit

    def _save_current(self):
        translated = [unit for unit in self.document.units if unit.translatable and unit.target_text is not None]
        write_json(self.project / "work" / "document.json", self.document.to_dict())
        write_json(self.project / "qa" / "book-consistency.json", {
            "status": "PASS", "target_hash": _target_hash(translated),
        })

    def _cleanup(self):
        if self.root.exists():
            self.assertTrue(self.root.resolve().is_relative_to((self.repo / ".test-tmp").resolve()))
            shutil.rmtree(self.root)

    def test_two_reviewers_are_distinct_and_b_gets_no_a_evidence(self):
        target_before = {unit.id: unit.target_text for unit in self.document.units}
        def assert_a_evidence_is_not_on_disk(task):
            directory = self.project / "qa" / "spotcheck" / "runs" / task["run_id"] / f"round-{task['round']:04d}"
            if (directory / "tasks").exists() or (directory / "evidence").exists():
                raise AssertionError("reviewer A task/evidence was persisted before reviewer B finished")

        a = FakeSpotcheckExecutor("worker-a")
        b = FakeSpotcheckExecutor("worker-b", before_execute=assert_a_evidence_is_not_on_disk)
        result = run_agent_spotcheck_round(
            self.project, self.document, 101, "agent:reviewer-a", "agent:reviewer-b", a, b,
        )
        self.assertEqual(result["status"], "PASS")
        task_a, task_b = a.tasks[0], b.tasks[0]
        self.assertNotEqual(task_a["reviewer_id"], task_b["reviewer_id"])
        self.assertNotEqual(task_a["task_id"], task_b["task_id"])
        self.assertFalse(set(task_b) & {"reviewer_a_evidence", "reviewer_a_scores", "reviewer_a_findings"})
        self.assertNotIn(a.evidence[0], task_b.values())
        self.assertEqual({item["unit_id"] for item in task_b["assigned_samples"]}, set(task_b["target_hashes"]))
        directory = self.project / "qa" / "spotcheck" / "runs" / task_b["run_id"] / "round-0001"
        self.assertTrue((directory / "evidence" / "reviewer_1.json").is_file())
        self.assertTrue((directory / "evidence" / "reviewer_2.json").is_file())
        self.assertEqual(a.models + b.models, [DEFAULT_AGENT_SPOTCHECK_MODEL] * 2)
        saved = Document.from_dict(read_json(self.project / "work" / "document.json"))
        self.assertEqual(target_before, {unit.id: unit.target_text for unit in saved.units})
        self.assertTrue(all(unit.qa_status == "passed" for unit in saved.units))
        self.assertFalse((self.project / "state" / "state.json").exists())
        self.assertNotIn("status", read_json(self.project / "qa" / "spotcheck" / "active.json"))

    def test_same_reviewer_or_worker_is_rejected(self):
        worker = FakeSpotcheckExecutor("one-worker")
        with self.assertRaisesRegex(ValueError, "distinct"):
            run_agent_spotcheck_round(self.project, self.document, 101, "same", "same", worker, FakeSpotcheckExecutor("b"))
        with self.assertRaisesRegex(ValueError, "independent executor"):
            run_agent_spotcheck_round(self.project, self.document, 101, "a", "b", worker, worker)
        with self.assertRaisesRegex(ValueError, "independent workers"):
            run_agent_spotcheck_round(self.project, self.document, 101, "a", "b", worker, FakeSpotcheckExecutor("one-worker"))

    def test_worker_identity_is_required(self):
        class AnonymousExecutor:
            def execute(self, task, *, model):
                raise AssertionError("identity validation must happen before dispatch")

        with self.assertRaisesRegex(ValueError, "independent workers"):
            run_agent_spotcheck_round(
                self.project, self.document, 101, "a", "b", AnonymousExecutor(), FakeSpotcheckExecutor("worker-b"),
            )

    def test_invalid_evidence_is_rejected_without_partial_persistence(self):
        class InvalidExecutor(FakeSpotcheckExecutor):
            def execute(self, task, *, model):
                evidence = super().execute(task, model=model)
                evidence["scores"] = [{"unit_id": next(iter(evidence["scores"])), "score": 96}]
                return evidence

        targets_before = {unit.id: unit.target_text for unit in self.document.units}
        with self.assertRaisesRegex(ValueError, "score every assigned sample"):
            run_agent_spotcheck_round(
                self.project, self.document, 909, "a", "b", InvalidExecutor("worker-a"), FakeSpotcheckExecutor("worker-b"),
            )
        run_root = self.project / "qa" / "spotcheck" / "runs"
        run_id = read_json(self.project / "qa" / "spotcheck" / "active.json")["run_id"]
        self.assertEqual(list((run_root / run_id).glob("round-*")), [])
        saved = Document.from_dict(read_json(self.project / "work" / "document.json"))
        self.assertEqual(targets_before, {unit.id: unit.target_text for unit in saved.units})

    def test_spotcheck_evidence_rejects_stale_sample_target_hash(self):
        directory = create_spotcheck_round(self.project, self.document, 303)
        tasks = create_spotcheck_tasks(self.project, self.document, directory, "a", "b")
        task = tasks["reviewer_1"]
        sample = task["assigned_samples"][0]
        evidence = {
            "schema_version": 1, "task_id": task["task_id"], "round_id": task["round_id"],
            "reviewer_id": task["reviewer_id"], "scores": {item["unit_id"]: 96 for item in task["assigned_samples"]},
            "findings": [], "style_debt_count": 0,
        }
        unit = next(unit for unit in self.document.units if unit.id == sample["unit_id"])
        unit.target_text = "被改变的 target。"
        unit.last_qa_target_hash = content_hash(unit.target_text)
        self._save_current()
        canonical = Document.from_dict(read_json(self.project / "work" / "document.json"))
        with self.assertRaisesRegex(ValueError, "changed after sampling"):
            validate_spotcheck_evidence_current(self.project, canonical, directory, task, evidence)

    def test_thresholds_stay_at_average_92_minimum_88_zero_style_debt(self):
        a, b = FakeSpotcheckExecutor("worker-a", score=90), FakeSpotcheckExecutor("worker-b", score=92)
        result = run_agent_spotcheck_round(self.project, self.document, 404, "a", "b", a, b)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["scores"]["reviewer_1"]["average"], 90)

    def test_defect_requires_full_book_closure_and_new_seed_before_two_passes(self):
        a, b = FakeSpotcheckExecutor("worker-a", finding=True), FakeSpotcheckExecutor("worker-b")
        defect = run_agent_spotcheck_round(self.project, self.document, 505, "a", "b", a, b)
        self.assertEqual(defect["round_status"], "DEFECT_FOUND")
        first_dir = next((self.project / "qa" / "spotcheck" / "runs").glob("*/round-0001"))
        sample = read_json(first_dir / "sample.json")
        reviews = read_json(first_dir / "reviews.json")["reviewers"]
        issue_ids = {finding["unit_id"] for reviewer in reviews for finding in reviewer["findings"]}
        family = read_json(first_dir / "closure-required.json")["families"][0]
        issue_id = next(iter(issue_ids))
        unit = next(unit for unit in self.document.units if unit.id == issue_id)
        baseline_hash = next(item["target_hash"] for item in sample["samples"] if item["unit_id"] == issue_id)
        unit.target_text = "晨光洒向安静的港湾。"
        unit.last_qa_target_hash = content_hash(unit.target_text)
        self._save_current()
        closure_family = {
            **family, "audit_method": "Audited every paragraph, table, figure, formula and caption or note in the book.",
            "audit_results": {candidate.id: ("fixed" if candidate.id == issue_id else "clear") for candidate in self.document.units},
            "fixes": {issue_id: {
                "baseline_target_hash": baseline_hash, "target_hash": content_hash(unit.target_text),
                "resolution": "Reordered the clause into natural Simplified Chinese.",
            }}, "exceptions": {}, "closure_status": "CLOSED",
        }
        closure = {"run_id": sample["run_id"], "round_id": sample["round_id"], "round": 1, "families": [closure_family]}
        unresolved = json.loads(json.dumps(closure))
        unresolved_family = unresolved["families"][0]
        unresolved_family["audit_results"][issue_id] = "exception"
        unresolved_family["fixes"] = {}
        unresolved_family["exceptions"] = {issue_id: "Leave the confirmed issue unresolved."}
        with self.assertRaisesRegex(ValueError, "must fix every reported sample defect"):
            close_spotcheck_defect_families(self.project, self.document, first_dir, unresolved)
        closed = close_spotcheck_defect_families(self.project, self.document, first_dir, closure)
        self.assertEqual(closed["quality_rules_added"], 1)
        knowledge = self.project.parent / ".knowledge" / "en_zh_hans" / "defect_families.md"
        self.assertIn("Rebuild information order", knowledge.read_text(encoding="utf-8"))

        third = run_agent_spotcheck_round(self.project, self.document, 606, "a", "b", FakeSpotcheckExecutor("worker-a"), FakeSpotcheckExecutor("worker-b"))
        fourth = run_agent_spotcheck_round(self.project, self.document, 707, "a", "b", FakeSpotcheckExecutor("worker-a"), FakeSpotcheckExecutor("worker-b"))
        self.assertEqual((third["status"], fourth["status"]), ("PASS", "PASS"))
        result = validate_spotcheck(self.project, self.document)
        self.assertEqual([round_["status"] for round_ in result["rounds"]], ["DEFECT_FOUND", "PASS", "PASS"])
        self.assertEqual([round_["seed"] for round_ in result["rounds"]], [505, 606, 707])


if __name__ == "__main__":
    unittest.main()
