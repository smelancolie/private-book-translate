from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_agent_review as api_fixtures
import test_host_consistency as fixtures
from test_pipeline import approve_current_spotcheck
from private_translate.core.execution import PatchConflict
from private_translate.core.glossary import _read_rows, _write_rows, load_glossary
from private_translate.core.language import load_language_package
from private_translate.core.model import Document
from private_translate.core.pipeline import build_project, validate_project
from private_translate.core.qa import (
    run_book_consistency, run_book_consistency_agent, run_chapter_qa, run_chapter_qa_agent, validate_qa,
)
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json, write_json
from private_translate.host import advance_host_run, get_host_request


class QAReuseTests(unittest.TestCase):
    setUp = fixtures.HostConsistencyTests.setUp
    _project = fixtures.HostConsistencyTests._project
    _write_glossary = staticmethod(fixtures.HostConsistencyTests._write_glossary)
    _book = staticmethod(fixtures.HostConsistencyTests._book)
    _dispatch = fixtures.HostConsistencyTests._dispatch
    _process_advance_in_fresh_process = fixtures.HostConsistencyTests._process_advance_in_fresh_process

    def advance(self, project, **kwargs):
        return advance_host_run(
            project, capabilities=fixtures.DEFAULT_CAPABILITIES,
            agent_model=fixtures.TRANSLATION_MODEL,
            review_model=kwargs.pop("review_model", fixtures.REVIEW_MODEL),
            reviewer_id=kwargs.pop("reviewer_id", fixtures.REVIEWER_ID), **kwargs,
        )

    def finish(self, project, **kwargs):
        request = self.advance(project, **kwargs)
        while request["status"] == "awaiting_host":
            self._dispatch(project, request)
            request = self.advance(project, **kwargs)
        self.assertEqual(request["status"], "spotcheck_pending")
        return Document.from_dict(read_json(project / "work/document.json"))

    def passed_project(self, text=None):
        project, _ = self._project(text or self._book((2,)), approved_term=True, pretranslated=True)
        return project, self.finish(project)

    def test_same_requirements_resume_in_fresh_process_without_new_tasks_or_rounds(self):
        project, document = self.passed_project()
        controls = {path: read_json(path) for path in (project / "qa/chapter-controls").glob("*.json")}
        report = read_json(project / "qa/book-consistency.json")
        self.assertEqual(self._process_advance_in_fresh_process(project, fixtures.DEFAULT_CAPABILITIES)["status"],
                         "spotcheck_pending")
        self.assertEqual({path: read_json(path) for path in controls}, controls)
        self.assertEqual(read_json(project / "qa/book-consistency.json"), report)
        self.assertEqual(Document.from_dict(read_json(project / "work/document.json")).units, document.units)

    def test_changing_reviewer_or_model_creates_new_chapter_and_book_evidence(self):
        for settings in ({"reviewer_id": "agent:new-reviewer"}, {"review_model": "test:new-reviewer-model"}):
            with self.subTest(settings=settings):
                project, _ = self.passed_project()
                old_ids = {task["task_id"] for task, _ in self.worker.tasks}
                start = len(self.worker.tasks)
                self.finish(project, **settings)
                new_tasks = self.worker.tasks[start:]
                self.assertEqual({task["task_type"] for task, _ in new_tasks}, {"chapter_review", "book_consistency"})
                self.assertTrue(all(task["task_id"] not in old_ids for task, _ in new_tasks))
                self.assertTrue(all(task["reviewer_id"] == settings.get("reviewer_id", fixtures.REVIEWER_ID)
                                    and model == settings.get("review_model", fixtures.REVIEW_MODEL)
                                    for task, model in new_tasks))
                again = len(self.worker.tasks)
                self.finish(project, **settings)
                self.assertEqual(len(self.worker.tasks), again)

    def test_rubric_changes_reaudit_only_the_requested_scope(self):
        for key, expected_types in (("review_rubric", {"chapter_review"}),
                                    ("consistency_rubric", {"book_consistency"})):
            with self.subTest(key=key):
                project, _ = self.passed_project()
                language = load_language_package("en", "zh-Hans")
                language[key] += "\nRecheck all assigned source and target passages."
                start = len(self.worker.tasks)
                with patch("private_translate.core.pipeline.load_language_package", return_value=language):
                    self.finish(project)
                    self.assertEqual({task["task_type"] for task, _ in self.worker.tasks[start:]}, expected_types)
                    self.assertTrue(all(task["rubric"] == language[key] for task, _ in self.worker.tasks[start:]))
                    again = len(self.worker.tasks)
                    self.finish(project)
                    self.assertEqual(len(self.worker.tasks), again)

    def test_context_change_reaudits_only_the_affected_chapter(self):
        project, document = self.passed_project(self._book((2, 0)))
        document.sections[0].title = "A revised chapter label"
        write_json(project / "work/document.json", document.to_dict())
        reviewer = api_fixtures.FakeReviewExecutor()
        run_chapter_qa_agent(
            project, document, None, load_language_package("en", "zh-Hans"),
            load_glossary(project / "glossary/terms.csv"), reviewer,
            reviewer_id=fixtures.REVIEWER_ID, translation_owner_id="agent:translation-owner",
            review_model=fixtures.REVIEW_MODEL, translation_backend=1,
            capabilities=fixtures.DEFAULT_CAPABILITIES,
        )
        self.assertTrue(reviewer.tasks)
        self.assertEqual({task["chapter_id"] for task in reviewer.tasks}, {document.sections[0].section_id})
        self.assertTrue(all(task["context_capsule"]["section_title"] == document.sections[0].title
                            for task in reviewer.tasks))

    def test_owner_cannot_become_reviewer_even_when_pass_is_cached(self):
        project, document = self.passed_project()
        before = read_json(project / "work/document.json")
        with self.assertRaisesRegex(ValueError, "reviewer_id must differ"):
            run_chapter_qa_agent(
                project, document, None, load_language_package("en", "zh-Hans"), [], self.worker,
                reviewer_id="agent:translation-owner", translation_owner_id="agent:translation-owner",
                review_model=fixtures.REVIEW_MODEL, translation_backend=1,
                capabilities=fixtures.DEFAULT_CAPABILITIES,
            )
        self.assertEqual(read_json(project / "work/document.json"), before)

    def test_changed_requirements_pending_review_reuses_original_id_and_blocks_final_validation(self):
        project, _ = self.passed_project()
        settings = {"review_model": "test:new-reviewer-model"}
        request = self.advance(project, **settings)
        original = request["tasks"][0]
        self.assertEqual(original["model"], settings["review_model"])
        self.assertEqual(get_host_request(project), request)
        self.assertEqual(self.advance(project, **settings), request)
        with self.assertRaisesRegex(ValueError, "chapter QA is incomplete"):
            validate_project(project)
        self._dispatch(project, request)
        self.finish(project, **settings)
        self.assertEqual(sum(task["task_id"] == original["task"]["task_id"] for task, _ in self.worker.tasks), 1)

    def test_pending_book_review_rejects_changed_configuration_without_replacing_task(self):
        project, document = self.passed_project()
        language = load_language_package("en", "zh-Hans")
        language["consistency_rubric"] += "\nReview under the new book rules."
        with patch("private_translate.core.pipeline.load_language_package", return_value=language):
            request = self.advance(project)
            self.assertEqual(request["tasks"][0]["task"]["task_type"], "book_consistency")
            for settings in ({"review_model": "changed-model"}, {"reviewer_id": "changed-reviewer"},
                             {"language": {**language, "consistency_rubric": "Changed again"}}):
                with self.subTest(settings=settings):
                    with self.assertRaisesRegex(PatchConflict, "BOOK_CONSISTENCY_CONFIG_CHANGED"):
                        run_book_consistency_agent(
                            project, document, fixtures.DEFAULT_CAPABILITIES, settings.get("language", language),
                            load_glossary(project / "glossary/terms.csv"), self.worker,
                            reviewer_id=settings.get("reviewer_id", fixtures.REVIEWER_ID),
                            translation_owner_id="agent:translation-owner",
                            review_model=settings.get("review_model", fixtures.REVIEW_MODEL),
                            translation_executor=self.worker, translation_model=fixtures.TRANSLATION_MODEL,
                        )
                    self.assertEqual(get_host_request(project), request)
            self.finish(project)

    def test_term_approval_change_rejects_build_and_validate_then_reaudits(self):
        for change in ("approve", "modify", "revoke"):
            with self.subTest(change=change):
                project, _ = self.passed_project()
                approve_current_spotcheck(project, (811, 812))
                validate_project(project)
                build_project(project)
                rows = _read_rows(project / "glossary/terms.csv")
                if change == "approve":
                    rows.append({**rows[0], "source": "guides", "preferred_translation": "指引",
                                 "category": "term", "forbidden_translations": ""})
                elif change == "modify":
                    rows[0]["preferred_translation"] = "北极星号"
                else:
                    rows[0]["status"] = "rejected"
                for name in ("terms.csv", "candidates.csv"):
                    _write_rows(project / "glossary" / name, rows)
                for action in (build_project, validate_project):
                    with self.assertRaisesRegex(ValueError, "approved glossary"):
                        action(project)
                start = len(self.worker.tasks)
                execute = self.worker.execute

                def execute_fix(task, *, model):
                    result = execute(task, model=model)
                    if change == "modify" and task["task_type"] == "translation":
                        for row in result["translations"]:
                            row["target_text"] = row["target_text"].replace("北极星", "北极星号")
                    return result

                with patch.object(self.worker, "execute", side_effect=execute_fix):
                    document = self.finish(project)
                self.assertTrue(any(task["task_type"] == "chapter_review" for task, _ in self.worker.tasks[start:]))
                validate_qa(project, document)
                with patch("test_pipeline.create_spotcheck_round", side_effect=lambda project, document, seed:
                           create_spotcheck_round(project, document, seed, new_run=seed == 813)):
                    approve_current_spotcheck(project, (813, 814))
                build_project(project)
                validate_project(project)

    def test_irrelevant_glossary_change_preserves_chapter_evidence_but_refreshes_book_qa(self):
        project, _ = self.passed_project()
        rows = _read_rows(project / "glossary/terms.csv")
        rows.append({**rows[0], "source": "Absent Term", "preferred_translation": "未出现术语"})
        _write_rows(project / "glossary/terms.csv", rows)
        start = len(self.worker.tasks)
        self.finish(project)
        self.assertEqual({task["task_type"] for task, _ in self.worker.tasks[start:]}, {"book_consistency"})

    def test_api_caches_check_model_rules_and_context_and_final_glossary(self):
        project, document = self._project(self._book((2,)), approved_term=True, pretranslated=True)
        language = load_language_package("en", "zh-Hans")
        glossary = load_glossary(project / "glossary/terms.csv")
        provider = api_fixtures.FakeProvider()
        run_chapter_qa(project, document, provider, language, glossary)
        run_book_consistency(project, document, provider, language, glossary)
        before = provider.review_calls
        run_chapter_qa(project, document, provider, language, glossary)
        run_book_consistency(project, document, provider, language, glossary)
        self.assertEqual(provider.review_calls, before)
        provider.capabilities = replace(provider.capabilities, model="new-api-model")
        run_chapter_qa(project, document, provider, language, glossary)
        run_book_consistency(project, document, provider, language, glossary)
        self.assertGreater(provider.review_calls, before)
        before = provider.review_calls
        language = deepcopy(language)
        language["review_rubric"] += "\nUse the revised chapter rules."
        language["consistency_rubric"] += "\nUse the revised book rules."
        run_chapter_qa(project, document, provider, language, glossary)
        run_book_consistency(project, document, provider, language, glossary)
        self.assertGreater(provider.review_calls, before)
        self._write_glossary(project, "北极星号")
        with self.assertRaisesRegex(ValueError, "approved glossary"):
            validate_qa(project, document)


if __name__ == "__main__":
    unittest.main()
