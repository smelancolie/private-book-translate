from pathlib import Path
import csv
import shutil
import subprocess
import unittest
import uuid

from private_translate.core.model import TranslationUnit, content_hash
from private_translate.core.project import create_project
from private_translate.core.state import merge_saved_state
from private_translate.core.glossary import load_glossary
from private_translate.core.language import load_language_package
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.pipeline import run_project


class PhaseOneTests(unittest.TestCase):
    def unit(self, text="Hello"):
        return TranslationUnit.create(
            section_id="chapter-1", type="paragraph", source_text=text,
            translatable=True, source_language="en", target_language="zh-Hans",
            locator={"path": [0]},
        )

    def test_reconciled_id_is_persistent_and_target_edit_invalidates_qa(self):
        first = self.unit()
        again = self.unit()
        self.assertNotEqual(first.id, again.id)
        uuid.UUID(first.id)
        self.assertEqual(first.source_hash, content_hash("Hello"))
        first.target_text = "你好"
        first.translation_status = "translated"
        first.qa_status = "passed"
        first.last_qa_target_hash = content_hash("different saved result")
        fresh = merge_saved_state(type("D", (), {"units": [again]})(), type("D", (), {"units": [first]})())
        self.assertEqual(fresh.units[0].id, first.id)
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
        self.assertTrue((project / "glossary" / "candidates.csv").exists())
        bad = temp_root / "book.pdf"
        bad.write_bytes(b"%PDF")
        with self.assertRaises(ValueError):
            create_project(bad, projects_dir)
        self.assertTrue(project.resolve().is_relative_to(projects_dir.resolve()))
        shutil.rmtree(project)

    def test_custom_projects_dir_workspace_is_self_ignored(self):
        repo = Path(__file__).parents[1]
        source_root = repo / ".test-tmp" / "custom-ignore"
        custom_projects = repo / "tests" / f"custom-projects-{uuid.uuid4().hex[:8]}"
        source_root.mkdir(parents=True, exist_ok=True)
        source = source_root / "private.md"
        source.write_text("# Private\n", encoding="utf-8")
        try:
            project = create_project(source, custom_projects)
            self.assertEqual((project / ".gitignore").read_text(encoding="utf-8"), "*\n!.gitignore\n")
            state_path = project / "work" / "private-state.json"
            state_relative = state_path.relative_to(repo)
            ignore = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", "--", str(state_relative)], cwd=repo)
            self.assertEqual(ignore.returncode, 0)
            ignore_file = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", "--", str((project / ".gitignore").relative_to(repo))], cwd=repo)
            self.assertEqual(ignore_file.returncode, 1)
        finally:
            if custom_projects.exists():
                self.assertTrue(custom_projects.resolve().is_relative_to(repo.resolve()))
                shutil.rmtree(custom_projects)

    def test_language_package_and_approved_glossary(self):
        package = load_language_package("en", "zh-Hans")
        self.assertEqual((package["source_language"], package["target_language"]), ("en", "zh-Hans"))
        self.assertIn("polysemy", package["translation_prompt"].lower())
        self.assertIn("quotation", package["review_rubric"].lower())
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
            "forbidden_translations": "北斗",
        }])
        glossary.write_text(
            "source,preferred_translation,category,status,first_occurrence,occurrence_count,notes,forbidden_translations\n"
            "Approved term,正式术语,specialist_term,approved,chapter/unit-1,3,,\n"
            "Proposed term,待定术语,specialist_term,proposed,chapter/unit-2,2,,\n",
            encoding="utf-8",
        )
        self.assertEqual(load_glossary(glossary)[0]["source"], "Approved term")
        shutil.rmtree(root)

    def test_pretranslation_discovery_proposes_and_approves_terms(self):
        root = Path(__file__).parents[1] / ".test-tmp" / "terminology-discovery"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(Path(__file__).parents[1].resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "book.md"
        source.write_text(
            "# Chapter\n\nNorth Star guides the ship.\n\nFollow the North Star.\n\nThe North Star returns.\n",
            encoding="utf-8",
        )
        project = create_project(source, root / "projects")

        class ProposalProvider:
            capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)

            def __init__(self):
                self.translation_calls = []

            def translate(self, units, context, glossary, config):
                self.translation_calls.append(([unit.type for unit in units], glossary, [unit.source_text for unit in units]))
                result = {}
                for unit in units:
                    translated = "北极星" if unit.type == "terminology_candidate" else f"译：{unit.source_text}"
                    for term in glossary:
                        translated = translated.replace(term["source"], term["preferred_translation"])
                    result[unit.id] = translated
                return result

            def review(self, source, target, context, rubric, config):
                return []

        provider = ProposalProvider()
        run_project(project, provider=provider)

        with (project / "glossary" / "candidates.csv").open(encoding="utf-8", newline="") as stream:
            candidates = list(csv.DictReader(stream))
        candidate = next(row for row in candidates if row["source"] == "North Star")
        self.assertEqual(candidate["preferred_translation"], "北极星")
        self.assertEqual(candidate["status"], "proposed")
        self.assertEqual(candidate["occurrence_count"], "3")
        self.assertEqual(provider.translation_calls[0][0], ["terminology_candidate"])
        with (project / "glossary" / "terms.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

        candidate["status"] = "approved"
        with (project / "glossary" / "candidates.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=candidates[0].keys())
            writer.writeheader()
            writer.writerows(candidates)
        source_copy = project / "source" / "book.md"
        source_copy.write_text(source_copy.read_text(encoding="utf-8") + "\nA new North Star reference.\n", encoding="utf-8")
        run_project(project, provider=provider)

        with (project / "glossary" / "terms.csv").open(encoding="utf-8", newline="") as stream:
            approved = list(csv.DictReader(stream))
        self.assertEqual([(row["source"], row["preferred_translation"], row["status"]) for row in approved], [("North Star", "北极星", "approved")])
        self.assertTrue(any(
            any("North Star" in text for text in call[2]) and any(term["source"] == "North Star" for term in call[1])
            for call in provider.translation_calls[1:]
        ))
        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
