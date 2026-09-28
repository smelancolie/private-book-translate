from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch as mock_patch

from private_translate.cli import main, parser
from private_translate.core.execution import (
    PatchConflict,
    SCHEMAS,
    apply_translation_patch,
    create_review_task,
    create_translation_task,
    select_translation_backend,
    validate_review_evidence,
    validate_review_task,
    validate_translation_patch,
)
from private_translate.core.model import Document, content_hash
from private_translate.core.pipeline import build_project, run_project
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter


class ApiProvider:
    capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)

    def __init__(self):
        self.translation_calls = 0

    def translate(self, units, context, glossary, config):
        self.translation_calls += 1
        return {unit.id: "译文：" + unit.source_text for unit in units}

    def review(self, source, target, context, rubric, config):
        return []


class ExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.temp_root = self.repo / "projects" / f".test-execution-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.assertTrue(self.temp_root.resolve().is_relative_to((self.repo / "projects").resolve()))
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def project(self, text="# First\n\nAda greets the reader.\n\n# Second\n\nBob answers.\n"):
        source = self.temp_root / "book.md"
        source.write_text(text, encoding="utf-8")
        project = create_project(source, self.temp_root / "projects")
        document = MarkdownAdapter().import_file(project / "source" / "book.md", "en", "zh-Hans")
        write_json(project / "work" / "document.json", document.to_dict())
        return project, document

    def task(self, project, document, index=0, **kwargs):
        return create_translation_task(project, document.sections[index].section_id, "translator-1", **kwargs)

    def patch(self, task_path, replacements=None):
        task = read_json(task_path)
        replacements = replacements or {}
        translations = [
            {"unit_id": unit["id"], "source_hash": unit["source_hash"],
             "target_text": replacements.get(unit["id"], "译文：" + unit["source_text"])}
            for unit in task["units"] if unit["id"] in task["required_unit_ids"]
        ]
        return {
            "schema_version": 1, "patch_id": str(uuid.uuid4()), "task_id": task["task_id"],
            "task_type": "translation", "chapter_id": task["chapter_id"], "owner_id": task["owner_id"],
            "base_document_revision": task["base_document_revision"],
            "base_chapter_digest": task["base_chapter_digest"], "translations": translations,
        }

    def test_schemas_are_machine_readable_and_include_all_contracts(self):
        self.assertEqual(set(SCHEMAS), {"TranslationTask", "TranslationPatch", "ReviewTask", "ReviewEvidence"})
        self.assertIn("proper_noun_slice", SCHEMAS["TranslationTask"]["required"])
        self.assertIn("translation_quality_rules", SCHEMAS["TranslationTask"]["required"])
        self.assertIn("translation_owner_id", SCHEMAS["ReviewEvidence"]["required"])
        json.dumps(SCHEMAS)

    def test_task_is_persisted_as_snapshot_with_quality_capsule(self):
        project, document = self.project()
        path = self.task(project, document)
        task = read_json(path)
        self.assertEqual(task["task_type"], "translation")
        self.assertEqual(task["required_unit_ids"], task["unit_ids"])
        self.assertGreaterEqual(len(task["style_rules"]), 5)
        self.assertIn("natural Simplified Chinese", " ".join(task["translation_quality_rules"]))
        self.assertEqual(task["output_contract"]["schema"], "TranslationPatch")
        snapshot = path.read_text(encoding="utf-8")
        document.units[0].source_text = "later in-memory change"
        self.assertEqual(path.read_text(encoding="utf-8"), snapshot)

    def test_valid_translation_patch_merges(self):
        project, document = self.project()
        task_path = self.task(project, document)
        patch = self.patch(task_path)
        validate_translation_patch(project, patch)
        merged = apply_translation_patch(project, patch)
        task = read_json(task_path)
        applied = [unit for unit in merged.units if unit.section_id == task["chapter_id"] and unit.translatable]
        self.assertTrue(all(unit.translation_status == "translated" for unit in applied))
        self.assertTrue(all(unit.target_text.startswith("译文：") for unit in applied))

    def test_wrong_schema_task_owner_chapter_and_unknown_task_rejected(self):
        project, document = self.project()
        task_path = self.task(project, document)
        patch = self.patch(task_path)
        bad = dict(patch, schema_version=2)
        with self.assertRaisesRegex(ValueError, "schema"):
            validate_translation_patch(project, bad)
        bad = dict(patch, task_type="review")
        with self.assertRaisesRegex(ValueError, "task type"):
            validate_translation_patch(project, bad)
        bad = dict(patch, owner_id="someone-else")
        with self.assertRaisesRegex(ValueError, "owner_id"):
            validate_translation_patch(project, bad)
        bad = dict(patch, chapter_id="different-chapter")
        with self.assertRaisesRegex(ValueError, "another chapter"):
            validate_translation_patch(project, bad)
        bad = dict(patch, task_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_TASK"):
            validate_translation_patch(project, bad)

    def test_unknown_cross_chapter_missing_duplicate_and_empty_units_rejected(self):
        project, document = self.project()
        first_path = self.task(project, document, 0)
        patch = self.patch(first_path)
        required = patch["translations"]
        extra_id = next(unit.id for unit in document.units if unit.section_id == document.sections[1].section_id)
        other = next(unit for unit in document.units if unit.id == extra_id)
        cross_chapter = {"unit_id": other.id, "source_hash": other.source_hash, "target_text": "错"}
        with self.assertRaisesRegex(ValueError, "another chapter"):
            validate_translation_patch(project, dict(patch, translations=required + [cross_chapter]))
        with self.assertRaisesRegex(ValueError, "Unknown translation unit"):
            validate_translation_patch(project, dict(patch, translations=required + [{"unit_id": "unknown", "source_hash": "0" * 64, "target_text": "x"}]))
        with self.assertRaisesRegex(ValueError, "Missing required units"):
            validate_translation_patch(project, dict(patch, translations=required[:-1]))
        with self.assertRaisesRegex(ValueError, "Duplicate translation unit"):
            validate_translation_patch(project, dict(patch, translations=required + [required[0]]))
        empty = [dict(item) for item in required]
        empty[0]["target_text"] = "  "
        with self.assertRaisesRegex(ValueError, "Empty required target"):
            validate_translation_patch(project, dict(patch, translations=empty))

    def test_stale_source_hash_and_invalid_target_type_rejected(self):
        project, document = self.project()
        task_path = self.task(project, document)
        patch = self.patch(task_path)
        stale_entries = [dict(item) for item in patch["translations"]]
        stale_entries[0]["source_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Stale source hash"):
            validate_translation_patch(project, dict(patch, translations=stale_entries))
        invalid_entries = [dict(item) for item in patch["translations"]]
        invalid_entries[0]["target_text"] = 42
        with self.assertRaisesRegex(ValueError, "target_text must be a string"):
            validate_translation_patch(project, dict(patch, translations=invalid_entries))

    def test_non_translatable_unit_cannot_be_patched(self):
        project, document = self.project()
        unit = next(unit for unit in document.units if unit.translatable)
        unit.translatable = False
        write_json(project / "work" / "document.json", document.to_dict())
        task_path = self.task(project, document)
        patch = self.patch(task_path)
        patch["translations"].append({"unit_id": unit.id, "source_hash": unit.source_hash, "target_text": "changed"})
        with self.assertRaisesRegex(ValueError, "Non-translatable unit"):
            validate_translation_patch(project, patch)

    def test_protected_format_marker_corruption_rejected(self):
        project, document = self.project("# First\n\nA **bold** sentence.\n")
        task_path = self.task(project, document)
        task = read_json(task_path)
        unit = next(unit for unit in task["units"] if unit["protected_tokens"])
        patch = self.patch(task_path)
        entry = next(item for item in patch["translations"] if item["unit_id"] == unit["id"])
        entry["target_text"] = "译文：普通文本"
        with self.assertRaisesRegex(ValueError, "Protected marker"):
            validate_translation_patch(project, patch)

    def test_proper_noun_placeholder_corruption_rejected(self):
        project, document = self.project("# First\n\nAda {{pn:person_ada_001}} greets us.\n")
        task_path = self.task(project, document)
        task = read_json(task_path)
        unit = next(unit for unit in task["units"] if "{{pn:" in unit["source_text"])
        patch = self.patch(task_path)
        entry = next(item for item in patch["translations"] if item["unit_id"] == unit["id"])
        entry["target_text"] = entry["target_text"].replace("{{pn:person_ada_001}}", "")
        with self.assertRaisesRegex(ValueError, "placeholder corruption"):
            validate_translation_patch(project, patch)

    def test_locked_proper_noun_contract_is_task_scoped(self):
        project, document = self.project()
        first_unit = next(unit for unit in document.units if unit.section_id == document.sections[0].section_id and "Ada" in unit.source_text)
        entity = {
            "entity_id": "person_ada_001", "source_form": "Ada", "normalized_source": "ada",
            "category": "person", "preferred_translation": "艾达", "first_rendering": "艾达（Ada）",
            "subsequent_rendering": "艾达", "display_policy": "first_bilingual_then_translated", "status": "approved",
            "forbidden_renderings": ["阿达"],
        }
        occurrence = {
            "entity_id": "person_ada_001", "chapter_id": first_unit.section_id, "unit_id": first_unit.id,
            "occurrence_index": 0, "is_body_first_occurrence": True, "is_title_occurrence": False,
            "rendering": "艾达（Ada）",
        }
        first_path = self.task(project, document, 0, proper_noun_slice=[entity], proper_noun_occurrences=[occurrence])
        valid = self.patch(first_path, {first_unit.id: "艾达（Ada）：初次出现"})
        validate_translation_patch(project, valid)
        invalid = self.patch(first_path, {first_unit.id: "阿达：初次出现"})
        with self.assertRaisesRegex(ValueError, "Locked proper-noun contract violation"):
            validate_translation_patch(project, invalid)

        second_path = self.task(project, document, 1)
        second_patch = self.patch(second_path, {next(unit.id for unit in document.units if unit.section_id == document.sections[1].section_id): "阿达：本章普通文本"})
        validate_translation_patch(project, second_patch)

    def test_stale_same_chapter_patch_rejected(self):
        project, document = self.project()
        task_path = self.task(project, document)
        patch = self.patch(task_path)
        document.units[0].target_text = "a newer canonical target"
        write_json(project / "work" / "document.json", document.to_dict())
        with self.assertRaisesRegex(PatchConflict, "PATCH_CONFLICT"):
            validate_translation_patch(project, patch)

    def test_unrelated_chapter_patch_remains_valid(self):
        project, document = self.project()
        first = self.task(project, document, 0)
        second = self.task(project, document, 1)
        first_patch, second_patch = self.patch(first), self.patch(second)
        apply_translation_patch(project, first_patch)
        validate_translation_patch(project, second_patch)
        merged = apply_translation_patch(project, second_patch)
        self.assertTrue(all(unit.translation_status == "translated" for unit in merged.units if unit.translatable))

    def test_apply_invalidates_only_relevant_chapter_qa(self):
        project, document = self.project()
        for unit in document.units:
            if unit.translatable:
                unit.target_text = "旧译文：" + unit.source_text
                unit.translation_status = "translated"
                unit.qa_status = "passed"
                unit.last_qa_target_hash = content_hash(unit.target_text)
        write_json(project / "work" / "document.json", document.to_dict())
        for section in document.sections:
            write_json(project / "qa" / "chapter-controls" / f"{section.section_id}.json", {"rounds": [], "latest_round_status": "PASS"})
        write_json(project / "qa" / "book-consistency.json", {"status": "PASS", "rounds": []})
        task_path = self.task(project, document)
        apply_translation_patch(project, self.patch(task_path))
        merged = Document.from_dict(read_json(project / "work" / "document.json"))
        first_id, second_id = (section.section_id for section in document.sections)
        first = [unit for unit in merged.units if unit.section_id == first_id and unit.translatable]
        second = [unit for unit in merged.units if unit.section_id == second_id and unit.translatable]
        self.assertTrue(all(unit.qa_status == "pending" for unit in first))
        self.assertTrue(all(unit.qa_status == "passed" and unit.target_text.startswith("旧译文：") for unit in second))
        self.assertEqual(read_json(project / "qa" / "chapter-controls" / f"{first_id}.json")["latest_round_status"], "STALE")
        self.assertEqual(read_json(project / "qa" / "chapter-controls" / f"{second_id}.json")["latest_round_status"], "PASS")
        report = read_json(project / "qa" / "book-consistency.json")
        self.assertEqual(report["status"], "STALE")
        self.assertEqual(report["stale_chapters"], [first_id])

    def test_review_task_binds_owner_reviewer_and_review_evidence_target_digest(self):
        project, document = self.project()
        for unit in document.units:
            if unit.translatable:
                unit.target_text = "目标译文"
                unit.translation_status = "translated"
        task = create_review_task(document, document.sections[0].section_id, "reviewer-2", "translator-1", "Check fidelity and polysemy.")
        validate_review_task(task)
        self.assertEqual((task["reviewer_id"], task["translation_owner_id"]), ("reviewer-2", "translator-1"))
        evidence = {
            "schema_version": 1, "task_id": task["task_id"], "chapter_id": task["chapter_id"],
            "reviewer_id": task["reviewer_id"], "translation_owner_id": task["translation_owner_id"],
            "base_chapter_digest": task["base_chapter_digest"], "target_digest": task["target_digest"],
            "issues": [], "summary": "Reviewed the chapter.",
        }
        validate_review_evidence(evidence, task)
        with self.assertRaisesRegex(ValueError, "target_digest"):
            validate_review_evidence(dict(evidence, target_digest="0" * 64), task)

    def test_backend_default_is_agent_and_option_two_runs_api(self):
        default_args = parser().parse_args(["run", "unused-project"])
        self.assertEqual(default_args.translation_backend, 1)
        with self.assertRaisesRegex(NotImplementedError, "AGENT_BACKEND_NOT_IMPLEMENTED"):
            select_translation_backend(1)
        self.assertEqual(select_translation_backend(2), "api")

        project, _ = self.project("# First\n\nHello.\n")
        with mock_patch("private_translate.cli.run_project", return_value=[]) as cli_run:
            self.assertEqual(main(["run", str(project), "--translation-backend", "2"]), 0)
            self.assertEqual(cli_run.call_args.kwargs["translation_backend"], 2)
        provider = ApiProvider()
        outputs = run_project(project, bilingual=True, provider=provider)
        self.assertEqual(provider.translation_calls, 1)
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all(path.is_file() for path in outputs))
        run_state = read_json(project / "state" / "state.json")
        self.assertEqual((run_state["requested_backend"], run_state["actual_backend"]), (2, 2))
        canonical = Document.from_dict(read_json(project / "work" / "document.json"))
        self.assertTrue(all(unit.target_text and unit.translation_status == "translated" for unit in canonical.units if unit.translatable))
        with mock_patch("private_translate.core.pipeline.validate_spotcheck", return_value={"run_id": "review-1"}):
            build_project(project)
        run_state = read_json(project / "state" / "state.json")
        self.assertEqual((run_state["requested_backend"], run_state["actual_backend"]), (2, 2))

    def test_backend_one_does_not_fall_back_or_call_api(self):
        project, _ = self.project()
        provider = ApiProvider()
        with self.assertRaisesRegex(NotImplementedError, "AGENT_BACKEND_NOT_IMPLEMENTED"):
            run_project(project, provider=provider, translation_backend=1)
        self.assertEqual(provider.translation_calls, 0)
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["run", str(project)]), 1)
        self.assertIn("AGENT_BACKEND_NOT_IMPLEMENTED", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
