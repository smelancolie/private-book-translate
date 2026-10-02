from copy import deepcopy
from unittest.mock import patch
import unittest

import test_agent_review as fixtures
from private_translate.core.execution import (
    PatchConflict, apply_translation_patch, create_review_task, create_translation_task,
    load_review_task_state, load_translation_task, review_target_digest,
    set_review_task_status, validate_review_evidence_current,
)
from private_translate.core.model import content_hash
from private_translate.core.qa import _append_round, run_chapter_qa_agent
from private_translate.core.state import read_json, write_json


class H3ReplayTests(unittest.TestCase):
    setUp = fixtures.AgentReviewTests.setUp
    _cleanup = fixtures.AgentReviewTests._cleanup
    project = fixtures.AgentReviewTests.project
    evidence = fixtures.AgentReviewTests.evidence

    def translated(self, text="The lighthouse was not burning."):
        project, document = self.project(text)
        for unit in document.units:
            if unit.translatable:
                unit.target_text = "旧译文：" + unit.source_text
                unit.translation_status = "translated"
        write_json(project / "work/document.json", document.to_dict())
        return project, document

    def run_h3(self, project, document, reviewer=None, **kwargs):
        return run_chapter_qa_agent(
            project, document, fixtures.FakeProvider(), self.language, [],
            reviewer or fixtures.FakeReviewExecutor(), reviewer_id="agent:reviewer",
            translation_owner_id="api:translation-owner", review_model="test-model",
            translation_backend=kwargs.pop("translation_backend", 2), **kwargs,
        )

    def control(self, project, document):
        return project / "qa/chapter-controls" / f"{document.sections[0].section_id}.json"

    def task(self, project, document, status="completed", issues=None):
        section = document.sections[0].section_id
        task = create_review_task(document, section, "agent:reviewer", "api:translation-owner",
                                  self.language["review_rubric"])
        task["context_capsule"]["chapter_target_digest"] = task["target_digest"]
        write_json(project / "work/tasks/review" / f"{task['task_id']}.json", task)
        set_review_task_status(project, task, status)
        if status == "completed":
            write_json(project / "qa/review-evidence" / f"{task['task_id']}.json", self.evidence(task, issues))
        return task

    def test_same_digest_same_evidence_replay_does_not_add_round(self):
        project, document = self.translated()
        task = self.task(project, document)
        path = self.control(project, document)
        record = {"round": 1, "target_digest": task["target_digest"],
                  "evidence_task_ids": [task["task_id"]], "latest_round_status": "PASS", "fixes_applied": 0}
        rounds = []
        first = _append_round(path, rounds, record)
        replay = dict(record, round=2)
        self.assertIs(_append_round(path, rounds, replay), first)
        self.assertEqual(len(read_json(path)["rounds"]), 1)

    def test_current_digest_pass_reused_after_qa_bookkeeping_reset(self):
        project, document = self.translated()
        document = self.run_h3(project, document)
        path = self.control(project, document)
        control = read_json(path)
        # Observed PASS -> FIXED_RECHECK_REQUIRED, without a target revision.
        control["rounds"].append(dict(control["rounds"][0], round=2,
                                     latest_round_status="FIXED_RECHECK_REQUIRED"))
        control["latest_round_status"] = "FIXED_RECHECK_REQUIRED"
        write_json(path, control)
        for unit in document.units:
            unit.qa_status = "pending"
            unit.last_qa_target_hash = None
        write_json(project / "work/document.json", document.to_dict())
        reviewer = fixtures.FakeReviewExecutor()
        document = self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(read_json(path)["latest_round_status"], "PASS")
        self.assertEqual(len(read_json(path)["rounds"]), 2)
        self.assertTrue(all(u.qa_status == "passed" for u in document.units if u.translatable))

    def test_resume_completed_task_uses_evidence_without_dispatch(self):
        project, document = self.translated()
        task = self.task(project, document)
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(read_json(self.control(project, document))["rounds"][0]["evidence_task_ids"], [task["task_id"]])

    def test_zero_issue_evidence_repairs_round_status_in_place(self):
        project, document = self.translated()
        document = self.run_h3(project, document)
        path = self.control(project, document)
        control = read_json(path)
        control["rounds"][0]["latest_round_status"] = "FIXED_RECHECK_REQUIRED"
        control["latest_round_status"] = "FIXED_RECHECK_REQUIRED"
        write_json(path, control)
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        updated = read_json(path)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(len(updated["rounds"]), 1)
        self.assertEqual(updated["rounds"][0]["latest_round_status"], "PASS")
        self.assertEqual(updated["latest_round_status"], "PASS")

    def test_resume_pending_task_continues_same_id(self):
        project, document = self.translated()
        task = self.task(project, document, "pending")
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        self.assertEqual([t["task_id"] for t in reviewer.tasks], [task["task_id"]])
        state = load_review_task_state(project, task)
        self.assertEqual([r["status"] for r in state["history"]], ["pending", "completed"])

    def test_resume_after_evidence_save_before_completed_status_does_not_dispatch(self):
        project, document = self.translated()
        task = self.task(project, document, "pending")
        write_json(project / "qa/review-evidence" / f"{task['task_id']}.json", self.evidence(task))
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(load_review_task_state(project, task)["status"], "completed")

    def test_completed_missing_evidence_never_redispatches(self):
        project, document = self.translated()
        task = self.task(project, document)
        (project / "qa/review-evidence" / f"{task['task_id']}.json").unlink()
        reviewer = fixtures.FakeReviewExecutor()
        with self.assertRaises(FileNotFoundError):
            self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(load_review_task_state(project, task)["status"], "completed")

    def test_unknown_legacy_task_is_not_inferred_pending(self):
        project, document = self.translated()
        task = self.task(project, document, "pending")
        (project / "work/task-state/review" / f"{task['task_id']}.json").unlink()
        reviewer = fixtures.FakeReviewExecutor()
        with self.assertRaisesRegex(PatchConflict, "STATE_UNKNOWN"):
            self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])

    def test_legacy_completion_recovered_from_positive_evidence(self):
        project, document = self.translated()
        task = self.task(project, document)
        (project / "work/task-state/review" / f"{task['task_id']}.json").unlink()
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        self.assertEqual(reviewer.tasks, [])
        self.assertEqual(load_review_task_state(project, task)["status"], "completed")

    def test_terminal_tasks_and_evidence_cannot_be_reexecuted(self):
        for status in ("superseded", "rejected"):
            with self.subTest(status=status):
                project, document = self.translated(status)
                task = self.task(project, document, status)
                reviewer = fixtures.FakeReviewExecutor()
                with self.assertRaises(PatchConflict):
                    self.run_h3(project, document, reviewer)
                with self.assertRaises(PatchConflict):
                    validate_review_evidence_current(project, self.evidence(task), task)
                self.assertEqual(reviewer.tasks, [])
                self.assertFalse(self.control(project, document).exists())

    def test_fix_recheck_resumes_original_round_without_copy(self):
        project, document = self.translated()
        reviewer = fixtures.FakeReviewExecutor(issue_once=True)
        # Stop after review round acceptance, before fixes; then resume it.
        with patch("private_translate.core.qa._translate_fixes", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.run_h3(project, document, reviewer)
        original = read_json(self.control(project, document))["rounds"][0]
        resumed = fixtures.FakeReviewExecutor()
        document = self.run_h3(project, document, resumed)
        rounds = read_json(self.control(project, document))["rounds"]
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["evidence_task_ids"], original["evidence_task_ids"])
        self.assertEqual(rounds[0]["fixes_applied"], 1)
        self.assertTrue(rounds[0]["fixes_completed"])
        self.assertNotEqual(rounds[0]["target_digest"], rounds[1]["target_digest"])
        self.assertEqual(rounds[1]["latest_round_status"], "PASS")
        self.assertEqual(len(resumed.tasks), 1)

    def test_observed_adjacent_duplicate_rounds_are_not_copied_again(self):
        project, document = self.translated()
        unit = next(u for u in document.units if u.translatable)
        task = self.task(project, document)
        # Synthetic contents, actual observed IDs and round/fix pattern from spine-0006.
        ids = ["f3ff21e6-7f00-4a63-bfbb-229da03b4aec", "d0e1c79b-1590-4ed5-952f-5b957a418305"]
        issue = {"unit_id": unit.id, "category": "fidelity", "severity": "major",
                 "description": "Negation missing", "suggested_fix": "Restore negation"}
        first = {"round": 5, "target_digest": task["target_digest"], "evidence_task_ids": ids,
                 "issues": [issue], "issues_found": 1, "fixes_applied": 0,
                 "latest_round_status": "FIXED_RECHECK_REQUIRED"}
        second = dict(deepcopy(first), round=6, fixes_applied=1)
        rounds = [first, second]
        path = self.control(project, document)
        write_json(path, {"rounds": rounds, "latest_round_status": "FIXED_RECHECK_REQUIRED"})
        accepted = _append_round(path, rounds, dict(deepcopy(first), round=7))
        self.assertIs(accepted, second)
        self.assertEqual(accepted["fixes_applied"], 1)
        self.assertEqual(len(read_json(path)["rounds"]), 2)

    def test_target_change_reviews_only_affected_section(self):
        project, document = self.translated("# One\n\nFirst sentence.\n\n# Two\n\nSecond sentence.")
        document = self.run_h3(project, document)
        section = document.sections[0].section_id
        unit = next(u for u in document.units if u.section_id == section and u.translatable)
        unit.target_text += "改变"
        write_json(project / "work/document.json", document.to_dict())
        reviewer = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, reviewer)
        self.assertEqual({t["chapter_id"] for t in reviewer.tasks}, {section})
        other = project / "qa/chapter-controls" / f"{document.sections[1].section_id}.json"
        self.assertEqual(len(read_json(other)["rounds"]), 1)

    def test_metadata_only_patch_does_not_stale_pass(self):
        project, document = self.translated()
        document = self.run_h3(project, document)
        unit = next(u for u in document.units if u.translatable)
        unit.translation_status = "stale"
        write_json(project / "work/document.json", document.to_dict())
        path = create_translation_task(project, unit.section_id, "api:translation-owner", batch_context={})
        task = load_translation_task(project, path.stem)
        result = fixtures.FakeTranslationExecutor().execute(task, model="test")
        result["translations"][0]["target_text"] = unit.target_text
        apply_translation_patch(project, result)
        self.assertEqual(read_json(self.control(project, document))["latest_round_status"], "PASS")

    def test_stale_evidence_rejected_without_new_round(self):
        project, document = self.translated()
        reviewer = fixtures.FakeReviewExecutor()
        execute = reviewer.execute
        def change_target(task, *, model):
            result = execute(task, model=model)
            unit = next(u for u in document.units if u.translatable)
            unit.target_text += "改变"
            write_json(project / "work/document.json", document.to_dict())
            return result
        reviewer.execute = change_target
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW"):
            self.run_h3(project, document, reviewer)
        self.assertFalse(self.control(project, document).exists())
        self.assertEqual(load_review_task_state(project, reviewer.tasks[0])["status"], "superseded")

    def test_stale_round_replay_rejected_without_append(self):
        project, document = self.translated()
        document = self.run_h3(project, document)
        path = self.control(project, document)
        control = read_json(path)
        next(u for u in document.units if u.translatable).target_text += "改变"
        write_json(project / "work/document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW_ROUND"):
            _append_round(path, control["rounds"], dict(control["rounds"][0], round=2))
        self.assertEqual(len(read_json(path)["rounds"]), 1)

    def test_qa_status_change_alone_does_not_stale_evidence(self):
        project, document = self.translated()
        task = self.task(project, document)
        for unit in document.units:
            unit.qa_status = "passed"
            unit.last_qa_target_hash = content_hash(unit.target_text or "")
        write_json(project / "work/document.json", document.to_dict())
        validate_review_evidence_current(project, self.evidence(task), task)

    def test_partial_batch_resume_reuses_completed_and_pending_tasks(self):
        text = "\n\n".join(f"Lantern {i} stood beside the narrow bridge while the river moved quietly under the old arches." for i in range(12))
        project, document = self.translated(text)
        reviewer = fixtures.FakeReviewExecutor()
        execute = reviewer.execute
        pending = []
        def interrupt_second(task, *, model):
            if len(reviewer.tasks) == 1:
                pending.append(task["task_id"])
                raise RuntimeError("interrupted batch")
            return execute(task, model=model)
        reviewer.execute = interrupt_second
        with self.assertRaisesRegex(RuntimeError, "interrupted batch"):
            self.run_h3(project, document, reviewer)
        completed = reviewer.tasks[0]["task_id"]
        resumed = fixtures.FakeReviewExecutor()
        self.run_h3(project, document, resumed)
        self.assertNotIn(completed, [t["task_id"] for t in resumed.tasks])
        self.assertEqual(resumed.tasks[0]["task_id"], pending[0])
        rounds = read_json(self.control(project, document))["rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(len(rounds[0]["evidence_task_ids"]), 1 + len(resumed.tasks))

    def test_fix_patch_history_belongs_to_original_round(self):
        project, document = self.translated()
        document = self.run_h3(project, document, fixtures.FakeReviewExecutor(issue_once=True),
                               translation_backend=1, translation_executor=fixtures.FakeTranslationExecutor())
        rounds = read_json(self.control(project, document))["rounds"]
        self.assertEqual([r["fixes_applied"] for r in rounds], [1, 0])
        fix_id = rounds[0]["fix_task_ids"][0]
        accepted = read_json(project / "work/patches/translation" / f"{fix_id}.json")
        self.assertEqual(accepted["review_round"], rounds[0]["round"])
        self.assertEqual(accepted["review_task_ids"], rounds[0]["evidence_task_ids"])
        self.assertEqual(accepted["target_digest_after_merge"], rounds[0]["target_digest_after_fixes"])

    def test_noop_fixes_do_not_copy_round_or_repeat_review(self):
        project, document = self.translated()
        reviewer = fixtures.FakeReviewExecutor(issue_once=True)
        same = {u.id: u.target_text for u in document.units if u.translatable}
        with patch("private_translate.core.qa._translate_fixes", return_value=same):
            with self.assertRaisesRegex(RuntimeError, "H3_FIX_NO_TARGET_CHANGE"):
                self.run_h3(project, document, reviewer)
        self.assertEqual(len(reviewer.tasks), 1)
        rounds = read_json(self.control(project, document))["rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["fixes_applied"], 0)
        self.assertTrue(rounds[0]["fixes_completed"])

    def test_invalid_evidence_records_rejected_task_without_round(self):
        project, document = self.translated()
        reviewer = fixtures.FakeReviewExecutor()
        execute = reviewer.execute
        def invalid(task, *, model):
            result = execute(task, model=model)
            result["target_digest"] = "0" * 64
            return result
        reviewer.execute = invalid
        with self.assertRaisesRegex(ValueError, "target_digest"):
            self.run_h3(project, document, reviewer)
        self.assertFalse(self.control(project, document).exists())
        state = load_review_task_state(project, reviewer.tasks[0])
        self.assertEqual([r["status"] for r in state["history"]], ["pending", "rejected"])

    def test_legacy_partial_evidence_without_chapter_provenance_is_rejected(self):
        project, document = self.translated("First sentence.\n\nSecond sentence.")
        units = [u for u in document.units if u.translatable]
        task = create_review_task(document, units[0].section_id, "agent:reviewer", "api:translation-owner",
                                  self.language["review_rubric"], unit_ids=[units[0].id])
        task["context_capsule"].pop("chapter_target_digest")
        units[1].target_text += "改变"
        write_json(project / "work/document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STATE_UNKNOWN"):
            validate_review_evidence_current(project, self.evidence(task), task)
        self.assertFalse(self.control(project, document).exists())

    def test_legacy_partial_evidence_uses_round_digest_after_qa_reset(self):
        project, document = self.translated("First sentence.\n\nSecond sentence.")
        units = [u for u in document.units if u.translatable]
        task = create_review_task(document, units[0].section_id, "agent:reviewer", "api:translation-owner",
                                  self.language["review_rubric"], unit_ids=[units[0].id])
        task["context_capsule"].pop("chapter_target_digest")
        digest = review_target_digest([{"unit_id": u.id, "target_text": u.target_text} for u in units])
        write_json(self.control(project, document), {"rounds": [{"round": 1,
                   "target_digest": digest, "evidence_task_ids": [task["task_id"]]}]})
        for unit in units:
            unit.qa_status = "passed"
            unit.last_qa_target_hash = content_hash(unit.target_text)
        write_json(project / "work/document.json", document.to_dict())
        validate_review_evidence_current(project, self.evidence(task), task)
        units[1].target_text += "改变"
        write_json(project / "work/document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW_TASK"):
            validate_review_evidence_current(project, self.evidence(task), task)

    def test_review_task_factory_binds_entire_chapter_for_a_batch(self):
        project, document = self.translated("First sentence.\n\nSecond sentence.")
        units = [u for u in document.units if u.translatable]
        task = create_review_task(document, units[0].section_id, "agent:reviewer", "api:translation-owner",
                                  self.language["review_rubric"], unit_ids=[units[0].id])
        self.assertNotEqual(task["target_digest"], task["context_capsule"]["chapter_target_digest"])
        units[1].target_text += "改变"
        write_json(project / "work/document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "STALE_REVIEW_TASK"):
            validate_review_evidence_current(project, self.evidence(task), task)


if __name__ == "__main__":
    unittest.main()
