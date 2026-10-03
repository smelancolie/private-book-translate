import json
from pathlib import Path
import tempfile
import unittest

from private_translate.core.batching import provider_budgets, split_oversized_units, translation_batches
from private_translate.core.language import load_language_package
from private_translate.core.model import Document, Section, TranslationUnit
from private_translate.core.pipeline import run_project
from private_translate.core.project import create_project
from private_translate.core.providers import OpenAICompatibleProvider, ProviderCapabilities
from private_translate.core.qa import run_book_consistency


class RecordingProvider:
    def __init__(self, fail_on_call=None, context_window=1200):
        self.capabilities = ProviderCapabilities("fake", "small-context", context_window, 100, True, False)
        self.translation_calls = []
        self.review_calls = []
        self.fail_on_call = fail_on_call

    def translate(self, units, context, glossary, config):
        self.translation_calls.append([unit.id for unit in units])
        if self.fail_on_call == len(self.translation_calls):
            raise RuntimeError("temporary provider failure")
        return {unit.id: f"译：{unit.source_text}" for unit in units}

    def review(self, source, target, context, rubric, config):
        self.review_calls.append((source, target, context))
        return []


class BatchingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.temp = tempfile.TemporaryDirectory(dir=self.repo / "tests")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def unit(self, text, locator):
        return TranslationUnit.create(
            section_id="section-0001", type="paragraph", source_text=text,
            translatable=True, source_language="en", target_language="zh-Hans", locator={"path": locator},
        )

    def test_provider_capabilities_bound_configured_budgets(self):
        provider = RecordingProvider()
        language = load_language_package("en", "zh-Hans")

        budgets = provider_budgets(provider, language)

        self.assertEqual(budgets["max_output_tokens"], 100)
        self.assertEqual(budgets["max_input_tokens"], 1100)
        self.assertEqual(budgets["translation_batch_target_tokens"], 1100)
        self.assertEqual(budgets["review_batch_target_tokens"], 1100)

    def test_openai_compatible_provider_reports_configurable_capabilities(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {
            "PRIVATE_TRANSLATE_MAX_CONTEXT_TOKENS": "32000",
            "PRIVATE_TRANSLATE_MAX_OUTPUT_TOKENS": "4000",
        }):
            provider = OpenAICompatibleProvider("https://provider.invalid/v1", "test-model", api_key="test")

        self.assertEqual(provider.capabilities.max_context_tokens, 32000)
        self.assertEqual(provider.capabilities.max_output_tokens, 4000)

    def test_long_section_is_split_into_translation_batches(self):
        paragraph_count = 32
        source = self.root / "book.md"
        source.write_text("# Chapter\n\n" + "\n\n".join(f"Paragraph {i} contains several ordinary words." for i in range(paragraph_count)), encoding="utf-8")
        project = create_project(source, self.root / "projects")
        provider = RecordingProvider(context_window=1500)

        run_project(project, provider=provider)

        self.assertGreater(len(provider.translation_calls), 1)
        self.assertEqual(len([unit_id for batch in provider.translation_calls for unit_id in batch]), paragraph_count + 1)
        self.assertTrue(provider.review_calls)
        self.assertLess(max(len(review_source) for review_source, _, _ in provider.review_calls), len(source.read_text(encoding="utf-8")))

    def test_batch_never_splits_an_ordinary_unit(self):
        units = [self.unit(f"Unit {index} has a complete sentence.", [index]) for index in range(4)]
        provider = RecordingProvider()
        language = load_language_package("en", "zh-Hans")
        batches = translation_batches(units, provider, language, "", [])

        self.assertEqual([unit.id for batch, _, _ in batches for unit in batch], [unit.id for unit in units])
        self.assertTrue(all(unit.source_text.endswith("sentence.") for batch, _, _ in batches for unit in batch))

    def test_oversized_unit_splits_only_at_sentence_boundaries(self):
        source = "First sentence has several words. Second sentence has several words. Third sentence has several words."
        unit = self.unit(source, [0])
        document = Document("markdown", "en", "zh-Hans", {}, [Section("section-0001", "Chapter", {})], [unit])

        split_oversized_units(document, max_tokens=12)

        self.assertGreater(len(document.units), 1)
        self.assertEqual("".join(candidate.source_text for candidate in document.units), source)
        self.assertTrue(all(candidate.source_text.rstrip().endswith(".") for candidate in document.units[:-1]))
        self.assertEqual(len({candidate.id for candidate in document.units}), len(document.units))
        self.assertEqual({candidate.source_parent_id for candidate in document.units}, {unit.id})

    def test_batch_resume_skips_already_translated_units(self):
        source = self.root / "book.md"
        source.write_text("# Chapter\n\n" + "\n\n".join(f"Paragraph {i} contains several ordinary words." for i in range(16)), encoding="utf-8")
        project = create_project(source, self.root / "projects")
        provider = RecordingProvider(fail_on_call=2, context_window=1500)

        with self.assertRaisesRegex(RuntimeError, "temporary provider failure"):
            run_project(project, provider=provider)
        saved = Document.from_dict(json.loads((project / "work" / "document.json").read_text(encoding="utf-8")))
        completed = {unit.id for unit in saved.units if unit.translation_status == "translated" and unit.translatable}
        self.assertTrue(completed)
        failed_call_index = len(provider.translation_calls)
        provider.fail_on_call = None

        run_project(project, provider=provider)

        retried_ids = {unit_id for batch in provider.translation_calls[failed_call_index:] for unit_id in batch}
        self.assertFalse(completed & retried_ids)

    def test_book_consistency_reviews_only_bounded_candidate_batches(self):
        provider = RecordingProvider()
        language = load_language_package("en", "zh-Hans")
        units = [self.unit(f"Arcana appears in passage {index}.", [index]) for index in range(32)]
        for unit in units:
            unit.target_text = "秘传术语出现在这里。"
            unit.translation_status = "translated"
        document = Document("markdown", "en", "zh-Hans", {}, [Section("section-0001", "Chapter", {})], units)
        glossary = [{"source": "Arcana", "preferred_translation": "秘传术语", "forbidden_translations": ""}]

        run_book_consistency(self.root / "project", document, provider, language, glossary)

        self.assertGreater(len(provider.review_calls), 1)
        self.assertTrue(all(source.count("[" ) < len(units) for source, _, _ in provider.review_calls))
        occurrences = json.loads((self.root / "project" / "qa" / "consistency" / "occurrences.json").read_text(encoding="utf-8"))
        self.assertEqual(len(occurrences["terms"][0]["unit_ids"]), len(units))


if __name__ == "__main__":
    unittest.main()
