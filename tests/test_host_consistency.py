from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from private_translate.core.execution import PatchConflict
from private_translate.core.glossary import FIELDS, load_glossary
from private_translate.core.model import Document, content_hash
from private_translate.core.pipeline import _target_hash
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter
from private_translate.host import advance_host_run, submit_host_result


REVIEWER_ID = "agent:book-consistency-reviewer"
TRANSLATION_MODEL = "test:translator"
REVIEW_MODEL = "test:reviewer"
TERM = "North Star"
PREFERRED = "北极星"
FORBIDDEN = "北斗星"
EMPTY_REVIEW_CAPABILITIES = ProviderCapabilities("test", "small-review", 3000, 256, True, False)
DEFAULT_CAPABILITIES = ProviderCapabilities("test", "host-consistency", 4096, 1024, True, False)


class HostWorker:
    def __init__(self):
        self.tasks = []

    def execute(self, task, *, model):
        self.tasks.append((deepcopy(task), model))
        if task["task_type"] == "translation":
            batch = task["context_capsule"].get("batch", {})
            is_fix = bool(batch.get("consistency_issues"))
            units = {unit["id"]: unit for unit in task["units"]}
            return {
                "schema_version": 1,
                "patch_id": str(uuid.uuid4()),
                "task_id": task["task_id"],
                "task_type": "translation",
                "chapter_id": task["chapter_id"],
                "owner_id": task["owner_id"],
                "base_document_revision": task["base_document_revision"],
                "base_chapter_digest": task["base_chapter_digest"],
                "translations": [
                    {"unit_id": unit_id, "source_hash": units[unit_id]["source_hash"],
                     "target_text": translate(units[unit_id]["source_text"], preferred=is_fix)}
                    for unit_id in task["required_unit_ids"]
                ],
                "model": model,
            }
        if task["task_type"] == "chapter_review":
            return {
                "schema_version": 1,
                "task_id": task["task_id"],
                "chapter_id": task["chapter_id"],
                "reviewer_id": task["reviewer_id"],
                "translation_owner_id": task["translation_owner_id"],
                "base_chapter_digest": task["base_chapter_digest"],
                "target_digest": task["target_digest"],
                "issues": [],
                "summary": "The full chapter review found no issues.",
            }
        if task["task_type"] == "book_consistency":
            return consistency_result(task)
        raise AssertionError(f"Unexpected host task type: {task['task_type']}")


def translate(source_text, *, preferred=True):
    if source_text.startswith("The North Star guides us"):
        return f"{PREFERRED if preferred else FORBIDDEN}为我们指引方向。"
    if TERM in source_text:
        return source_text.replace(TERM, PREFERRED if preferred else FORBIDDEN).replace(".", "。")
    if source_text.startswith("Birds flew over water"):
        return "鸟儿飞过水面。"
    if source_text.startswith("The river is quiet"):
        return "河水静静地流淌。"
    if source_text.lower().startswith("chapter"):
        return "章节"
    return "译文。"


def consistency_result(task, *, reviewer_id=None, issues=None):
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "reviewer_id": reviewer_id or task["reviewer_id"],
        "target_hash": task["target_hash"],
        "glossary_hash": task["glossary_hash"],
        "issues": [] if issues is None else issues,
        "summary": "The assigned terminology occurrences were reviewed.",
    }


class HostConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".test-host-consistency-", dir=self.repo / "projects",
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = HostWorker()

    def _project(self, markdown, *, approved_term=False, pretranslated=False):
        source = self.root / f"book-{uuid.uuid4().hex}.md"
        source.write_text(markdown, encoding="utf-8")
        project = create_project(source, self.root / "workspaces")
        document = MarkdownAdapter().import_file(
            project / "source" / source.name, "en", "zh-Hans",
        )
        if approved_term:
            self._write_glossary(project, PREFERRED)
        if pretranslated:
            for unit in document.units:
                if not unit.translatable:
                    continue
                target = translate(unit.source_text)
                unit.target_text = target
                unit.translation_status = "translated"
                unit.qa_status = "passed"
                unit.last_qa_target_hash = content_hash(target)
            write_json(project / "work" / "document.json", document.to_dict())
        else:
            write_json(project / "work" / "document.json", document.to_dict())
        return project, Document.from_dict(read_json(project / "work" / "document.json"))

    @staticmethod
    def _write_glossary(project, preferred):
        row = {
            "source": TERM,
            "preferred_translation": preferred,
            "category": "proper_name",
            "status": "approved",
            "first_occurrence": "",
            "occurrence_count": "",
            "notes": "",
            "forbidden_translations": FORBIDDEN,
        }
        for name in ("terms.csv", "candidates.csv"):
            with (project / "glossary" / name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(row)

    @staticmethod
    def _book(term_units=(2, 2), extra_first_chapter=0):
        blocks = []
        for chapter, count in enumerate(term_units, start=1):
            blocks.append(f"# Chapter {chapter}")
            blocks.extend("The North Star guides us." for _ in range(count))
            if chapter == 1:
                blocks.extend("The river is quiet." for _ in range(extra_first_chapter))
        return "\n\n".join(blocks)

    @staticmethod
    def _advance(project, capabilities=DEFAULT_CAPABILITIES):
        return advance_host_run(
            project,
            capabilities=capabilities,
            agent_model=TRANSLATION_MODEL,
            review_model=REVIEW_MODEL,
            reviewer_id=REVIEWER_ID,
        )

    def _dispatch(self, project, request):
        self.assertEqual(request["status"], "awaiting_host")
        self.assertEqual(len(request["tasks"]), 1)
        item = request["tasks"][0]
        result = self.worker.execute(item["task"], model=item["model"])
        submit_host_result(project, result)
        return item, result

    def _consistency_task(self, project, capabilities=DEFAULT_CAPABILITIES):
        request = self._advance(project, capabilities)
        for _ in range(200):
            if request["status"] == "terminology_ready":
                request = self._advance(project, capabilities)
                continue
            if request["status"] != "awaiting_host":
                self.fail(f"Expected a consistency review task, got {request['status']}")
            task_type = request["tasks"][0]["task"]["task_type"]
            if task_type == "book_consistency":
                return request, request["tasks"][0]["task"]
            if task_type != "chapter_review":
                self.fail(f"Expected chapter or consistency review, got {task_type}")
            self._dispatch(project, request)
            request = self._advance(project, capabilities)
        self.fail("Host run did not reach a consistency review task")

    def _process_advance_in_fresh_process(self, project, capabilities=EMPTY_REVIEW_CAPABILITIES):
        code = (
            "import json,sys; from pathlib import Path; "
            "from private_translate.core.providers import ProviderCapabilities; "
            "from private_translate.host import advance_host_run; "
            "caps=ProviderCapabilities(**json.loads(sys.argv[2])); "
            "print(json.dumps(advance_host_run(Path(sys.argv[1]), capabilities=caps, "
            "agent_model='test:translator', review_model='test:reviewer', "
            "reviewer_id='agent:book-consistency-reviewer'), ensure_ascii=False))"
        )
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "PRIVATE_TRANSLATE_MODEL", "PRIVATE_TRANSLATE_BASE_URL"}
        }
        environment["PYTHONPATH"] = str(self.repo / "src")
        completed = subprocess.run(
            [sys.executable, "-c", code, str(project), json.dumps(capabilities.__dict__)],
            env=environment, capture_output=True, text=True, check=True,
        )
        return json.loads(completed.stdout)

    def test_formal_host_run_needs_no_api_environment_or_http_provider(self):
        project, _ = self._project("Birds flew over water.")
        task_types = []
        with (
            patch.dict(os.environ) as environment,
            patch("private_translate.core.pipeline.OpenAICompatibleProvider",
                  side_effect=AssertionError("HTTP provider constructor called")),
            patch("private_translate.core.providers.urlopen",
                  side_effect=AssertionError("HTTP request called")),
        ):
            for key in ("OPENAI_API_KEY", "PRIVATE_TRANSLATE_MODEL", "PRIVATE_TRANSLATE_BASE_URL"):
                environment.pop(key, None)
            request = self._advance(project)
            for _ in range(10):
                if request["status"] != "awaiting_host":
                    break
                item, _ = self._dispatch(project, request)
                task_types.append(item["task"]["task_type"])
                request = self._advance(project)

        self.assertEqual(request["status"], "spotcheck_pending")
        self.assertIn("translation", task_types)
        self.assertIn("chapter_review", task_types)

    def test_glossary_review_batches_resume_the_same_task_and_wait_for_all_evidence(self):
        project, document = self._project(
            self._book((60, 60)), approved_term=True, pretranslated=True,
        )
        term_unit_ids = {unit.id for unit in document.units if TERM in unit.source_text}
        request, first_task = self._consistency_task(project, EMPTY_REVIEW_CAPABILITIES)
        task_count = first_task["context_capsule"]["batch_count"]
        self.assertGreater(task_count, 1)
        self.assertGreater(first_task["context_capsule"]["candidate_unit_count"], 1)
        self.assertEqual(len(first_task["context_capsule"]["candidate_section_ids"]), 2)
        report = read_json(project / "qa" / "book-consistency.json")
        task_ids = report["active_round"]["task_ids"]
        self.assertEqual(len(task_ids), task_count)
        self.assertEqual(len(list((project / "work" / "tasks" / "consistency").glob("*.json"))), task_count)

        self.assertEqual(self._advance(project, EMPTY_REVIEW_CAPABILITIES), request)
        self.assertEqual(self._process_advance_in_fresh_process(project), request)

        assigned = set()
        evidence_ids = []
        current = request
        while current["status"] == "awaiting_host":
            item, result = self._dispatch(project, current)
            task = item["task"]
            self.assertEqual(task["task_type"], "book_consistency")
            assigned.update(row["unit_id"] for row in task["source_units"])
            evidence_ids.append(task["task_id"])
            current = self._advance(project, EMPTY_REVIEW_CAPABILITIES)
            if len(evidence_ids) < task_count:
                active_report = read_json(project / "qa" / "book-consistency.json")
                self.assertEqual(active_report["status"], "REVIEWING")
                self.assertEqual(active_report["active_round"]["task_ids"], task_ids)
                self.assertEqual(current["status"], "awaiting_host")
        self.assertEqual(current["status"], "spotcheck_pending")
        self.assertEqual(set(evidence_ids), set(task_ids))
        self.assertEqual(assigned, term_unit_ids)
        passed = read_json(project / "qa" / "book-consistency.json")
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(passed["rounds"][-1]["evidence_task_ids"], task_ids)

    def test_consistency_fix_returns_to_original_translator_then_rechecks_chapter_and_book(self):
        project, document = self._project(
            self._book((1, 1), extra_first_chapter=1), approved_term=True,
            pretranslated=True,
        )
        wrong_unit = next(unit for unit in document.units if TERM in unit.source_text)
        document_path = project / "work" / "document.json"
        stored = read_json(document_path)
        for row in stored["units"]:
            if row["id"] == wrong_unit.id:
                row["target_text"] = row["target_text"].replace(PREFERRED, FORBIDDEN)
                row["last_qa_target_hash"] = content_hash(row["target_text"])
        write_json(document_path, stored)

        request, _ = self._consistency_task(project)
        first_review_ids = []
        while request["tasks"][0]["task"]["task_type"] == "book_consistency":
            item, result = self._dispatch(project, request)
            first_review_ids.append(item["task"]["task_id"])
            request = self._advance(project)
        self.assertEqual(request["status"], "awaiting_host")
        fix_item = request["tasks"][0]
        fix_task = fix_item["task"]
        self.assertEqual(fix_task["task_type"], "translation")
        self.assertEqual(fix_item["worker_id"], "agent:translation-owner")
        self.assertTrue(fix_task["context_capsule"]["batch"]["consistency_issues"])
        self.assertEqual(self._advance(project), request)
        self.assertEqual(self._process_advance_in_fresh_process(project, DEFAULT_CAPABILITIES), request)

        before_submit = read_json(document_path)
        fix_result = self.worker.execute(fix_task, model=fix_item["model"])
        submit_host_result(project, fix_result)
        self.assertEqual(read_json(document_path), before_submit)
        request = self._process_advance_in_fresh_process(project, DEFAULT_CAPABILITIES)
        self.assertEqual(request["tasks"][0]["task"]["task_type"], "chapter_review")
        chapter_task = request["tasks"][0]["task"]
        chapter_unit_ids = {
            unit.id for unit in document.units
            if unit.translatable and unit.section_id == fix_task["chapter_id"]
        }
        self.assertEqual({row["unit_id"] for row in chapter_task["target_units"]}, chapter_unit_ids)

        chapter_review_types = []
        while request["status"] == "awaiting_host" and request["tasks"][0]["task"]["task_type"] == "chapter_review":
            item, _ = self._dispatch(project, request)
            chapter_review_types.append(item["task"]["task_type"])
            request = self._advance(project)
        self.assertTrue(chapter_review_types)
        self.assertEqual(request["status"], "awaiting_host")
        self.assertEqual(request["tasks"][0]["task"]["task_type"], "book_consistency")
        second_review_ids = []
        while request["status"] == "awaiting_host":
            item, _ = self._dispatch(project, request)
            if item["task"]["task_type"] == "book_consistency":
                second_review_ids.append(item["task"]["task_id"])
            request = self._advance(project)
        self.assertEqual(request["status"], "spotcheck_pending")

        report = read_json(project / "qa" / "book-consistency.json")
        self.assertEqual([row["status"] for row in report["rounds"][-2:]], [
            "FIXED_RECHECK_REQUIRED", "PASS",
        ])
        self.assertEqual(report["rounds"][-2]["evidence_task_ids"], first_review_ids)
        self.assertEqual(report["rounds"][-1]["evidence_task_ids"], second_review_ids)
        self.assertTrue(list((project / "qa" / "review-evidence").glob("*.json")))
        chapter_evidence_before = sorted(path.name for path in (project / "qa" / "review-evidence").glob("*.json"))
        consistency_evidence_before = sorted(path.name for path in (project / "qa" / "consistency" / "evidence").glob("*.json"))
        self.assertEqual(self._advance(project)["status"], "spotcheck_pending")
        self.assertEqual(
            sorted(path.name for path in (project / "qa" / "review-evidence").glob("*.json")),
            chapter_evidence_before,
        )
        self.assertEqual(
            sorted(path.name for path in (project / "qa" / "consistency" / "evidence").glob("*.json")),
            consistency_evidence_before,
        )

    def test_consistency_result_rejects_identity_units_and_stale_snapshots_without_applying(self):
        project, _ = self._project(
            self._book((2, 2)), approved_term=True, pretranslated=True,
        )
        _, task = self._consistency_task(project)
        document_path = project / "work" / "document.json"
        original_document = read_json(document_path)
        valid = consistency_result(task)

        with self.assertRaisesRegex(ValueError, "reviewer_id"):
            submit_host_result(project, consistency_result(task, reviewer_id="agent:other-reviewer"))
        unknown_issue = {
            "unit_id": str(uuid.uuid4()), "category": "fidelity", "severity": "minor",
            "description": "The report points outside this task.", "suggested_fix": "Use an assigned unit.",
        }
        with self.assertRaisesRegex(ValueError, "unknown or unassigned"):
            submit_host_result(project, consistency_result(task, issues=[unknown_issue]))
        self.assertEqual(read_json(document_path), original_document)

        stale_document = deepcopy(original_document)
        stale_document["units"][0]["target_text"] += "已改动"
        write_json(document_path, stale_document)
        stale_snapshot = read_json(document_path)
        with self.assertRaisesRegex(PatchConflict, "STALE_BOOK_CONSISTENCY"):
            submit_host_result(project, valid)
        self.assertEqual(read_json(document_path), stale_snapshot)
        write_json(document_path, original_document)

        self._write_glossary(project, "北极星号")
        stale_glossary_snapshot = read_json(document_path)
        with self.assertRaisesRegex(PatchConflict, "STALE_BOOK_CONSISTENCY"):
            submit_host_result(project, valid)
        self.assertEqual(read_json(document_path), stale_glossary_snapshot)
        self._write_glossary(project, PREFERRED)

        self.assertEqual(submit_host_result(project, valid)["status"], "result_stored")
        self.assertEqual(read_json(document_path), original_document)

    def test_api_pass_for_same_hash_does_not_replace_agent_consistency_evidence(self):
        project, document = self._project(
            self._book((2, 2)), approved_term=True, pretranslated=True,
        )
        glossary = load_glossary(project / "glossary" / "terms.csv")
        translated = [unit for unit in document.units if unit.translatable and unit.target_text is not None]
        glossary_hash = content_hash(json.dumps(glossary, ensure_ascii=False, sort_keys=True))
        write_json(project / "qa" / "book-consistency.json", {
            "status": "PASS", "actual_backend": 2,
            "target_hash": _target_hash(translated), "glossary_hash": glossary_hash,
            "rounds": [{"round": 1, "status": "PASS", "reviewer_id": "api:reviewer"}],
        })

        _, task = self._consistency_task(project)
        self.assertEqual(task["task_type"], "book_consistency")
        self.assertEqual(task["target_hash"], _target_hash(translated))
        report = read_json(project / "qa" / "book-consistency.json")
        self.assertEqual(report["actual_backend"], 1)
        self.assertEqual(report["status"], "REVIEWING")
        self.assertFalse((project / "qa" / "consistency" / "evidence" / f"{task['task_id']}.json").exists())


if __name__ == "__main__":
    unittest.main()
