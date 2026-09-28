from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from private_translate.core.execution import (
    PatchConflict,
    create_review_task,
    review_target_digest,
    validate_review_evidence_current,
)
from private_translate.core.language import load_language_package
from private_translate.core.model import Document
from private_translate.core.pipeline import DEFAULT_AGENT_REVIEW_MODEL, run_project
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.qa import run_chapter_qa_agent
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter


class FakeProvider:
    capabilities = ProviderCapabilities("fake", "review-test", 2000, 400, True, False)

    def __init__(self):
        self.translation_calls = 0
        self.review_calls = 0

    def translate(self, units, context, glossary, config):
        self.translation_calls += 1
        return {unit.id: "API译文：" + unit.source_text for unit in units}

    def review(self, source, target, context, rubric, config):
        self.review_calls += 1
        return []


class FakeTranslationExecutor:
    def __init__(self):
        self.tasks = []
        self.models = []

    def execute(self, task, *, model):
        self.tasks.append(json.loads(json.dumps(task, ensure_ascii=False)))
        self.models.append(model)
        issues = task["context_capsule"].get("batch", {}).get("review_issues", [])
        translations = []
        for unit_id in task["required_unit_ids"]:
            unit = next(unit for unit in task["units"] if unit["id"] == unit_id)
            translations.append({
                "unit_id": unit_id, "source_hash": unit["source_hash"],
                "target_text": "灯塔没有亮。" if issues else "Agent初译：" + unit["source_text"],
            })
        return {
            "schema_version": 1, "patch_id": str(uuid.uuid4()), "task_id": task["task_id"],
            "task_type": "translation", "chapter_id": task["chapter_id"], "owner_id": task["owner_id"],
            "base_document_revision": task["base_document_revision"],
            "base_chapter_digest": task["base_chapter_digest"], "translations": translations,
        }


class FakeReviewExecutor:
    def __init__(self, issue_once=False):
        self.issue_once = issue_once
        self.issued = False
        self.tasks = []
        self.models = []

    def execute(self, task, *, model):
        self.tasks.append(json.loads(json.dumps(task, ensure_ascii=False)))
        self.models.append(model)
        issues = []
        if self.issue_once and not self.issued:
            self.issued = True
            issues = [{
                "unit_id": task["target_units"][0]["unit_id"], "category": "fidelity",
                "severity": "major", "description": "The negation is missing.",
                "suggested_fix": "Preserve the source negation.",
            }]
        return {
            "schema_version": 1, "task_id": task["task_id"], "chapter_id": task["chapter_id"],
            "reviewer_id": task["reviewer_id"], "translation_owner_id": task["translation_owner_id"],
            "base_chapter_digest": task["base_chapter_digest"], "target_digest": task["target_digest"],
            "issues": issues, "summary": "Independent chapter review completed.",
        }


class AgentReviewTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.language = load_language_package("en", "zh-Hans")
        self.temp_root = self.repo / "projects" / f".test-review-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.assertTrue(self.temp_root.resolve().is_relative_to((self.repo / "projects").resolve()))
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def project(self, text="The lighthouse was not burning."):
        source = self.temp_root / "book.md"
        source.write_text(text, encoding="utf-8")
        project = create_project(source, self.temp_root / "workspaces")
        document = MarkdownAdapter().import_file(project / "source" / "book.md", "en", "zh-Hans")
        write_json(project / "work" / "document.json", document.to_dict())
        return project, document

    def evidence(self, task, issues=None):
        return {
            "schema_version": 1, "task_id": task["task_id"], "chapter_id": task["chapter_id"],
            "reviewer_id": task["reviewer_id"], "translation_owner_id": task["translation_owner_id"],
            "base_chapter_digest": task["base_chapter_digest"], "target_digest": task["target_digest"],
            "issues": issues or [], "summary": "Reviewed the supplied chapter batch.",
        }

    def test_reviewer_cannot_equal_translation_owner(self):
        project, document = self.project()
        unit = next(row for row in document.units if row.translatable)
        unit.target_text = "灯塔没有亮。"
        unit.translation_status = "translated"
        with self.assertRaisesRegex(ValueError, "reviewer_id must differ"):
            create_review_task(document, unit.section_id, "agent:translator", "agent:translator", "Review all quality dimensions.")

    def test_review_target_digest_must_match_current_target(self):
        project, document = self.project()
        unit = next(row for row in document.units if row.translatable)
        unit.target_text = "灯塔没有亮。"
        unit.translation_status = "translated"
        write_json(project / "work" / "document.json", document.to_dict())
        task = create_review_task(document, unit.section_id, "agent:reviewer", "agent:translator", "Review all quality dimensions.")
        evidence = self.evidence(task)
        self.assertEqual(review_target_digest(task["target_units"]), task["target_digest"])
        validate_review_evidence_current(project, evidence, task)
        unit.target_text = "灯塔亮着。"
        write_json(project / "work" / "document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW"):
            validate_review_evidence_current(project, evidence, task)

    def test_review_after_target_change_becomes_stale(self):
        project, document = self.project()
        unit = next(row for row in document.units if row.translatable)
        unit.target_text = "灯塔没有亮。"
        unit.translation_status = "translated"
        write_json(project / "work" / "document.json", document.to_dict())
        task = create_review_task(document, unit.section_id, "agent:reviewer", "agent:translator", "Review all quality dimensions.")
        evidence = self.evidence(task)
        unit.target_text = "灯塔亮着。"
        write_json(project / "work" / "document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW_TASK|STALE_REVIEW_EVIDENCE"):
            validate_review_evidence_current(project, evidence, task)

    def test_issue_fix_requires_new_full_review_then_passes(self):
        text = "\n\n".join(
            f"In the evening, lantern {index} glowed beside the narrow stone bridge while the river moved quietly under the old arches."
            for index in range(1, 13)
        )
        project, document = self.project(text)
        provider, translator, reviewer = FakeProvider(), FakeTranslationExecutor(), FakeReviewExecutor(issue_once=True)
        run_project(
            project, provider=provider, translation_backend=1, agent_executor=translator,
            review_backend=1, review_executor=reviewer, reviewer_id="agent:independent-reviewer",
        )
        control = read_json(project / "qa" / "chapter-controls" / f"{document.sections[0].section_id}.json")
        rounds = control["rounds"]
        self.assertEqual([row["latest_round_status"] for row in rounds[-2:]], ["FIXED_RECHECK_REQUIRED", "PASS"])
        self.assertTrue(all(row["coverage_status"] == "FULL_CHAPTER_COVERAGE" for row in rounds[-2:]))
        self.assertGreater(rounds[-1]["review_batch_count"], 1)
        self.assertNotEqual(rounds[-1]["reviewer_id"], rounds[-1]["translation_owner_id"])
        self.assertEqual(set(reviewer.models), {DEFAULT_AGENT_REVIEW_MODEL})
        self.assertTrue(any(task["context_capsule"]["batch"].get("review_issues") for task in translator.tasks))
        self.assertEqual(len(list((project / "qa" / "review-evidence").glob("*.json"))), len(reviewer.tasks))
        self.assertTrue(all(unit.qa_status == "passed" for unit in Document.from_dict(read_json(project / "work" / "document.json")).units if unit.translatable))

    def test_zero_issue_review_evidence_cannot_write_canonical_target(self):
        project, document = self.project()
        for unit in document.units:
            if unit.translatable:
                unit.target_text = "灯塔没有亮。"
                unit.translation_status = "translated"
        write_json(project / "work" / "document.json", document.to_dict())
        target_before = {unit.id: unit.target_text for unit in document.units if unit.translatable}
        reviewer = FakeReviewExecutor()
        run_chapter_qa_agent(
            project, document, FakeProvider(), self.language, [], reviewer,
            reviewer_id="agent:reviewer", translation_owner_id="api:translation-owner",
            review_model=DEFAULT_AGENT_REVIEW_MODEL, translation_backend=2,
        )
        self.assertTrue(all("project_path" not in task and "document_path" not in task for task in reviewer.tasks))
        merged = Document.from_dict(read_json(project / "work" / "document.json"))
        self.assertEqual(target_before, {unit.id: unit.target_text for unit in merged.units if unit.translatable})

    def test_agent_review_backend_is_independent_from_api_translation(self):
        project, _ = self.project()
        provider, reviewer = FakeProvider(), FakeReviewExecutor()
        run_project(
            project, provider=provider, translation_backend=2,
            review_backend=1, review_executor=reviewer, reviewer_id="agent:reviewer",
        )
        state = read_json(project / "state" / "state.json")
        self.assertEqual((state["actual_backend"], state["actual_review_backend"]), (2, 1))
        self.assertGreater(provider.translation_calls, 0)
        self.assertTrue(reviewer.tasks)

    def test_api_review_backend_remains_available_and_agent_fallback_requires_explicit_opt_in(self):
        api_project, _ = self.project("The old clock stopped at noon.")
        api_provider = FakeProvider()
        run_project(api_project, provider=api_provider, translation_backend=2, review_backend=2)
        api_state = read_json(api_project / "state" / "state.json")
        self.assertEqual((api_state["requested_review_backend"], api_state["actual_review_backend"]), (2, 2))
        self.assertGreater(api_provider.review_calls, 0)

        fallback_project, _ = self.project("A silver coin lay on the table.")
        fallback_provider = FakeProvider()
        with self.assertRaisesRegex(RuntimeError, "AGENT_REVIEW_UNAVAILABLE"):
            run_project(fallback_project, provider=fallback_provider, translation_backend=2, review_backend=1)
        self.assertEqual(fallback_provider.review_calls, 0)
        run_project(
            fallback_project, provider=fallback_provider, translation_backend=2, review_backend=1,
            allow_review_fallback=True,
        )
        fallback_state = read_json(fallback_project / "state" / "state.json")
        self.assertEqual((fallback_state["requested_review_backend"], fallback_state["actual_review_backend"]), (1, 2))
        self.assertEqual(fallback_state["fallback_reason"], "AGENT_REVIEW_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
