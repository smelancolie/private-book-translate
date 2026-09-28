from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import time
import unittest

from private_translate.core.batching import estimate_tokens, provider_budgets
from private_translate.core.glossary import FIELDS
from private_translate.core.language import load_language_package
from private_translate.core.model import Document
from private_translate.core.pipeline import build_project, run_project
from private_translate.core.project import create_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json, write_json


class FakeProvider:
    capabilities = ProviderCapabilities("fake", "large-book-test", 32768, 8192, True, False)

    def __init__(self):
        self.translation_requests = []
        self.review_requests = []

    def translate(self, units, context, glossary, config):
        payload = json.dumps({
            "context": context,
            "glossary": glossary,
            "units": [{"id": unit.id, "text": unit.source_text} for unit in units],
            "required_output": {"translations": [{"id": "unit id", "target_text": "translation"}]},
        }, ensure_ascii=False)
        self.translation_requests.append({
            "units": len(units), "source_chars": sum(len(unit.source_text) for unit in units),
            "unit_ids": [unit.id for unit in units],
            "request_chars": len(payload),
            "tokens": estimate_tokens(config["translation_prompt"]) + estimate_tokens(payload) + 16,
        })
        return {unit.id: unit.source_text.replace("North Star", "北极星") for unit in units}

    def review(self, source, target, context, rubric, config):
        payload = json.dumps({
            "source": source, "target": target, "context": context,
            "required_output": {"issues": [{"unit_id": "unit id", "severity": "P1", "description": "issue"}]},
        }, ensure_ascii=False)
        self.review_requests.append({
            "units": source.count("["), "source_chars": len(source),
            "request_chars": len(payload), "tokens": estimate_tokens(rubric) + estimate_tokens(payload) + 16,
        })
        return []


class SmokeProvider(FakeProvider):
    capabilities = ProviderCapabilities("fake", "long-book-smoke", 1536, 256, True, False)


