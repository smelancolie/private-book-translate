import csv
import json
from pathlib import Path
import shutil
import unittest
import uuid

from private_translate.core.glossary import (
    FIELDS,
    discover_candidates,
    prepare_terminology,
    validate_terminology_result,
    validate_terminology_task_current,
)
from private_translate.core.batching import estimate_tokens, provider_budgets
from private_translate.core.language import load_language_package
from private_translate.core.model import content_hash
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter


class HostPending(RuntimeError):
    pass


class HostExecutor:
    def __init__(self, pending_each_task=False):
        self.pending_each_task = pending_each_task
        self.calls_by_task = {}
        self.tasks = []
        self.models = []

    def execute(self, task, *, model):
        task = json.loads(json.dumps(task, ensure_ascii=False))
        task_id = task["task_id"]
        self.tasks.append(task)
        self.models.append(model)
        call = self.calls_by_task.get(task_id, 0) + 1
        self.calls_by_task[task_id] = call
        if self.pending_each_task and call == 1:
            raise HostPending("waiting for host result")
        return {
            "schema_version": 1,
            "task_id": task_id,
            "owner_id": task["owner_id"],
            "proposals": [
                {"source": source, "preferred_translation": f"术语：{source}"}
                for source in task["source_keys"]
            ],
        }


class HostTerminologyTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.root = self.repo / "projects" / f".test-host-terminology-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(self._cleanup)
        self.language = load_language_package("en", "zh-Hans")
        self.capabilities = ProviderCapabilities("fake", "small", 3000, 256, True, False)

    def _cleanup(self):
        self.assertTrue(self.root.resolve().is_relative_to((self.repo / "projects").resolve()))
        shutil.rmtree(self.root, ignore_errors=True)

    def project(self, text):
        source = self.root / "book.md"
        source.write_text(text, encoding="utf-8")
        project = create_project(source, self.root / "projects")
        document = MarkdownAdapter().import_file(project / "source" / "book.md", "en", "zh-Hans")
        write_json(project / "work" / "document.json", document.to_dict())
        return project, document

    def prepare_host(self, project, document, executor):
        return prepare_terminology(
            project, document, None, self.language,
            executor=executor, model="gpt-6-luna", capabilities=self.capabilities,
        )

    def task(self, project, executor):
        task_id = executor.tasks[-1]["task_id"]
        path = project / "work" / "tasks" / "terminology" / f"{task_id}.json"
        return read_json(path)

    def test_duplicate_occurrences_propose_once_and_completed_task_is_skipped(self):
        project, document = self.project(
            "# Chapter\n\nNorth Star guides the ship.\n\nThe North Star shines at dusk.\n\nNorth Star returns.\n"
        )
        executor = HostExecutor()

        self.prepare_host(project, document, executor)
        self.prepare_host(project, document, executor)

        self.assertEqual(len(executor.tasks), 1)
        task = executor.tasks[0]
        self.assertEqual(task["task_type"], "terminology")
        self.assertEqual(task["owner_id"], "agent:terminology-owner")
        self.assertEqual(task["source_keys"], ["North Star"])
        self.assertEqual(task["candidate_snapshots"][0]["discovery"]["occurrence_count"], "3")
        with (project / "glossary" / "candidates.csv").open(encoding="utf-8", newline="") as stream:
            row = next(row for row in csv.DictReader(stream) if row["source"] == "North Star")
        self.assertEqual((row["preferred_translation"], row["status"]), ("术语：North Star", "proposed"))
        with (project / "glossary" / "terms.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_pending_multi_batch_tasks_resume_with_the_same_ids(self):
        names = [
            "North Star", "Polar Compass", "Silver Anchor", "Quiet Harbor", "Golden Current",
            "Blue Horizon", "Amber Lantern", "Winter Passage", "Eastern Beacon", "Hidden Channel",
        ]
        paragraphs = [f"{name} guides the ship." for name in names]
        paragraphs += [f"The {name} appears again." for name in names]
        project, document = self.project("# Chapter\n\n" + "\n\n".join(paragraphs) + "\n")
        self.assertGreaterEqual(len(discover_candidates(document)), 2)
        executor = HostExecutor(pending_each_task=True)

        for _ in range(20):
            try:
                self.prepare_host(project, document, executor)
                break
            except HostPending:
                continue
        else:
            self.fail("terminology batches did not finish")

        self.assertGreater(len(executor.calls_by_task), 1)
        self.assertTrue(all(count == 2 for count in executor.calls_by_task.values()))
        self.assertEqual(len(executor.tasks), 2 * len(executor.calls_by_task))
        for task_id in executor.calls_by_task:
            self.assertEqual(
                [task["task_id"] for task in executor.tasks].count(task_id), 2,
            )
        input_budget = provider_budgets(self.capabilities, self.language)["translation_batch_target_tokens"]
        self.assertTrue(all(
            estimate_tokens(json.dumps(task, ensure_ascii=False)) <= input_budget
            for task in executor.tasks
        ))
        self.assertEqual(set(executor.models), {"gpt-6-luna"})
        with (project / "glossary" / "candidates.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row["preferred_translation"] for row in rows))
        self.assertTrue(all(row["status"] == "proposed" for row in rows))

    def test_large_occurrence_index_is_not_sent_in_full_to_worker(self):
        paragraphs = [f"North Star guides the ship through passage {index}." for index in range(40)]
        project, document = self.project("# Chapter\n\n" + "\n\n".join(paragraphs) + "\n")
        executor = HostExecutor()

        self.prepare_host(project, document, executor)

        task = executor.tasks[0]
        snapshot = task["candidate_snapshots"][0]
        self.assertEqual(snapshot["source"], "North Star")
        self.assertEqual(snapshot["discovery"]["occurrence_count"], "40")
        self.assertEqual(len(snapshot["discovery"]["context_occurrences"]), 3)
        self.assertNotIn("source_units", snapshot)
        self.assertEqual(len(task["context_capsule"]["candidates"][0]["source_occurrences"]), 3)
        budget = provider_budgets(self.capabilities, self.language)["translation_batch_target_tokens"]
        self.assertLessEqual(estimate_tokens(json.dumps(task, ensure_ascii=False)), budget)

    def test_validate_result_returns_proposals_without_writing_csv(self):
        project, document = self.project("# Chapter\n\nNorth Star guides the ship.\n\nThe North Star returns.\n")
        executor = HostExecutor(pending_each_task=True)
        with self.assertRaises(HostPending):
            self.prepare_host(project, document, executor)
        task = self.task(project, executor)
        result = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "owner_id": task["owner_id"],
            "proposals": [
                {"source": source, "preferred_translation": "北极星"}
                for source in task["source_keys"]
            ],
        }
        candidate_path = project / "glossary" / "candidates.csv"
        before = candidate_path.read_bytes()

        proposals = validate_terminology_result(project, result, task)

        self.assertEqual(proposals, [{"source": "North Star", "preferred_translation": "北极星"}])
        self.assertEqual(candidate_path.read_bytes(), before)

    def test_manual_candidate_status_change_rejects_stale_result(self):
        project, document = self.project("# Chapter\n\nNorth Star guides the ship.\n\nThe North Star returns.\n")
        executor = HostExecutor(pending_each_task=True)
        with self.assertRaises(HostPending):
            self.prepare_host(project, document, executor)
        task = self.task(project, executor)
        result = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "owner_id": task["owner_id"],
            "proposals": [
                {"source": source, "preferred_translation": "北极星"}
                for source in task["source_keys"]
            ],
        }
        candidate_path = project / "glossary" / "candidates.csv"
        with candidate_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["status"] = "approved"
        with candidate_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        before = candidate_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "STALE_TERMINOLOGY_TASK"):
            validate_terminology_task_current(project, task)
        with self.assertRaisesRegex(ValueError, "STALE_TERMINOLOGY_TASK"):
            validate_terminology_result(project, result, task)

        self.assertEqual(candidate_path.read_bytes(), before)
        self.assertEqual(rows[0]["status"], "approved")

    def test_source_change_rejects_result_and_duplicate_or_unknown_proposals(self):
        project, document = self.project("# Chapter\n\nNorth Star guides the ship.\n\nThe North Star returns.\n")
        executor = HostExecutor(pending_each_task=True)
        with self.assertRaises(HostPending):
            self.prepare_host(project, document, executor)
        task = self.task(project, executor)
        result = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "owner_id": task["owner_id"],
            "proposals": [
                {"source": source, "preferred_translation": "北极星"}
                for source in task["source_keys"]
            ],
        }
        bad_results = [
            {**result, "proposals": result["proposals"] * 2},
            {**result, "proposals": [{"source": "Unknown Term", "preferred_translation": "未知"}]},
            {**result, "proposals": [{"source": "North Star", "preferred_translation": "  "}]},
        ]
        for bad_result in bad_results:
            with self.subTest(proposals=bad_result["proposals"]):
                with self.assertRaises(ValueError):
                    validate_terminology_result(project, bad_result, task)
        with self.assertRaises(ValueError):
            validate_terminology_result(project, {**result, "task_id": str(uuid.uuid4())}, task)

        changed = next(unit for unit in document.units if "North Star" in unit.source_text)
        changed.source_text += " The sky is cloudy."
        changed.source_hash = content_hash(changed.source_text)
        write_json(project / "work" / "document.json", document.to_dict())
        with self.assertRaisesRegex(ValueError, "STALE_TERMINOLOGY_TASK"):
            validate_terminology_task_current(project, task)
        with self.assertRaisesRegex(ValueError, "STALE_TERMINOLOGY_TASK"):
            validate_terminology_result(project, result, task)


if __name__ == "__main__":
    unittest.main()
