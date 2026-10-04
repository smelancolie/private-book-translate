from copy import deepcopy
import json
import os
import subprocess
import sys
import unittest

import test_agent_review as fixtures
from private_translate.core.pipeline import build_project, validate_project
from private_translate.core.project import create_project
from private_translate.core.state import read_json
from private_translate.host import (
    advance_host_run, finish_host_spotcheck, get_host_request, get_project_status,
    start_host_spotcheck, submit_host_result,
)


class HostTests(unittest.TestCase):
    setUp = fixtures.AgentReviewTests.setUp
    _cleanup = fixtures.AgentReviewTests._cleanup
    project = fixtures.AgentReviewTests.project

    def advance(self, project):
        return advance_host_run(project, capabilities=self.provider.capabilities,
                                agent_model="host-translation", review_model="host-review")

    def setUp(self):
        fixtures.AgentReviewTests.setUp(self)
        self.provider = fixtures.FakeProvider()
        self.translator = fixtures.FakeTranslationExecutor()
        self.reviewer = fixtures.FakeReviewExecutor()

    def result(self, item):
        if item["task"]["task_type"] == "terminology":
            task = item["task"]
            return {"schema_version": 1, "task_id": task["task_id"], "owner_id": task["owner_id"],
                    "proposals": [{"source": source, "preferred_translation": "术语" + source}
                                  for source in task["source_keys"]]}
        if item["task"]["task_type"] == "book_consistency":
            task = item["task"]
            return {"schema_version": 1, "task_id": task["task_id"], "reviewer_id": task["reviewer_id"],
                    "target_hash": task["target_hash"], "glossary_hash": task["glossary_hash"],
                    "issues": [], "summary": "Reviewed cross-chapter consistency."}
        executor = self.translator if item["task"]["task_type"] == "translation" else self.reviewer
        return executor.execute(item["task"], model=item["model"])

    def run_to_candidates(self, project):
        request = self.advance(project)
        while request["status"] in {"awaiting_host", "terminology_ready"}:
            for item in request["tasks"]:
                submit_host_result(project, self.result(item))
            request = self.advance(project)
        self.assertEqual(request["status"], "spotcheck_pending")
        return request

    def spotcheck_result(self, task):
        return {"schema_version": 1, "task_id": task["task_id"], "round_id": task["round_id"],
                "reviewer_id": task["reviewer_id"],
                "scores": {item["unit_id"]: 96 for item in task["assigned_samples"]},
                "findings": [], "style_debt_count": 0}

    def test_status_for_imported_project_does_not_parse_or_dispatch(self):
        source = self.temp_root / "new.md"
        source.write_text("The lantern was not lit.", encoding="utf-8")
        project = create_project(source, self.temp_root / "workspaces")
        self.assertEqual(get_project_status(project), {
            "project_id": project.name, "status": "imported", "unit_counts": {},
            "host_status": "not_started", "pending_task_ids": [],
        })
        self.assertFalse((project / "work/document.json").exists())

    def test_status_tracks_pending_results_and_candidate_qa(self):
        project, _ = self.project()
        request = self.advance(project)
        task_id = request["tasks"][0]["task"]["task_id"]
        status = get_project_status(project)
        self.assertEqual(status["status"], "awaiting_host")
        self.assertEqual(status["unit_counts"], {"pending": 1})
        self.assertEqual(status["pending_task_ids"], [task_id])
        submit_host_result(project, self.result(request["tasks"][0]))
        status = get_project_status(project)
        self.assertEqual(status["host_status"], "results_ready")
        self.assertEqual(status["pending_task_ids"], [])
        self.assertEqual(status["unit_counts"], {"pending": 1})
        self.run_to_candidates(project)
        status = get_project_status(project)
        self.assertEqual(status["status"], "spotcheck_pending")
        self.assertEqual(status["unit_counts"], {"qa_passed": 1})
        self.assertEqual(status["pending_task_ids"], [])

    def test_pending_task_survives_new_python_process_without_dispatch(self):
        project, _ = self.project()
        first = self.advance(project)
        self.assertEqual(first["tasks"][0]["task"]["task_type"], "translation")
        again = self.advance(project)
        self.assertEqual(first, again)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.repo / "src")
        completed = subprocess.run(
            [sys.executable, "-c", "import json,sys; from pathlib import Path; "
             "from private_translate.host import get_host_request; "
             "print(json.dumps(get_host_request(Path(sys.argv[1]))))", str(project)],
            env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(completed.stdout), first)
        self.assertEqual(len(list((project / "work/tasks/translation").glob("*.json"))), 1)

    def test_store_then_resume_applies_patch_once_and_preserves_task_id(self):
        project, _ = self.project()
        request = self.advance(project)
        item = request["tasks"][0]
        result = self.result(item)
        before = read_json(project / "work/document.json")
        invalid = deepcopy(result)
        invalid["translations"][0]["source_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source hash"):
            submit_host_result(project, invalid)
        self.assertEqual(read_json(project / "work/document.json"), before)
        submit_host_result(project, result)
        submit_host_result(project, result)
        self.assertEqual(get_host_request(project)["status"], "results_ready")
        self.assertEqual(read_json(project / "work/document.json"), before)
        next_request = self.advance(project)
        self.assertEqual(next_request["tasks"][0]["task"]["task_type"], "chapter_review")
        accepted = read_json(project / "work/patches/translation" / f"{result['task_id']}.json")
        self.assertEqual(accepted["patch_id"], result["patch_id"])
        submit_host_result(project, result)
        different = dict(result, patch_id="00000000-0000-0000-0000-000000000000")
        with self.assertRaisesRegex(ValueError, "different result"):
            submit_host_result(project, different)
        self.assertEqual(len(list((project / "work/tasks/translation").glob("*.json"))), 1)

    def test_full_chain_two_reviewers_two_rounds_build_validate_and_replay(self):
        project, _ = self.project()
        self.run_to_candidates(project)
        self.assertEqual(len(self.translator.tasks), 1)
        self.assertEqual(len(self.reviewer.tasks), 1)
        control = project / "qa/chapter-controls" / f"{self.reviewer.tasks[0]['chapter_id']}.json"
        for seed in (17, 18):
            request = start_host_spotcheck(project, seed=seed,
                                          reviewer_a_id="worker:a", reviewer_b_id="worker:b")
            submit_host_result(project, self.spotcheck_result(request["tasks"][0]["task"]))
            pending = finish_host_spotcheck(project)
            self.assertEqual(len(pending["tasks"]), 1)
            self.assertEqual(pending["tasks"][0]["task"]["task_id"], request["tasks"][1]["task"]["task_id"])
            submit_host_result(project, self.spotcheck_result(request["tasks"][1]["task"]))
            self.assertEqual(finish_host_spotcheck(project)["result"]["status"], "PASS")
        build_project(project)
        validate_project(project)
        self.assertEqual(read_json(project / "state/state.json")["status"], "complete")
        self.assertEqual(self.advance(project)["status"], "spotcheck_pending")
        self.assertEqual(len(read_json(control)["rounds"]), 1)
        self.assertEqual(len(list((project / "work/patches/translation").glob("*.json"))), 1)

    def test_agent_fix_round_resumes_original_task_without_duplicate_fix_ids(self):
        project, _ = self.project()
        self.reviewer = fixtures.FakeReviewExecutor(issue_once=True)
        request = self.advance(project)
        submit_host_result(project, self.result(request["tasks"][0]))
        request = self.advance(project)
        submit_host_result(project, self.result(request["tasks"][0]))
        fix = self.advance(project)
        task = fix["tasks"][0]["task"]
        self.assertTrue(task["context_capsule"]["batch"]["review_issues"])
        self.assertEqual(self.advance(project), fix)
        submit_host_result(project, self.result(fix["tasks"][0]))
        self.run_to_candidates(project)
        control = read_json(project / "qa/chapter-controls" / f"{task['chapter_id']}.json")
        self.assertEqual(control["rounds"][0]["fix_task_ids"], [task["task_id"]])
        self.assertEqual(control["rounds"][0]["fixes_applied"], 1)
        self.assertEqual(len(control["rounds"]), 2)

    def test_multiple_batches_resume_without_changing_pending_units(self):
        text = "\n\n".join(
            f"The lantern number {index} was not burning beside the narrow bridge while the river moved quietly."
            for index in range(1, 17)
        )
        project, _ = self.project(text)
        self.run_to_candidates(project)
        self.assertGreater(len(self.translator.tasks), 1)
        self.assertEqual(len(self.translator.tasks), len({task["task_id"] for task in self.translator.tasks}))

    def test_multiple_fix_batches_finish_before_full_recheck(self):
        class Reviewer(fixtures.FakeReviewExecutor):
            def execute(self, task, *, model):
                evidence = super().execute(task, model=model)
                evidence["issues"] = [
                    {"unit_id": unit["unit_id"], "category": "fidelity", "severity": "major",
                     "description": "Fix wording.", "suggested_fix": "Use the corrected sentence."}
                    for unit in task["target_units"] if unit["target_text"].startswith("Agent初译")
                ]
                return evidence

        self.reviewer = Reviewer()
        text = "\n\n".join(
            f"The lantern {index} was not burning beside the narrow bridge while the river moved quietly."
            for index in range(1, 6)
        )
        project, _ = self.project(text)
        self.run_to_candidates(project)
        fixes = [task for task in self.translator.tasks if task["context_capsule"]["batch"].get("review_issues")]
        self.assertGreater(len(fixes), 1)
        control = read_json(project / "qa/chapter-controls" / f"{fixes[0]['chapter_id']}.json")
        self.assertEqual(len(control["rounds"]), 2)
        self.assertEqual(control["rounds"][0]["fixes_applied"], 5)
        self.assertTrue(control["rounds"][0]["fixes_completed"])
        self.assertEqual(control["rounds"][0]["fix_task_ids"], [task["task_id"] for task in fixes])
        self.assertEqual(control["rounds"][1]["latest_round_status"], "PASS")


    def test_source_changed_while_host_runs_rejects_result(self):
        project, _ = self.project()
        request = self.advance(project)
        result = self.result(request["tasks"][0])
        (project / "source/book.md").write_text("Different source.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            submit_host_result(project, result)

    def test_wrong_task_and_invalid_id_cannot_write_host_result(self):
        project, _ = self.project()
        request = self.advance(project)
        result = self.result(request["tasks"][0])
        with self.assertRaisesRegex(ValueError, "Invalid host task"):
            submit_host_result(project, dict(result, task_id="../escape"))
        with self.assertRaisesRegex(ValueError, "pending request"):
            submit_host_result(project, dict(result, task_id="00000000-0000-0000-0000-000000000000"))

    def test_same_spotcheck_worker_rejected_before_new_round(self):
        project, _ = self.project()
        self.run_to_candidates(project)
        with self.assertRaisesRegex(ValueError, "distinct host workers"):
            start_host_spotcheck(project, seed=17, reviewer_a_id="worker:a", reviewer_b_id="WORKER:A")
        self.assertFalse((project / "qa/spotcheck/active.json").exists())


if __name__ == "__main__":
    unittest.main()