class LargeBookTests(unittest.TestCase):
    def test_long_book_smoke_covers_resume_qa_build_and_bounded_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "smoke.md"
            chapters, paragraphs = 8, 16
            source.write_text("\n\n".join(
                block
                for chapter in range(1, chapters + 1)
                for block in [
                    f"# Chapter {chapter:03d}",
                    *(f"the passage {chapter:03d}-{paragraph:03d} mentions North Star guiding a traveler."
                      for paragraph in range(1, paragraphs + 1)),
                ]
            ), encoding="utf-8")
            project = create_project(source, root / "projects")
            approved = {
                "source": "North Star", "preferred_translation": "北极星", "category": "proper_name",
                "status": "approved", "first_occurrence": "", "occurrence_count": "",
                "notes": "", "forbidden_translations": "",
            }
            for path in (project / "glossary" / "terms.csv", project / "glossary" / "candidates.csv"):
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerow(approved)

            provider = SmokeProvider()
            run_project(project, provider=provider)
            document_path = project / "work" / "document.json"
            first = Document.from_dict(read_json(document_path))
            first_ids = {unit.source_text: unit.id for unit in first.units}
            self.assertEqual(len(first.units), chapters * (paragraphs + 1))
            self.assertEqual(read_json(project / "qa" / "book-consistency.json")["status"], "PASS")
            self.assertEqual(len(list((project / "qa" / "chapter-controls").glob("*.json"))), chapters)
            self.assertGreater(len(provider.translation_requests), chapters)
            self.assertGreater(len(provider.review_requests), chapters)

            source_in_project = project / "source" / source.name
            source_text = source_in_project.read_text(encoding="utf-8")
            source_in_project.write_text(source_text.replace(
                "# Chapter 001\n\n", "# Chapter 001\n\nA new North Star passage was added.\n\n", 1,
            ), encoding="utf-8")
            before_resume = len(provider.translation_requests)
            run_project(project, provider=provider)
            document = Document.from_dict(read_json(document_path))
            self.assertEqual(len(document.units), len(first.units) + 1)
            self.assertTrue(all(unit.id == first_ids[unit.source_text] for unit in document.units if unit.source_text in first_ids))
            inserted = next(unit for unit in document.units if unit.source_text == "A new North Star passage was added.")
            resumed_translations = provider.translation_requests[before_resume:]
            self.assertEqual(len(resumed_translations), 1)
            self.assertEqual(resumed_translations[0]["unit_ids"], [inserted.id])

            occurrences = read_json(project / "qa" / "consistency" / "occurrences.json")["terms"]
            north_star = next(term for term in occurrences if term["term"] == "North Star")
            self.assertEqual(len(north_star["unit_ids"]), chapters * paragraphs + 1)
            language = load_language_package("en", "zh-Hans")
            budgets = provider_budgets(provider, language)
            total_units = len(document.units)
            total_source_chars = len(source_in_project.read_text(encoding="utf-8"))
            self.assertTrue(all(request["tokens"] <= budgets["translation_batch_target_tokens"] for request in provider.translation_requests))
            self.assertTrue(all(request["tokens"] <= budgets["review_batch_target_tokens"] for request in provider.review_requests))
            self.assertLess(max(request["units"] for request in provider.translation_requests), total_units)
            self.assertLess(max(request["units"] for request in provider.review_requests), total_units)
            self.assertLess(max(request["source_chars"] for request in provider.translation_requests), total_source_chars)
            self.assertLess(max(request["source_chars"] for request in provider.review_requests), total_source_chars)

            for round_number, seed in enumerate((1001, 2002), 1):
                directory = create_spotcheck_round(project, document, seed)
                sample = read_json(directory / "sample.json")
                write_json(directory / "reviews.json", {
                    "run_id": sample["run_id"], "round": round_number, "seed": seed,
                    "reviewers": [
                        {"slot": slot, "reviewer_id": f"synthetic-{reviewer_number}",
                         "scores": {unit_id: 96 for unit_id in sample["reviewer_unit_ids"][slot]},
                         "style_debt_count": 0, "findings": []}
                        for reviewer_number, slot in enumerate(("reviewer_1", "reviewer_2"), 1)
                    ],
                })
            self.assertTrue(all(path.is_file() for path in build_project(project)))
            print(
                "long-book smoke: "
                f"units={len(document.units)}, translation_calls={len(provider.translation_requests)}, "
                f"review_calls={len(provider.review_requests)}, resumed_units=1"
            )

    def test_large_book_never_sends_full_book_to_provider(self):
        repo = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory(dir=repo / "tests") as temporary:
            root = Path(temporary)
            source = root / "large.md"
            source.write_text("\n\n".join(
                block
                for chapter in range(1, 101)
                for block in [
                    f"# Chapter {chapter:03d}",
                    *(f"the passage {chapter:03d}-{paragraph:03d} mentions North Star guiding a traveler."
                      for paragraph in range(1, 501)),
                ]
            ), encoding="utf-8")

            init_started = time.perf_counter()
            project = create_project(source, root / "projects")
            init_seconds = time.perf_counter() - init_started
            approved = {
                "source": "North Star", "preferred_translation": "北极星", "category": "proper_name",
                "status": "approved", "first_occurrence": "", "occurrence_count": "",
                "notes": "", "forbidden_translations": "",
            }
            for path in (project / "glossary" / "terms.csv", project / "glossary" / "candidates.csv"):
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerow(approved)

            provider = FakeProvider()
            first_run_started = time.perf_counter()
            output = run_project(project, provider=provider)
            first_run_seconds = time.perf_counter() - first_run_started
            document_path = project / "work" / "document.json"
            first_document = Document.from_dict(read_json(document_path))
            first_ids = [unit.id for unit in first_document.units]
            full_unit_count = len(first_document.units)
            source_chars = len(source.read_text(encoding="utf-8"))
            calls_before_resume = (len(provider.translation_requests), len(provider.review_requests))

            resume_started = time.perf_counter()
            run_project(project, provider=provider)
            resume_seconds = time.perf_counter() - resume_started
            self.assertEqual(
                (len(provider.translation_requests), len(provider.review_requests)),
                calls_before_resume,
            )
            document = Document.from_dict(read_json(document_path))
            self.assertEqual([unit.id for unit in document.units], first_ids)
            self.assertEqual(len(first_ids), 50100)
            self.assertTrue(output[0].is_file())
            self.assertGreater(document_path.stat().st_size, 0)

            language = load_language_package("en", "zh-Hans")
            budgets = provider_budgets(provider, language)
            self.assertTrue(provider.translation_requests)
            self.assertTrue(provider.review_requests)
            self.assertTrue(all(request["tokens"] <= budgets["translation_batch_target_tokens"] for request in provider.translation_requests))
            self.assertTrue(all(request["tokens"] <= budgets["review_batch_target_tokens"] for request in provider.review_requests))
            self.assertLess(max(request["units"] for request in provider.translation_requests), full_unit_count)
            self.assertLess(max(request["units"] for request in provider.review_requests), full_unit_count)
            self.assertLess(max(request["source_chars"] for request in provider.translation_requests), source_chars)
            self.assertLess(max(request["source_chars"] for request in provider.review_requests), source_chars)

            occurrences = read_json(project / "qa" / "consistency" / "occurrences.json")["terms"]
            north_star = next(term for term in occurrences if term["term"] == "North Star")
            self.assertEqual(sum(item["count"] for item in north_star["source_occurrences"]), 50000)
            self.assertEqual(len(north_star["unit_ids"]), 50000)

            for round_number, seed in enumerate((1001, 2002), 1):
                directory = create_spotcheck_round(project, document, seed)
                sample = read_json(directory / "sample.json")
                write_json(directory / "reviews.json", {
                    "run_id": sample["run_id"], "round": round_number, "seed": seed,
                    "reviewers": [
                        {"slot": slot, "reviewer_id": f"synthetic-{reviewer_number}",
                         "scores": {unit_id: 96 for unit_id in sample["reviewer_unit_ids"][slot]},
                         "style_debt_count": 0, "findings": []}
                        for reviewer_number, slot in enumerate(("reviewer_1", "reviewer_2"), 1)
                    ],
                })
            self.assertTrue(all(path.is_file() for path in build_project(project)))
            print(
                "large-book metrics: "
                f"units={full_unit_count}, init={init_seconds:.2f}s, run={first_run_seconds:.2f}s, "
                f"resume={resume_seconds:.2f}s, state={document_path.stat().st_size} bytes, "
                f"translate_calls={len(provider.translation_requests)}, "
                f"review_calls={len(provider.review_requests)}"
            )


if __name__ == "__main__":
    unittest.main()
