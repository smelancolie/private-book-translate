from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from private_translate.core.execution import (
    apply_translation_patch,
    create_translation_task,
    load_translation_task,
    validate_translation_patch,
)
from private_translate.core.language import load_language_package
from private_translate.core.model import Document
from private_translate.core.pipeline import DEFAULT_AGENT_MODEL, _translate_agent, run_project
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter


class FakeProvider:
    capabilities = ProviderCapabilities("fake", "qa-test", 116000, 16000, True, False)

    def __init__(self):
        self.translation_calls = 0

    def translate(self, units, context, glossary, config):
        self.translation_calls += 1
        return {unit.id: "API译文：" + unit.source_text for unit in units}

    def review(self, source, target, context, rubric, config):
        return []


class FakeAgentExecutor:
    def __init__(self, project: Path | None = None, translations: dict[str, str] | None = None, on_first=None, partial=False):
        self.project = project
        self.translations = translations or {}
        self.on_first = on_first
        self.partial = partial
        self.tasks = []
        self.models = []

    def execute(self, task: dict, *, model: str) -> dict:
        self.tasks.append(json.loads(json.dumps(task, ensure_ascii=False)))
        self.models.append(model)
        if self.on_first and len(self.tasks) == 1:
            self.on_first()
        translations = [
            {
                "unit_id": unit_id,
                "source_hash": next(unit["source_hash"] for unit in task["units"] if unit["id"] == unit_id),
                "target_text": self.translations.get(
                    unit_id,
                    "Agent译文：" + next(unit["source_text"] for unit in task["units"] if unit["id"] == unit_id),
                ),
            }
            for unit_id in task["required_unit_ids"]
        ]
        if self.partial:
            translations = translations[:-1]
        return {
            "schema_version": 1,
            "patch_id": str(uuid.uuid4()),
            "task_id": task["task_id"],
            "task_type": "translation",
            "chapter_id": task["chapter_id"],
            "owner_id": task["owner_id"],
            "base_document_revision": task["base_document_revision"],
            "base_chapter_digest": task["base_chapter_digest"],
            "translations": translations,
            "model": model,
        }


class AgentBackendTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.language = load_language_package("en", "zh-Hans")
        self.temp_root = self.repo / "projects" / f".test-agent-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.assertTrue(self.temp_root.resolve().is_relative_to((self.repo / "projects").resolve()))
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def project(self, text="# First\n\nThe river runs west.\n\n# Second\n\nThe wind grows quiet.\n"):
        source = self.temp_root / "book.md"
        source.write_text(text, encoding="utf-8")
        project = create_project(source, self.temp_root / "workspaces")
        document = MarkdownAdapter().import_file(project / "source" / "book.md", "en", "zh-Hans")
        write_json(project / "work" / "document.json", document.to_dict())
        return project, document

    def test_agent_translation_task_roundtrip_and_quality_capsule(self):
        project, document = self.project("# First\n\nThe river runs west.\n")
        task_path = create_translation_task(project, document.sections[0].section_id, "worker-a")
        task = load_translation_task(project, task_path.stem)
        executor = FakeAgentExecutor()
        patch = executor.execute(task, model=DEFAULT_AGENT_MODEL)
        self.assertIn("natural Simplified Chinese", " ".join(executor.tasks[0]["translation_quality_rules"]))
        self.assertEqual(executor.models, ["gpt-6-luna-max"])
        validate_translation_patch(project, patch)
        merged = apply_translation_patch(project, patch)
        self.assertTrue(all(unit.target_text.startswith("Agent译文：") for unit in merged.units if unit.translatable))

    def test_agent_proper_noun_contract_survives_roundtrip(self):
        project, document = self.project("# First\n\n{{pn:person_ada}} greets the reader.\n")
        unit = next(unit for unit in document.units if unit.translatable and "{{pn:" in unit.source_text)
        entity = {
            "entity_id": "person_ada", "source_form": "Ada", "normalized_source": "ada",
            "category": "person", "preferred_translation": "艾达", "first_rendering": "艾达（Ada）",
            "subsequent_rendering": "艾达", "display_policy": "first_bilingual_then_translated", "status": "approved",
            "forbidden_renderings": ["阿达"],
        }
        occurrence = {
            "entity_id": "person_ada", "chapter_id": unit.section_id, "unit_id": unit.id,
            "occurrence_index": 0, "is_body_first_occurrence": True, "is_title_occurrence": False,
            "rendering": "艾达（Ada）", "placeholder": "{{pn:person_ada}}",
        }
        task_path = create_translation_task(
            project, unit.section_id, "worker-a", proper_noun_slice=[entity],
            proper_noun_occurrences=[occurrence],
        )
        task = load_translation_task(project, task_path.stem)
        self.assertEqual(task["proper_noun_slice"], [entity])
        executor = FakeAgentExecutor(translations={unit.id: "{{pn:person_ada}}向读者问候。"})
        patch = executor.execute(task, model=DEFAULT_AGENT_MODEL)
        validate_translation_patch(project, patch)
        merged = apply_translation_patch(project, patch)
        self.assertEqual(next(row for row in merged.units if row.id == unit.id).target_text, "艾达（Ada）向读者问候。")

    def test_agent_patch_resume_and_target_only_bilingual_share_canonical_target(self):
        project, _ = self.project(
            "# First\n\nThe river runs west.\n\nIt reaches the sea.\n\n# Second\n\nThe wind grows quiet.\n",
        )
        provider = FakeProvider()
        executor = FakeAgentExecutor(project)
        outputs = run_project(project, bilingual=True, provider=provider, translation_backend=1, agent_executor=executor)
        first_call_count = len(executor.tasks)
        self.assertEqual(len(outputs), 2)
        self.assertGreater(first_call_count, 0)
        self.assertEqual(first_call_count, 2)
        self.assertTrue(all(len(task["required_unit_ids"]) > 1 for task in executor.tasks))
        self.assertEqual({task["owner_id"] for task in executor.tasks}, {"agent:translation-owner"})
        self.assertEqual(provider.translation_calls, 0)
        self.assertEqual(read_json(project / "state" / "state.json")["actual_backend"], 1)
        canonical = Document.from_dict(read_json(project / "work" / "document.json"))
        targets = [unit.target_text for unit in canonical.units if unit.translatable]
        self.assertTrue(all(target and target.startswith("Agent译文：") for target in targets))
        self.assertTrue(all(target in outputs[0].read_text(encoding="utf-8") for target in targets))
        self.assertTrue(all(target in outputs[1].read_text(encoding="utf-8") for target in targets))

        run_project(project, bilingual=True, provider=provider, translation_backend=1, agent_executor=executor)
        self.assertEqual(len(executor.tasks), first_call_count)
        accepted = list((project / "work" / "patches" / "translation").glob("*.json"))
        self.assertEqual(len(accepted), first_call_count)
        provenance = [read_json(path) for path in accepted]
        required = {
            "task_id", "patch_id", "chapter_id", "owner_id", "requested_backend", "actual_backend",
            "model", "base_chapter_digest", "accepted_at", "target_digest_after_merge",
        }
        for record in provenance:
            self.assertTrue(required <= record.keys())
            self.assertEqual((record["requested_backend"], record["actual_backend"]), (1, 1))
            self.assertEqual(record["patch_id"], record["patch"]["patch_id"])

    def test_resume_dispatches_only_pending_chapters_with_stable_owner(self):
        project, document = self.project("# First\n\nThe river runs west.\n\n# Second\n\nThe wind grows quiet.\n")

        class InterruptAfterFirstBatch:
            def __init__(self):
                self.fake = FakeAgentExecutor()
                self.calls = 0

            def execute(self, task, *, model):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("simulated worker interruption")
                return self.fake.execute(task, model=model)

        interrupted = InterruptAfterFirstBatch()
        with self.assertRaisesRegex(RuntimeError, "simulated worker interruption"):
            _translate_agent(document, project, FakeProvider(), self.language, [], interrupted, DEFAULT_AGENT_MODEL)
        accepted_chapter = document.sections[0].section_id
        accepted_files = list((project / "work" / "patches" / "translation").glob("*.json"))
        self.assertEqual(len(accepted_files), 1)
        self.assertEqual(read_json(accepted_files[0])["chapter_id"], accepted_chapter)

        executor = FakeAgentExecutor()
        run_project(project, provider=FakeProvider(), translation_backend=1, agent_executor=executor)
        self.assertTrue(executor.tasks)
        self.assertTrue(all(task["chapter_id"] != accepted_chapter for task in executor.tasks))
        self.assertEqual({task["owner_id"] for task in executor.tasks}, {"agent:translation-owner"})
        dispatched = len(executor.tasks)
        run_project(project, provider=FakeProvider(), translation_backend=1, agent_executor=executor)
        self.assertEqual(len(executor.tasks), dispatched)

    def test_invalid_agent_patch_does_not_modify_canonical_state(self):
        project, document = self.project("# First\n\nThe river runs west.\n")
        document_path = project / "work" / "document.json"
        original = document_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Missing required units"):
            _translate_agent(document, project, FakeProvider(), self.language, [], FakeAgentExecutor(partial=True), DEFAULT_AGENT_MODEL)
        self.assertEqual(document_path.read_bytes(), original)

    def test_stale_agent_patch_regenerates_task_from_current_canonical_state(self):
        project, document = self.project("# First\n\nThe river runs west.\n")

        def concurrent_change():
            path = project / "work" / "document.json"
            current = Document.from_dict(read_json(path))
            current.units[0].target_text = "Concurrent canonical edit"
            current.units[0].translation_status = "translated"
            write_json(path, current.to_dict())

        executor = FakeAgentExecutor(project, on_first=concurrent_change)
        merged = _translate_agent(document, project, FakeProvider(), self.language, [], executor, DEFAULT_AGENT_MODEL)
        self.assertEqual(len(executor.tasks), 2)
        self.assertNotEqual(executor.tasks[0]["task_id"], executor.tasks[1]["task_id"])
        self.assertNotEqual(executor.tasks[0]["base_chapter_digest"], executor.tasks[1]["base_chapter_digest"])
        self.assertTrue(merged.units[0].target_text.startswith("Agent译文："))

    def test_agent_executor_receives_task_without_workspace_handle(self):
        project, document = self.project("# First\n\nThe river runs west.\n")
        executor = FakeAgentExecutor()
        _translate_agent(document, project, FakeProvider(), self.language, [], executor, DEFAULT_AGENT_MODEL)
        task = executor.tasks[0]
        self.assertNotIn("project_path", task)
        self.assertNotIn("document_path", task)
        self.assertEqual(set(executor.models), {DEFAULT_AGENT_MODEL})
        self.assertIn("candidate A/B/C and synthesize D", " ".join(task["translation_quality_rules"]))


if __name__ == "__main__":
    unittest.main()
