from collections import Counter, defaultdict
from pathlib import Path
import unittest
import uuid

from private_translate.core.batching import provider_budgets, task_input_tokens
from private_translate.core.language import load_language_package
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import read_json
from private_translate.host import get_host_request, submit_host_result

import test_host_consistency as host_fixture


SMALL_CAPABILITIES = ProviderCapabilities("test", "small-review", 3000, 256, True, False)
README_CAPABILITIES = ProviderCapabilities("codex-host", "gpt-6-luna", 8000, 2000, True, True)
TOO_SMALL_CAPABILITIES = ProviderCapabilities("test", "too-small", 2000, 400, True, False)


class BudgetWorker:
    def __init__(self):
        self.tasks = []
        self.chapter_issue_sent = False
        self.book_issue_sent = False

    def execute(self, task, *, model):
        self.tasks.append((task, model))
        task_type = task["task_type"]
        if task_type == "translation":
            batch = task["context_capsule"].get("batch", {})
            if batch.get("review_issues"):
                suffix = "（章节修订）"
            elif batch.get("consistency_issues"):
                suffix = "（一致性修订）"
            else:
                suffix = ""
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
                    {
                        "unit_id": unit_id,
                        "source_hash": units[unit_id]["source_hash"],
                        "target_text": host_fixture.translate(units[unit_id]["source_text"]) + suffix,
                    }
                    for unit_id in task["required_unit_ids"]
                ],
                "model": model,
            }
        if task_type == "chapter_review":
            issues = []
            if not self.chapter_issue_sent:
                issues = [{
                    "unit_id": task["source_units"][0]["unit_id"],
                    "category": "fidelity",
                    "severity": "major",
                    "description": "The assigned translation needs one correction.",
                    "suggested_fix": "Revise the assigned translation.",
                }]
                self.chapter_issue_sent = True
            return {
                "schema_version": 1,
                "task_id": task["task_id"],
                "chapter_id": task["chapter_id"],
                "reviewer_id": task["reviewer_id"],
                "translation_owner_id": task["translation_owner_id"],
                "base_chapter_digest": task["base_chapter_digest"],
                "target_digest": task["target_digest"],
                "issues": issues,
                "summary": "The assigned chapter units were reviewed.",
            }
        if task_type == "book_consistency":
            issues = []
            if not self.book_issue_sent:
                issues = [{
                    "unit_id": task["source_units"][0]["unit_id"],
                    "category": "terminology",
                    "severity": "major",
                    "description": "The approved term needs a consistency correction.",
                    "suggested_fix": "Use the approved term consistently.",
                }]
                self.book_issue_sent = True
            return host_fixture.consistency_result(task, issues=issues)
        raise AssertionError(f"Unexpected host task type: {task_type}")


class HostBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = host_fixture.HostConsistencyTests(
            methodName="test_formal_host_run_needs_no_api_environment_or_http_provider",
        )
        self.fixture.repo = Path(__file__).parents[1]
        self.fixture.root = (
            self.fixture.repo / "projects" / "f01-n03-2026-10-04" / "host-budget-tests"
            / uuid.uuid4().hex
        )
        self.fixture.root.mkdir(parents=True)
        self.fixture.worker = host_fixture.HostWorker()

    def _advance(self, project, capabilities):
        return self.fixture._advance(project, capabilities)

    @staticmethod
    def _budget(task, capabilities):
        language = load_language_package("en", "zh-Hans")
        budgets = provider_budgets(capabilities, language)
        if task["task_type"] in {"chapter_review", "book_consistency"}:
            return budgets["review_batch_target_tokens"]
        return budgets["translation_batch_target_tokens"]

    def _assert_task_fits(self, task, capabilities):
        size = task_input_tokens(task)
        budget = self._budget(task, capabilities)
        self.assertLessEqual(size, budget, f"{task['task_type']} task uses {size}/{budget} input tokens")

    def test_host_handoffs_fit_complete_task_budgets_and_cover_recovery_and_fixes(self):
        markdown = host_fixture.HostConsistencyTests._book((16, 16))
        project, document = self.fixture._project(markdown, approved_term=True)
        worker = BudgetWorker()
        term_unit_ids = {
            unit.id for unit in document.units if unit.translatable and host_fixture.TERM in unit.source_text
        }
        expected_by_chapter = defaultdict(set)
        for unit in document.units:
            if unit.translatable:
                expected_by_chapter[unit.section_id].add(unit.id)

        initial_translation_ids = defaultdict(list)
        chapter_fix_ids = set()
        book_fix_ids = set()
        review_coverage = defaultdict(set)
        review_assigned_count = Counter()
        review_task_counts = Counter()
        consistency_coverage = defaultdict(set)
        consistency_assigned_count = Counter()
        consistency_task_counts = Counter()
        task_types = Counter()
        recovery_checked = False
        recovery_continued = False

        request = self._advance(project, SMALL_CAPABILITIES)
        for _ in range(240):
            if request["status"] == "terminology_ready":
                request = self._advance(project, SMALL_CAPABILITIES)
                continue
            if request["status"] != "awaiting_host":
                break

            self.assertEqual(len(request["tasks"]), 1)
            item = request["tasks"][0]
            task = item["task"]
            self._assert_task_fits(task, SMALL_CAPABILITIES)
            task_types[task["task_type"]] += 1

            if not recovery_checked:
                self.assertEqual(get_host_request(project), request)
                self.assertEqual(self._advance(project, SMALL_CAPABILITIES), request)
                self.assertEqual(
                    self.fixture._process_advance_in_fresh_process(project, SMALL_CAPABILITIES),
                    request,
                )
                recovery_checked = True

            if task["task_type"] == "translation":
                batch = task["context_capsule"].get("batch", {})
                required = set(task["required_unit_ids"])
                if batch.get("review_issues"):
                    issue_ids = {issue["unit_id"] for issue in batch["review_issues"]}
                    self.assertEqual(required, issue_ids)
                    chapter_fix_ids.update(required)
                elif batch.get("consistency_issues"):
                    issue_ids = {issue["unit_id"] for issue in batch["consistency_issues"]}
                    self.assertEqual(required, issue_ids)
                    book_fix_ids.update(required)
                else:
                    initial_translation_ids[task["chapter_id"]].extend(task["required_unit_ids"])
            elif task["task_type"] == "chapter_review":
                key = (task["chapter_id"], task["context_capsule"]["chapter_target_digest"])
                assigned_ids = [row["unit_id"] for row in task["source_units"]]
                review_coverage[key].update(assigned_ids)
                review_assigned_count[key] += len(assigned_ids)
                review_task_counts[key] += 1
            elif task["task_type"] == "book_consistency":
                key = task["target_hash"]
                assigned_ids = [row["unit_id"] for row in task["source_units"]]
                consistency_coverage[key].update(assigned_ids)
                consistency_assigned_count[key] += len(assigned_ids)
                consistency_task_counts[key] += 1
            elif task["task_type"] == "terminology":
                self.fail("The approved North Star glossary unexpectedly produced a terminology task")

            before_document = read_json(project / "work" / "document.json")
            result = worker.execute(task, model=item["model"])
            receipt = submit_host_result(project, result)
            self.assertEqual(receipt["status"], "result_stored")
            self.assertEqual(submit_host_result(project, result), receipt)
            self.assertEqual(read_json(project / "work" / "document.json"), before_document)

            if recovery_checked and not recovery_continued:
                submitted_task_id = task["task_id"]
                request = self.fixture._process_advance_in_fresh_process(project, SMALL_CAPABILITIES)
                self.assertEqual(get_host_request(project), request)
                self.assertNotIn(submitted_task_id, [row["task"]["task_id"] for row in request["tasks"]])
                recovery_continued = True
            else:
                request = self._advance(project, SMALL_CAPABILITIES)
        else:
            self.fail("Host budget scenario did not finish within 240 handoffs")

        self.assertEqual(request["status"], "spotcheck_pending")
        self.assertTrue(recovery_checked)
        self.assertTrue(recovery_continued)
        self.assertIn("translation", task_types)
        self.assertIn("chapter_review", task_types)
        self.assertIn("book_consistency", task_types)
        self.assertTrue(chapter_fix_ids)
        self.assertTrue(book_fix_ids)
        self.assertTrue(term_unit_ids)

        self.assertEqual(set(initial_translation_ids), set(expected_by_chapter))
        for chapter_id, expected_ids in expected_by_chapter.items():
            self.assertEqual(set(initial_translation_ids[chapter_id]), expected_ids)
            self.assertEqual(len(initial_translation_ids[chapter_id]), len(expected_ids))
        self.assertTrue(any(count > 1 for count in Counter(
            task["chapter_id"] for task, _ in worker.tasks
            if task["task_type"] == "translation"
            and not task["context_capsule"].get("batch", {}).get("review_issues")
            and not task["context_capsule"].get("batch", {}).get("consistency_issues")
        ).values()))

        review_groups_by_chapter = defaultdict(list)
        for (chapter_id, target_digest), covered_ids in review_coverage.items():
            self.assertEqual(covered_ids, expected_by_chapter[chapter_id])
            self.assertEqual(review_assigned_count[(chapter_id, target_digest)], len(expected_by_chapter[chapter_id]))
            review_groups_by_chapter[chapter_id].append(covered_ids)
        self.assertEqual(set(review_groups_by_chapter), set(expected_by_chapter))
        self.assertTrue(any(count > 1 for count in review_task_counts.values()))
        for target_hash, covered_ids in consistency_coverage.items():
            self.assertEqual(covered_ids, term_unit_ids)
            self.assertEqual(consistency_assigned_count[target_hash], len(term_unit_ids))
        self.assertTrue(any(count > 1 for count in consistency_task_counts.values()))

    def test_readme_120_approved_term_units_fit_all_host_tasks(self):
        project, document = self.fixture._project(
            host_fixture.HostConsistencyTests._book((60, 60)), approved_term=True,
        )
        worker = BudgetWorker()
        worker.chapter_issue_sent = True
        worker.book_issue_sent = True
        self.fixture.worker = worker
        term_unit_ids = {
            unit.id for unit in document.units
            if unit.translatable and host_fixture.TERM in unit.source_text
        }
        consistency_coverage = defaultdict(list)
        consistency_task_counts = Counter()
        task_types = Counter()
        request = self._advance(project, README_CAPABILITIES)

        for _ in range(120):
            if request["status"] == "terminology_ready":
                request = self._advance(project, README_CAPABILITIES)
                continue
            if request["status"] != "awaiting_host":
                break
            self.assertEqual(len(request["tasks"]), 1)
            item = request["tasks"][0]
            task = item["task"]
            task_types[task["task_type"]] += 1
            self._assert_task_fits(task, README_CAPABILITIES)
            if task["task_type"] == "book_consistency":
                target_hash = task["target_hash"]
                consistency_coverage[target_hash].extend(
                    row["unit_id"] for row in task["source_units"]
                )
                consistency_task_counts[target_hash] += 1
            self.fixture._dispatch(project, request)
            request = self._advance(project, README_CAPABILITIES)
        else:
            self.fail("The 120-unit host budget scenario did not finish within 120 handoffs")

        self.assertEqual(request["status"], "spotcheck_pending")
        self.assertEqual(len(term_unit_ids), 120)
        self.assertIn("translation", task_types)
        self.assertIn("chapter_review", task_types)
        self.assertIn("book_consistency", task_types)
        self.assertEqual(len(consistency_coverage), 1)
        assigned_ids = next(iter(consistency_coverage.values()))
        self.assertEqual(set(assigned_ids), term_unit_ids)
        self.assertEqual(len(assigned_ids), len(term_unit_ids))
        target_hash = next(iter(consistency_task_counts))
        self.assertGreater(consistency_task_counts[target_hash], 1)

    def test_single_complete_translation_task_over_budget_is_rejected_before_dispatch(self):
        project, _ = self.fixture._project("Quiet rain falls softly.")

        with self.assertRaisesRegex(ValueError, "budget"):
            self._advance(project, TOO_SMALL_CAPABILITIES)

        self.assertNotEqual(get_host_request(project)["status"], "awaiting_host")
        task_dir = project / "work" / "tasks" / "translation"
        self.assertFalse(task_dir.exists() and list(task_dir.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
