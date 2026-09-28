from pathlib import Path
import json
import shutil
import unittest

from private_translate.core.model import Document, Section, TranslationUnit, content_hash
from private_translate.core.spotcheck import create_spotcheck_round, validate_spotcheck
from private_translate.core.state import read_json, write_json


class SpotcheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.root = self.repo / ".test-tmp" / self._testMethodName
        if self.root.exists():
            self.assertTrue(self.root.resolve().is_relative_to(self.repo.resolve()))
            shutil.rmtree(self.root)
        (self.root / "qa").mkdir(parents=True)
        (self.root / "work").mkdir()
        self.units = []
        for index, (kind, source, target, translatable) in enumerate([
            ("paragraph", "Source one.", "译文一。", True),
            ("paragraph", "Source two.", "译文二。", True),
            ("table", "Table source.", "表格译文。", True),
            ("caption_note", "Caption source.", "说明译文。", True),
            ("figure", "A cover.", "A cover.", False),
            ("formula", "x + y", "x + y", False),
        ]):
            metadata = {}
            if kind == "figure":
                metadata = {
                    "asset_path": "images/cover.png", "asset_hash": "asset-sha256",
                    "alt": "A cover", "caption_unit_ids": [],
                    "surrounding_unit_ids": [self.units[0].id], "surrounding_text": [self.units[0].source_text],
                }
            unit = TranslationUnit.create(section_id="chapter-1", type=kind, source_text=source,
                translatable=translatable, source_language="en", target_language="zh-Hans", locator={"slot": index}, metadata=metadata)
            unit.target_text = target
            unit.translation_status = "translated"
            unit.qa_status = "passed"
            unit.last_qa_target_hash = content_hash(target)
            self.units.append(unit)
        self.document = Document("markdown", "en", "zh-Hans", {},
            [Section("chapter-1", "Chapter", {})], self.units)
        self.save_qa_state()
        write_json(self.root / "work" / "document.json", self.document.to_dict())

    def save_qa_state(self):
        for unit in self.units:
            if unit.translatable:
                unit.last_qa_target_hash = content_hash(unit.target_text or "")
                unit.qa_status = "passed"
        translated = [unit for unit in self.units if unit.translatable and unit.target_text is not None]
        target_hash = content_hash(json.dumps([(unit.id, unit.target_text) for unit in translated], ensure_ascii=False, separators=(",", ":")))
        write_json(self.root / "qa" / "book-consistency.json", {"status": "PASS", "target_hash": target_hash})

    def tearDown(self):
        if self.root.exists():
            self.assertTrue(self.root.resolve().is_relative_to(self.repo.resolve()))
            shutil.rmtree(self.root)

    def approve_round(self, directory: Path, finding: dict | None = None):
        sample = read_json(directory / "sample.json")
        reviewers = []
        for index, slot in enumerate(("reviewer_1", "reviewer_2"), 1):
            findings = [finding] if finding and finding["unit_id"] in sample["reviewer_unit_ids"][slot] else []
            reviewers.append({
                "slot": slot, "reviewer_id": f"independent-{index}",
                "scores": {unit_id: 96 for unit_id in sample["reviewer_unit_ids"][slot]},
                "style_debt_count": 0, "findings": findings,
            })
        write_json(directory / "reviews.json", {
            "run_id": sample["run_id"], "round": sample["round"], "seed": sample["seed"], "reviewers": reviewers,
        })

    def test_stratified_sample_and_two_current_run_passes(self):
        first = create_spotcheck_round(self.root, self.document, 101)
        sample = read_json(first / "sample.json")
        self.assertEqual(set(sample["strata"]), {"paragraph", "table", "figure", "formula", "caption_note"})
        self.assertEqual(sample["strata"]["formula"]["samples_by_reviewer"]["reviewer_1"], [self.units[-1].id])
        figure_sample = next(item for item in sample["samples"] if item["type"] == "figure")
        self.assertEqual(figure_sample["asset_path"], "images/cover.png")
        self.assertEqual(figure_sample["asset_hash"], "asset-sha256")
        self.assertEqual(figure_sample["alt"], "A cover")
        self.assertEqual(figure_sample["surrounding_text"], ["Source one."])
        with self.assertRaisesRegex(ValueError, "review evidence is incomplete"):
            validate_spotcheck(self.root, self.document)
        self.approve_round(first)
        second = create_spotcheck_round(self.root, self.document, 202)
        self.approve_round(second)
        result = validate_spotcheck(self.root, self.document)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([round_["status"] for round_ in result["rounds"]], ["PASS", "PASS"])
        self.units[0].target_text = "手工修改后的译文。"
        with self.assertRaisesRegex(ValueError, "changed after sampling"):
            validate_spotcheck(self.root, self.document)

    def test_sample_fix_alone_does_not_close_a_defect_family(self):
        first = create_spotcheck_round(self.root, self.document, 303)
        issue = {"unit_id": self.units[0].id, "family_id": "punctuation-consistency", "description": "Wrong punctuation."}
        self.approve_round(first, issue)
        active = read_json(self.root / "qa" / "spotcheck" / "active.json")
        sample = read_json(first / "sample.json")
        unit = self.units[0]
        unit.target_text = "修订后的译文。"
        self.save_qa_state()
        write_json(self.root / "work" / "document.json", self.document.to_dict())
        audit_ids = [candidate.id for candidate in self.units if candidate.type == "paragraph"]
        write_json(first / "closure.json", {
            "run_id": active["run_id"], "round": 1,
            "families": [{
                "family_id": "punctuation-consistency", "scope_strata": ["paragraph"],
                "description": "Inconsistent full-width sentence punctuation.", "audit_method": "Compared each paragraph target to the punctuation style guide.",
                "reported_issue_unit_ids": [unit.id], "audit_results": {unit.id: "fixed"},
                "fixes": {unit.id: {
                    "baseline_target_hash": next(item["target_hash"] for item in sample["samples"] if item["unit_id"] == unit.id),
                    "target_hash": content_hash(unit.target_text), "resolution": "Applied the corrected Chinese punctuation.",
                }}, "exceptions": {}, "closure_status": "CLOSED",
            }],
        })
        self.assertEqual(len(audit_ids), 2)
        with self.assertRaisesRegex(ValueError, "cover every matching unit"):
            validate_spotcheck(self.root, self.document)
        with self.assertRaisesRegex(ValueError, "cover every matching unit"):
            create_spotcheck_round(self.root, self.document, 404)
        other_unit = self.units[1].id
        closure = read_json(first / "closure.json")
        closure["families"][0]["audit_results"][other_unit] = "clear"
        write_json(first / "closure.json", closure)
        second = create_spotcheck_round(self.root, self.document, 404)
        self.approve_round(second)
        third = create_spotcheck_round(self.root, self.document, 505)
        self.approve_round(third)
        result = validate_spotcheck(self.root, self.document)
        self.assertEqual([round_["status"] for round_ in result["rounds"]], ["DEFECT_FOUND", "PASS", "PASS"])


if __name__ == "__main__":
    unittest.main()
