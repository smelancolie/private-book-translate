from pathlib import Path
import shutil
import subprocess
import unittest

from private_translate.core.model import TranslationUnit, content_hash
from private_translate.core.project import create_project
from private_translate.core.state import merge_saved_state
from private_translate.core.glossary import load_glossary
from private_translate.core.language import load_language_package


class PhaseOneTests(unittest.TestCase):
    def unit(self, text="Hello"):
        return TranslationUnit.create(
            section_id="chapter-1", type="paragraph", source_text=text,
            translatable=True, source_language="en", target_language="zh-Hans",
            locator={"path": [0]},
        )

    def test_stable_id_hash_and_target_edit_invalidation(self):
        first = self.unit()
        again = self.unit()
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.source_hash, content_hash("Hello"))
        first.target_text = "你好"
        first.translation_status = "translated"
        first.qa_status = "passed"
        first.last_qa_target_hash = content_hash("different saved result")
        fresh = merge_saved_state(type("D", (), {"units": [again]})(), type("D", (), {"units": [first]})())
        self.assertEqual(fresh.units[0].qa_status, "pending")

    def test_source_change_is_stale(self):
        old = self.unit("old")
        old.target_text = "旧"
        old.translation_status = "translated"
        fresh = self.unit("new")
        merged = merge_saved_state(type("D", (), {"units": [fresh]})(), type("D", (), {"units": [old]})())
        self.assertEqual(merged.units[0].translation_status, "stale")
        self.assertIsNone(merged.units[0].target_text)

    def test_private_data_paths_are_gitignored(self):
        repo = Path(__file__).parents[1]
        for path in ("projects/example/source/book.md", ".env"):
            result = subprocess.run(["git", "check-ignore", "--quiet", "--", path], cwd=repo)
            self.assertEqual(result.returncode, 0, path)

    def test_private_workspace_and_supported_input(self):
        temp_root = Path(__file__).parents[1] / ".test-tmp" / "phase1"
        temp_root.mkdir(parents=True, exist_ok=True)
        source = temp_root / "book.md"
        source.write_text("# A book", encoding="utf-8")
        projects_dir = temp_root / "projects"
        project = create_project(source, projects_dir)
        self.assertEqual((project / "source" / "book.md").read_text(encoding="utf-8"), "# A book")
        self.assertTrue((project / "glossary" / "terms.csv").exists())
        bad = temp_root / "book.pdf"
        bad.write_bytes(b"%PDF")
        with self.assertRaises(ValueError):
            create_project(bad, projects_dir)
        self.assertTrue(project.resolve().is_relative_to(projects_dir.resolve()))
        shutil.rmtree(project)

    def test_language_package_and_approved_glossary(self):
        package = load_language_package("en", "zh-Hans")
        self.assertEqual((package["source_language"], package["target_language"]), ("en", "zh-Hans"))
        root = Path(__file__).parents[1] / ".test-tmp" / "glossary"
        root.mkdir(parents=True, exist_ok=True)
        glossary = root / "terms.csv"
        glossary.write_text(
            "source,preferred_translation,category,approved,forbidden_translations\n"
            "North Star,北极星,names,yes,北斗\n"
            "Draft term,草稿,technical_terms,no,\n"
            "Incomplete,,names,yes,\n",
            encoding="utf-8",
        )
        self.assertEqual(load_glossary(glossary), [{
            "source": "North Star", "preferred_translation": "北极星", "category": "names",
            "approved": "yes", "forbidden_translations": "北斗",
        }])
        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
