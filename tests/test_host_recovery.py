import csv
from pathlib import Path
import unittest
from unittest.mock import patch

import test_host_consistency as fixtures
from private_translate.core import glossary, qa
from private_translate.core.execution import PatchConflict, set_review_task_status
from private_translate.core.model import content_hash
from private_translate.core.state import read_json, write_json
from private_translate.host import get_host_request, submit_host_result


class HostRecoveryTests(unittest.TestCase):
    setUp = fixtures.HostConsistencyTests.setUp
    _project = fixtures.HostConsistencyTests._project
    _write_glossary = staticmethod(fixtures.HostConsistencyTests._write_glossary)
    _book = staticmethod(fixtures.HostConsistencyTests._book)
    _advance = staticmethod(fixtures.HostConsistencyTests._advance)
    _dispatch = fixtures.HostConsistencyTests._dispatch
    _consistency_task = fixtures.HostConsistencyTests._consistency_task
    _process_advance_in_fresh_process = fixtures.HostConsistencyTests._process_advance_in_fresh_process

    def resume(self, project):
        return self._process_advance_in_fresh_process(project, fixtures.DEFAULT_CAPABILITIES)

    def terminology_result(self, project):
        request = self._advance(project)
        task = request["tasks"][0]["task"]
        self.assertEqual(task["task_type"], "terminology")
        self.assertEqual(len(task["source_keys"]), 2)
        result = {"schema_version": 1, "task_id": task["task_id"], "owner_id": task["owner_id"],
                  "proposals": [{"source": source, "preferred_translation": "术语：" + source}
                                for source in task["source_keys"]]}
        submit_host_result(project, result)
        return task, result

    def interrupt_terminology(self, project, window):
        candidates = project / "glossary/candidates.csv"
        original_rows = glossary._write_rows

        def save_rows(path, rows):
            original_rows(path, rows)
            if Path(path) == candidates and all(row["preferred_translation"] for row in rows):
                raise KeyboardInterrupt("after candidate CSV")

        def save_json(path, value):
            if window == "run_state" and value.get("status") == "terminology_ready":
                raise KeyboardInterrupt("before run state")
            if window == "host_request" and Path(path) == project / "work/host/request.json":
                raise KeyboardInterrupt("before host acknowledgement")
            write_json(path, value)

        if window == "candidate_csv":
            with patch.object(glossary, "_write_rows", side_effect=save_rows):
                with self.assertRaises(KeyboardInterrupt):
                    self._advance(project)
        else:
            module = "private_translate.core.pipeline" if window == "run_state" else "private_translate.host"
            with patch(module + ".write_json", side_effect=save_json):
                with self.assertRaises(KeyboardInterrupt):
                    self._advance(project)

    def test_terminology_apply_windows_resume_without_reproposal_or_autoapproval(self):
        for window in ("candidate_csv", "run_state", "host_request"):
            with self.subTest(window=window):
                project, _ = self._project(
                    "North Star guides Silver Anchor.\n\nSilver Anchor follows North Star."
                )
                task, result = self.terminology_result(project)
                self.interrupt_terminology(project, window)
                before = (project / "glossary/candidates.csv").read_bytes()
                self.assertEqual(get_host_request(project)["status"], "results_ready")
                self.assertEqual(self.resume(project)["status"], "terminology_ready")
                self.assertEqual((project / "glossary/candidates.csv").read_bytes(), before)
                self.assertEqual(
                    [path.stem for path in (project / "work/tasks/terminology").glob("*.json")],
                    [task["task_id"]],
                )
                self.assertEqual(read_json(project / "work/host/results" / f"{task['task_id']}.json"), result)
                with (project / "glossary/candidates.csv").open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertTrue(all(row["status"] == "proposed" for row in rows))
                with (project / "glossary/terms.csv").open(encoding="utf-8", newline="") as stream:
                    self.assertEqual(list(csv.DictReader(stream)), [])

    def test_applied_terminology_rejects_manual_and_partial_changes(self):
        project, _ = self._project("North Star guides Silver Anchor.\n\nSilver Anchor follows North Star.")
        self.terminology_result(project)
        self.interrupt_terminology(project, "candidate_csv")
        candidates = project / "glossary/candidates.csv"
        with candidates.open(encoding="utf-8", newline="") as stream:
            applied = list(csv.DictReader(stream))
        for field, value in (("status", "approved"), ("notes", "manual note"),
                             ("preferred_translation", "different proposal"), ("preferred_translation", "")):
            with self.subTest(field=field, value=value):
                rows = [dict(row) for row in applied]
                rows[0][field] = value
                glossary._write_rows(candidates, rows)
                before = candidates.read_bytes()
                with self.assertRaisesRegex(ValueError, "STALE_TERMINOLOGY_TASK"):
                    self._advance(project)
                self.assertEqual(candidates.read_bytes(), before)

    def consistency_fix(self):
        project, document = self._project(self._book((1, 1), extra_first_chapter=1),
                                          approved_term=True, pretranslated=True)
        wrong = next(unit for unit in document.units if fixtures.TERM in unit.source_text)
        wrong.target_text = wrong.target_text.replace(fixtures.PREFERRED, fixtures.FORBIDDEN)
        wrong.last_qa_target_hash = content_hash(wrong.target_text)
        write_json(project / "work/document.json", document.to_dict())
        request, _ = self._consistency_task(project)
        while request["tasks"][0]["task"]["task_type"] == "book_consistency":
            self._dispatch(project, request)
            request = self._advance(project)
        item = request["tasks"][0]
        self.assertTrue(item["task"]["context_capsule"]["batch"]["consistency_issues"])
        result = self.worker.execute(item["task"], model=item["model"])
        submit_host_result(project, result)
        return project, item["task"], result

    def interrupt_fix(self, project, task, window):
        archive = project / "work/patches/translation" / f"{task['task_id']}.json"

        def save(path, value):
            if window == "host_request" and Path(path) == project / "work/host/request.json":
                raise KeyboardInterrupt("before host acknowledgement")
            write_json(path, value)
            if window == "canonical" and Path(path) == project / "work/document.json":
                raise KeyboardInterrupt("after canonical apply")
            if Path(path) == archive and value.get("application_status") == window:
                raise KeyboardInterrupt("after patch archive")
            if (window == "fix_progress" and Path(path) == project / "qa/book-consistency.json"
                    and value.get("active_round", {}).get("fix_index") == 1):
                raise KeyboardInterrupt("after fix progress")

        module = {"canonical": "private_translate.core.execution",
                  "host_request": "private_translate.host"}.get(window, "private_translate.core.qa")
        if window == "canonical":
            apply_patch = qa.apply_translation_patch

            def apply_and_interrupt(project, result):
                with patch(module + ".write_json", side_effect=save):
                    return apply_patch(project, result)

            context = patch.object(qa, "apply_translation_patch", side_effect=apply_and_interrupt)
        else:
            context = patch(module + ".write_json", side_effect=save)
        with context:
            with self.assertRaises(KeyboardInterrupt):
                self._advance(project)

    def test_consistency_apply_windows_resume_one_fix_then_full_rechecks(self):
        for window in ("pending", "canonical", "applied", "fix_progress", "host_request"):
            with self.subTest(window=window):
                project, task, result = self.consistency_fix()
                self.interrupt_fix(project, task, window)
                request = self.resume(project)
                self.assertEqual(request["tasks"][0]["task"]["task_type"], "chapter_review")
                archive_path = project / "work/patches/translation" / f"{task['task_id']}.json"
                archive = read_json(archive_path)
                self.assertEqual(archive["application_status"], "applied")
                self.assertEqual(archive["patch"], result)
                self.assertEqual(archive["fixes_applied"], 1)
                before_replay = read_json(project / "work/document.json")
                self.assertEqual(self.resume(project), request)
                self.assertEqual(read_json(project / "work/document.json"), before_replay)
                review_types = []
                while request["status"] == "awaiting_host":
                    item, _ = self._dispatch(project, request)
                    review_types.append(item["task"]["task_type"])
                    request = self._advance(project)
                self.assertEqual(request["status"], "spotcheck_pending")
                self.assertIn("book_consistency", review_types)
                self.assertNotIn("translation", review_types)
                report = read_json(project / "qa/book-consistency.json")
                self.assertEqual([row["status"] for row in report["rounds"]], ["FIXED_RECHECK_REQUIRED", "PASS"])
                self.assertEqual(report["rounds"][0]["fixes_applied"], 1)
                self.assertEqual(report["rounds"][0]["fix_task_ids"], [task["task_id"]])
                self.assertEqual(read_json(archive_path), archive)

    def test_consistency_recovery_rejects_manual_changes_anywhere_and_new_glossary(self):
        project, task, _ = self.consistency_fix()
        self.interrupt_fix(project, task, "canonical")
        document_path = project / "work/document.json"
        applied = read_json(document_path)
        for unit in applied["units"]:
            if not unit["translatable"]:
                continue
            with self.subTest(unit_id=unit["id"]):
                changed = read_json(document_path)
                next(row for row in changed["units"] if row["id"] == unit["id"])["target_text"] += "人工修改"
                write_json(document_path, changed)
                with self.assertRaisesRegex(PatchConflict, "STALE_BOOK_CONSISTENCY_FIX"):
                    self._advance(project)
                self.assertEqual(read_json(document_path), changed)
                write_json(document_path, applied)
        self._write_glossary(project, "新术语")
        with self.assertRaisesRegex(PatchConflict, "STALE_BOOK_CONSISTENCY_FIX"):
            self._advance(project)
        self.assertEqual(read_json(document_path), applied)

    def test_review_write_windows_resume_in_fresh_process_without_repeat_dispatch(self):
        for window in ("pending", "task", "evidence", "completed"):
            with self.subTest(window=window):
                review_calls_before = sum(task["task_type"] == "chapter_review" for task, _ in self.worker.tasks)
                project, _ = self._project("Birds flew over water.")
                request = self._advance(project)
                self._dispatch(project, request)
                saved_task_ids = []

                def save_status(project, task, status, **details):
                    value = set_review_task_status(project, task, status, **details)
                    if status == window:
                        saved_task_ids.append(task["task_id"])
                        raise KeyboardInterrupt("after review lifecycle state")
                    return value

                def save(path, value):
                    write_json(path, value)
                    directory = "work/tasks/review" if window == "task" else "qa/review-evidence"
                    if Path(path).parent == project / directory:
                        saved_task_ids.append(value["task_id"])
                        raise KeyboardInterrupt("after review JSON")

                if window in {"evidence", "completed"}:
                    request = self._advance(project)
                    self._dispatch(project, request)
                target = "set_review_task_status" if window in {"pending", "completed"} else "write_json"
                callback = save_status if target == "set_review_task_status" else save
                with patch("private_translate.core.qa." + target, side_effect=callback):
                    with self.assertRaises(KeyboardInterrupt):
                        self._advance(project)
                self.assertEqual(len(saved_task_ids), 1)
                task_id = saved_task_ids[0]
                resumed = self.resume(project)
                if window in {"pending", "task"}:
                    resumed_id = resumed["tasks"][0]["task"]["task_id"]
                    if window == "task":
                        self.assertEqual(resumed_id, task_id)
                    else:
                        self.assertFalse((project / "work/tasks/review" / f"{task_id}.json").exists())
                    self._dispatch(project, resumed)
                    resumed = self.resume(project)
                    task_id = resumed_id
                self.assertEqual(resumed["status"], "spotcheck_pending")
                self.assertEqual(sum(task["task_type"] == "chapter_review" for task, _ in self.worker.tasks)
                                 - review_calls_before, 1)
                state = read_json(project / "work/task-state/review" / f"{task_id}.json")
                self.assertEqual([row["status"] for row in state["history"]], ["pending", "completed"])
                control = next((project / "qa/chapter-controls").glob("*.json"))
                self.assertEqual(read_json(control)["rounds"][0]["evidence_task_ids"], [task_id])


if __name__ == "__main__":
    unittest.main()
