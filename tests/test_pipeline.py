from pathlib import Path
import shutil
import unittest

from private_translate.core.pipeline import build_project, run_project, validate_project
from private_translate.core.project import create_project
from private_translate.core.model import Document, Section, TranslationUnit, content_hash
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.qa import run_chapter_qa
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json, write_json
from private_translate.formats.markdown import MarkdownAdapter


def approve_current_spotcheck(project, seeds):
    for index, seed in enumerate(seeds, 1):
        directory = create_spotcheck_round(project, Document.from_dict(read_json(project / "work" / "document.json")), seed)
        sample = read_json(directory / "sample.json")
        reviewers = [
            {"slot": slot, "reviewer_id": f"reviewer-{reviewer_index}",
             "scores": {unit_id: 96 for unit_id in sample["reviewer_unit_ids"][slot]},
             "style_debt_count": 0, "findings": []}
            for reviewer_index, slot in enumerate(("reviewer_1", "reviewer_2"), 1)
        ]
        write_json(directory / "reviews.json", {"run_id": sample["run_id"], "round": index, "seed": seed, "reviewers": reviewers})


class ExampleProvider:
    capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)
    translations = {
        "A Small Book": "一本小书",
        "The story begins with": "故事从",
        "bold words": "粗体文字",
        "guide": "指南",
        "a guide": "一份指南",
        "A quoted paragraph.": "一段引用。",
        "Cover": "封面",
        "First item": "第一项",
        "Nested item": "嵌套项",
        "Ada": "艾达",
        "A person": "一个人",
        "See the": "参见",
        "Chapter One": "第一章",
        "Preface text before the first heading.": "首个标题前的序言。",
        "Preface with front matter.": "带有前置元数据的序言。",
        "A short footnote.": "简短脚注。",
        "Readable HTML block text.": "可读的 HTML 块文本。",
    }

    def __init__(self):
        self.translation_calls = []
        self.review_calls = []

    def translate(self, units, context, glossary, config):
        self.translation_calls.append([unit.id for unit in units])
        result = {}
        for unit in units:
            text = unit.source_text
            for source, target in self.translations.items():
                text = text.replace(source, target)
            result[unit.id] = text
        return result

    def review(self, source, target, context, rubric, config):
        self.review_calls.append((source, target, rubric))
        return []


