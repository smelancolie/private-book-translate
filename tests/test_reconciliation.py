from pathlib import Path
import tempfile
import unittest
import uuid

from private_translate.core.model import Document, Section, TranslationUnit
from private_translate.core.pipeline import assert_project_source_current, run_project
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.state import merge_saved_state, read_json
from private_translate.formats.markdown import MarkdownAdapter


class IdentityProvider:
    def __init__(self):
        self.capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)
        self.translation_calls = []

    def translate(self, units, context, glossary, config):
        self.translation_calls.append([unit.id for unit in units])
        return {unit.id: f"译：{unit.source_text}" for unit in units}

    def review(self, source, target, context, rubric, config):
        return []


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.temp = tempfile.TemporaryDirectory(dir=self.repo / "tests")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, text):
        path = self.root / "book.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reconciliation_remaps_figure_neighbor_ids(self):
        old_paragraph = TranslationUnit.create(
            section_id="chapter", type="paragraph", source_text="Caption.", translatable=True,
            source_language="en", target_language="zh-Hans", locator={"path": [0]},
        )
        old_figure = TranslationUnit.create(
            section_id="chapter", type="figure", source_text="Cover", translatable=False,
            source_language="en", target_language="zh-Hans", locator={"path": [1]},
            metadata={"asset_path": "cover.png", "asset_hash": "asset-hash", "alt": "Cover",
                     "caption_unit_ids": [old_paragraph.id], "surrounding_unit_ids": [old_paragraph.id],
                     "surrounding_text": [old_paragraph.source_text]},
        )
        saved = Document("markdown", "en", "zh-Hans", {}, [Section("chapter", "", {})], [old_paragraph, old_figure])
        new_paragraph = TranslationUnit.create(
            section_id="chapter", type="paragraph", source_text="Caption.", translatable=True,
            source_language="en", target_language="zh-Hans", locator={"path": [0]},
        )
        new_figure = TranslationUnit.create(
            section_id="chapter", type="figure", source_text="Cover", translatable=False,
            source_language="en", target_language="zh-Hans", locator={"path": [1]},
            metadata={"asset_path": "cover.png", "asset_hash": "asset-hash", "alt": "Cover",
                     "caption_unit_ids": [new_paragraph.id], "surrounding_unit_ids": [new_paragraph.id],
                     "surrounding_text": [new_paragraph.source_text]},
        )
        fresh = Document("markdown", "en", "zh-Hans", {}, [Section("chapter", "", {})], [new_paragraph, new_figure])

        merged = merge_saved_state(fresh, saved)

        self.assertEqual(merged.units[0].id, old_paragraph.id)
        self.assertEqual(merged.units[1].id, old_figure.id)
        self.assertEqual(merged.units[1].metadata["caption_unit_ids"], [old_paragraph.id])
        self.assertEqual(merged.units[1].metadata["surrounding_unit_ids"], [old_paragraph.id])

    def test_persistent_ids_survive_preceding_paragraph_insertion(self):
        path = self.write_source("# Chapter\n\nA paragraph.\n\nB paragraph.\n\nC paragraph.\n")
        adapter = MarkdownAdapter()
        saved = adapter.import_file(path, "en", "zh-Hans")
        for unit in saved.units:
            unit.target_text = f"译：{unit.source_text}"
            unit.translation_status = "translated"
            unit.qa_status = "passed"
        old = {unit.source_text: unit.id for unit in saved.units}
        old_targets = {unit.source_text: unit.target_text for unit in saved.units}

        path.write_text("# Chapter\n\nNEW paragraph.\n\nA paragraph.\n\nB paragraph.\n\nC paragraph.\n", encoding="utf-8")
        fresh = merge_saved_state(adapter.import_file(path, "en", "zh-Hans"), saved)

        by_text = {unit.source_text: unit for unit in fresh.units}
        for text in ("A paragraph.", "B paragraph.", "C paragraph."):
            self.assertEqual(by_text[text].id, old[text])
            self.assertEqual(by_text[text].target_text, old_targets[text])
        self.assertNotIn(by_text["NEW paragraph."].id, set(old.values()))

    def test_persistent_ids_survive_preceding_heading_insertion(self):
        path = self.write_source("# First\n\nOne.\n\n# Second\n\nTwo.\n")
        adapter = MarkdownAdapter()
        saved = adapter.import_file(path, "en", "zh-Hans")
        old = {(unit.type, unit.source_text): unit.id for unit in saved.units}

        path.write_text("# Inserted\n\nNew.\n\n# First\n\nOne.\n\n# Second\n\nTwo.\n", encoding="utf-8")
        fresh = merge_saved_state(adapter.import_file(path, "en", "zh-Hans"), saved)

        by_text = {(unit.type, unit.source_text): unit.id for unit in fresh.units}
        for key, unit_id in old.items():
            self.assertEqual(by_text[key], unit_id)

    def test_duplicate_source_paragraphs_receive_distinct_persistent_ids(self):
        path = self.write_source("# Chapter\n\nRepeated.\n\nRepeated.\n")
        adapter = MarkdownAdapter()
        saved = adapter.import_file(path, "en", "zh-Hans")
        repeated_ids = [unit.id for unit in saved.units if unit.source_text == "Repeated."]
        self.assertEqual(len(repeated_ids), 2)
        self.assertEqual(len(set(repeated_ids)), 2)
        for unit in saved.units:
            uuid.UUID(unit.id)

        path.write_text("# Chapter\n\nNEW.\n\nRepeated.\n\nRepeated.\n", encoding="utf-8")
        fresh = merge_saved_state(adapter.import_file(path, "en", "zh-Hans"), saved)
        after = [unit.id for unit in fresh.units if unit.source_text == "Repeated."]
        self.assertEqual(after, repeated_ids)

    def test_changed_paragraph_keeps_identity_but_becomes_stale(self):
        path = self.write_source("# Chapter\n\nOriginal paragraph.\n")
        adapter = MarkdownAdapter()
        saved = adapter.import_file(path, "en", "zh-Hans")
        old = next(unit for unit in saved.units if unit.source_text == "Original paragraph.")
        old.target_text = "原段落。"
        old.translation_status = "translated"
        old.qa_status = "passed"

        path.write_text("# Chapter\n\nEdited paragraph.\n", encoding="utf-8")
        fresh = merge_saved_state(adapter.import_file(path, "en", "zh-Hans"), saved)
        edited = next(unit for unit in fresh.units if unit.source_text == "Edited paragraph.")
        self.assertEqual(edited.id, old.id)
        self.assertEqual(edited.translation_status, "stale")
        self.assertIsNone(edited.target_text)

    def test_markdown_link_url_change_requires_resume(self):
        path = self.write_source("# Chapter\n\nRead [the guide](old.html).\n")
        project = create_project(path, self.root / "projects")
        provider = IdentityProvider()
        run_project(project, provider=provider)
        old_doc = Document.from_dict(read_json(project / "work" / "document.json"))
        old_unit = next(unit for unit in old_doc.units if "the guide" in unit.source_text)
        old_target = old_unit.target_text

        source = project / "source" / "book.md"
        source.write_text(source.read_text(encoding="utf-8").replace("old.html", "new.html"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            assert_project_source_current(project)
        run_project(project, provider=provider)
        new_doc = Document.from_dict(read_json(project / "work" / "document.json"))
        new_unit = next(unit for unit in new_doc.units if "the guide" in unit.source_text)

        self.assertEqual(new_unit.id, old_unit.id)
        self.assertEqual(new_unit.target_text, old_target)
        self.assertNotEqual(new_unit.structure_hash, old_unit.structure_hash)
        self.assertEqual(len(provider.translation_calls), 1)

    def test_markdown_image_path_change_requires_resume(self):
        (self.root / "assets").mkdir()
        (self.root / "assets" / "old.svg").write_text("<svg/>", encoding="utf-8")
        (self.root / "assets" / "new.svg").write_text("<svg/>", encoding="utf-8")
        path = self.write_source("# Chapter\n\n![A figure](assets/old.svg).\n")
        project = create_project(path, self.root / "projects")
        provider = IdentityProvider()
        run_project(project, provider=provider)
        old_doc = Document.from_dict(read_json(project / "work" / "document.json"))
        old_unit = next(unit for unit in old_doc.units if unit.type == "paragraph")
        old_fingerprint = old_doc.structure_fingerprint

        (project / "source" / "assets" / "new.svg").write_text("<svg/>", encoding="utf-8")
        source = project / "source" / "book.md"
        source.write_text(source.read_text(encoding="utf-8").replace("old.svg", "new.svg"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            assert_project_source_current(project)
        run_project(project, provider=provider)
        new_doc = Document.from_dict(read_json(project / "work" / "document.json"))
        new_unit = next(unit for unit in new_doc.units if unit.type == "paragraph")

        self.assertEqual(new_unit.id, old_unit.id)
        self.assertEqual(new_unit.target_text, old_unit.target_text)
        self.assertNotEqual(new_doc.structure_fingerprint, old_fingerprint)
        self.assertEqual(len(provider.translation_calls), 1)

    def test_source_file_and_structure_fingerprints_are_separate(self):
        path = self.write_source("# Chapter\n\nRead [the guide](old.html).\n")
        before = MarkdownAdapter().import_file(path, "en", "zh-Hans")
        old_text_hashes = [unit.source_hash for unit in before.units]
        old_structure = before.structure_fingerprint
        path.write_text(path.read_text(encoding="utf-8").replace("old.html", "new.html"), encoding="utf-8")
        after = MarkdownAdapter().import_file(path, "en", "zh-Hans")

        self.assertNotEqual(before.source_file_sha256, after.source_file_sha256)
        self.assertNotEqual(after.structure_fingerprint, old_structure)
        self.assertEqual(old_text_hashes, [unit.source_hash for unit in after.units])


if __name__ == "__main__":
    unittest.main()