class MarkdownPipelineTests(unittest.TestCase):
    def test_markdown_text_before_first_heading_is_translated(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "markdown-preamble"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "preamble.md"
        source.write_text("Preface text before the first heading.\n\n# Chapter One\n\nChapter body.\n", encoding="utf-8")
        project = create_project(source, root / "projects")

        output = run_project(project, provider=ExampleProvider())[0].read_text(encoding="utf-8")

        self.assertIn("首个标题前的序言。", output)
        document = Document.from_dict(read_json(project / "work" / "document.json"))
        preamble = next(section for section in document.sections if section.section_id == "section-0000")
        self.assertEqual(preamble.title, "Preamble")
        self.assertTrue(any(unit.section_id == preamble.section_id and unit.translation_status == "translated" for unit in document.units))
        shutil.rmtree(root)

    def test_markdown_frontmatter_preamble_and_heading_have_sections(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "markdown-frontmatter-preamble"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "preamble.md"
        source.write_text("---\ntitle: A Small Book\n---\n\nPreface with front matter.\n\n# Chapter One\n\nChapter body.\n", encoding="utf-8")
        project = create_project(source, root / "projects")

        output = run_project(project, provider=ExampleProvider())[0].read_text(encoding="utf-8")

        self.assertIn("title: 一本小书", output)
        self.assertIn("带有前置元数据的序言。", output)
        document = Document.from_dict(read_json(project / "work" / "document.json"))
        self.assertEqual([section.section_id for section in document.sections[:3]], ["frontmatter", "section-0000", "section-0001"])
        shutil.rmtree(root)

    def test_bilingual_markdown_keeps_only_source_heading_id(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "markdown-bilingual-heading-id"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "linked-heading.md"
        source.write_text("# Chapter One {#chapter-one}\n\nSee [chapter](#chapter-one).\n", encoding="utf-8")
        project = create_project(source, root / "projects")

        bilingual_path = run_project(project, bilingual=True, provider=ExampleProvider())[1]
        bilingual = bilingual_path.read_text(encoding="utf-8")
        rendered = MarkdownAdapter().import_file(bilingual_path, "en", "zh-Hans")
        header_ids = [block["c"][1][0] for block in rendered.structure["pandoc"]["blocks"] if block.get("t") == "Header"]

        self.assertEqual(header_ids.count("chapter-one"), 1)
        self.assertRegex(bilingual, r"\]\(#chapter-one\)")
        shutil.rmtree(root)

    def test_markdown_import_creates_figure_audit_unit(self):
        from private_translate.core.spotcheck import _eligible_units

        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "markdown-figure-unit"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        (root / "assets").mkdir()
        (root / "assets" / "cover.png").write_bytes(b"private-figure-bytes")
        source = root / "figures.md"
        source.write_text("A cover illustration: ![Blue cover](assets/cover.png)\n", encoding="utf-8")

        document = MarkdownAdapter().import_file(source, "en", "zh-Hans")
        figure = next(unit for unit in document.units if unit.type == "figure")

        self.assertFalse(figure.translatable)
        self.assertEqual(figure.metadata["asset_path"], "assets/cover.png")
        self.assertTrue(figure.metadata["asset_hash"])
        self.assertEqual(figure.metadata["alt"], "Blue cover")
        self.assertTrue(figure.metadata["surrounding_unit_ids"])
        self.assertIn(figure, _eligible_units(document)["figure"])
        shutil.rmtree(root)

    def test_markdown_target_and_bilingual_outputs_and_resume(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "markdown-vertical-slice"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        project = create_project(repo / "tests" / "fixtures" / "markdown" / "structure.md", root / "projects")
        provider = ExampleProvider()

        outputs = run_project(project, bilingual=True, provider=provider)
        self.assertEqual(len(outputs), 2)
        self.assertEqual([path.name for path in outputs], ["structure.zh-Hans.md", "structure.en-zh-Hans.bilingual.md"])
        target = outputs[0].read_text(encoding="utf-8")
        bilingual = outputs[1].read_text(encoding="utf-8")
        self.assertIn("一本小书", target)
        self.assertIn("title: 一本小书", target)
        self.assertIn("private_note: leave this unchanged", target)
        self.assertNotIn("title: A Small Book", target)
        self.assertIn("https://example.test/guide", target)
        self.assertIn("inline_code", target)
        self.assertIn('print("leave this code")', target)
        self.assertIn("unknown extension marker; preserve", target)
        self.assertIn("可读的 HTML 块文本。", target)
        self.assertIn("简短脚注。", target)
        self.assertIn("assets/cover.svg", target)
        self.assertIn('"封面"', target)
        self.assertTrue((project / "source" / "assets" / "cover.svg").is_file())
        self.assertTrue((project / "output" / "assets" / "cover.svg").is_file())
        self.assertIn("A Small Book", bilingual)
        self.assertIn("一本小书", bilingual)
        self.assertIn("https://example.test/guide", bilingual)
        self.assertIn("[^1]", target)
        approve_current_spotcheck(project, [11, 22])
        self.assertEqual(validate_project(project)["bilingual"], str(outputs[1]))
        self.assertEqual(build_project(project), outputs)
        alignment = read_json(project / "qa" / "alignment.json")
        self.assertTrue(alignment["alignments"])
        self.assertTrue(all(item["unit_id"] and item["source_hash"] and item["target_hash"] for item in alignment["alignments"]))

        calls = len(provider.translation_calls)
        self.assertEqual(run_project(project, provider=provider), outputs)
        self.assertEqual(len(provider.translation_calls), calls)
        shutil.rmtree(root)

    def test_default_markdown_output_is_target_only(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "default-target-only"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "small.md"
        source.write_text("# One chapter\n\nA short paragraph.\n", encoding="utf-8")
        project = create_project(source, root / "projects")
        outputs = run_project(project, provider=ExampleProvider())
        self.assertEqual([path.name for path in outputs], ["small.zh-Hans.md"])
        with self.assertRaisesRegex(ValueError, "No stratified spot-check"):
            build_project(project)
        shutil.rmtree(root)

    def test_resume_after_provider_failure_keeps_completed_section(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "resume-after-provider-failure"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source = root / "chapters.md"
        source.write_text("# First\n\nFirst chapter text.\n\n# Second\n\nSecond chapter text.\n", encoding="utf-8")
        project = create_project(source, root / "projects")

        class FailsOnce:
            calls = 0
            capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)

            def translate(self, units, context, glossary, config):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("temporary provider failure")
                return {unit.id: f"译文{unit.source_text}" for unit in units}

            def review(self, source, target, context, rubric, config):
                return []

        failing = FailsOnce()
        with self.assertRaisesRegex(RuntimeError, "temporary provider failure"):
            run_project(project, provider=failing)
        saved = read_json(project / "work" / "document.json")
        first_section = saved["sections"][0]["section_id"]
        completed_ids = {unit["id"] for unit in saved["units"] if unit["section_id"] == first_section and unit["translatable"]}

        class Resuming(FailsOnce):
            def __init__(self):
                self.calls = []

            def translate(self, units, context, glossary, config):
                self.calls.append({unit.id for unit in units})
                return {unit.id: f"译文{unit.source_text}" for unit in units}

        resumed = Resuming()
        run_project(project, provider=resumed)
        self.assertTrue(completed_ids)
        self.assertTrue(all(not (completed_ids & call) for call in resumed.calls))
        shutil.rmtree(root)

    def test_a_fixed_chapter_requires_a_new_full_recheck(self):
        repo = Path(__file__).parents[1]
        root = repo / ".test-tmp" / "chapter-gate"
        if root.exists():
            self.assertTrue(root.resolve().is_relative_to(repo.resolve()))
            shutil.rmtree(root)
        (root / "qa").mkdir(parents=True)
        (root / "work").mkdir()
        unit = TranslationUnit.create(
            section_id="section-0001", type="paragraph", source_text="Source sentence.",
            translatable=True, source_language="en", target_language="zh-Hans", locator={"path": [0]},
        )
        unit.target_text = "初稿。"
        unit.translation_status = "translated"
        document = Document("markdown", "en", "zh-Hans", {}, [Section("section-0001", "Chapter", {"path": [0]})], [unit])

        class FixingProvider:
            capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)
            reviews = 0

            def review(self, source, target, context, rubric, config):
                self.reviews += 1
                if self.reviews == 1:
                    return [{"unit_id": unit.id, "severity": "P1", "description": "Improve the sentence."}]
                return []

            def translate(self, units, context, glossary, config):
                return {candidate.id: "修订稿。" for candidate in units}

        language = {
            "review_rubric": "Review the full chapter.",
            "translation_prompt": "Translate while preserving markers.",
            "provider_limits": {
                "max_context_tokens": 116000, "max_input_tokens": 100000, "max_output_tokens": 16000,
                "translation_batch_target_tokens": 12000, "review_batch_target_tokens": 16000,
            },
        }
        run_chapter_qa(root, document, FixingProvider(), language, [])
        rounds = read_json(root / "qa" / "chapter-controls" / "section-0001.json")["rounds"]
        self.assertEqual([item["latest_round_status"] for item in rounds], ["FIXED_RECHECK_REQUIRED", "PASS"])
        self.assertEqual(rounds[1]["issues_found"], 0)
        self.assertEqual(unit.qa_status, "passed")
        self.assertEqual(unit.last_qa_target_hash, content_hash("修订稿。"))
        unsupported_evidence = {
            "expert_translation_skill_used", "expert_level_review_status",
            "polysemy_translation_stage_review", "polysemy_context_review",
        }
        self.assertFalse(unsupported_evidence & set(rounds[1]))
        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
