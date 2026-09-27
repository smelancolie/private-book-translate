from pathlib import Path
import shutil
import zipfile
import unittest

from lxml import etree

from private_translate.core.pipeline import run_project, validate_project
from private_translate.core.project import create_project
from private_translate.core.model import Document
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json, write_json
from private_translate.formats.epub import EpubAdapter


def make_epub(path: Path, encrypted: bool = False, obfuscated_font: bool = False, epub2: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "META-INF/container.xml": b'''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''',
        "EPUB/package.opf": b'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="bookid"><metadata><dc:identifier id="bookid">urn:test:book</dc:identifier><dc:title>EPUB Sample</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ch1" href="content/ch1.xhtml" media-type="application/xhtml+xml"/><item id="ch2" href="content/ch2.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="toc.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="styles/book.css" media-type="text/css"/><item id="pixel" href="images/pixel.png" media-type="image/png"/><item id="svg" href="images/mark.svg" media-type="image/svg+xml"/><item id="font" href="fonts/book.ttf" media-type="font/ttf"/></manifest><spine><itemref idref="ch1"/><itemref idref="ch2"/></spine></package>''',
        "EPUB/content/ch1.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>Chapter One</title><link rel="stylesheet" href="../styles/book.css"/></head><body><h1 id="start">Chapter One</h1><p>Hello <em class="voice">brave</em> world.</p><p>See <a href="#note1">a note</a> and <a href="ch2.xhtml#second">Chapter Two</a>.</p><ol><li id="list-item">First note.<a href="#note1">note</a></li></ol><table><tr><th>Column</th><td>One cell.</td></tr></table><math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mi>y</mi></math><figure><img src="../images/pixel.png" alt="A blue square" title="Blue square"/><figcaption>A sample figure.</figcaption></figure><aside id="note1" epub:type="footnote">Footnote text.</aside></body></html>''',
        "EPUB/content/ch2.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Chapter Two</title></head><body><h1 id="second">Chapter Two</h1><p>Welcome to the second chapter.</p></body></html>''',
        "EPUB/toc.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Contents</title></head><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Contents</h1><ol><li><a href="content/ch1.xhtml#start">Chapter One</a><ol><li><a href="content/ch2.xhtml#second">Second chapter</a></li></ol></li></ol></nav></body></html>''',
        "EPUB/styles/book.css": b"body { font-family: serif; } .voice { font-style: italic; }",
        "EPUB/images/pixel.png": b"synthetic-pixel-image",
        "EPUB/images/mark.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2"><title>Blue mark</title><circle cx="1" cy="1" r="1"/></svg>',
        "EPUB/fonts/book.ttf": b"synthetic-font-bytes",
    }
    if encrypted:
        files["META-INF/encryption.xml"] = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/><enc:CipherData><enc:CipherReference URI="EPUB/images/pixel.png"/></enc:CipherData></enc:EncryptedData></encryption>'''
    if obfuscated_font:
        files["META-INF/encryption.xml"] = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/><enc:CipherData><enc:CipherReference URI="EPUB/fonts/book.ttf"/></enc:CipherData></enc:EncryptedData></encryption>'''
    if epub2:
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b'<item id="nav" href="toc.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        ).replace(b"<spine>", b'<spine toc="ncx">')
        files["EPUB/toc.ncx"] = b'''<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>EPUB Sample</text></docTitle><navMap><navPoint id="chapter-one" playOrder="1"><navLabel><text>Chapter One</text></navLabel><content src="content/ch1.xhtml#start"/><navPoint id="chapter-two" playOrder="2"><navLabel><text>Second chapter</text></navLabel><content src="content/ch2.xhtml#second"/></navPoint></navPoint></navMap></ncx>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            archive.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)


class EpubProvider:
    replacements = {
        "EPUB Sample": "电子书样本", "Chapter One": "第一章", "Chapter Two": "第二章",
        "Second chapter": "第二章", "Contents": "目录", "Hello ": "你好，", "brave": "勇敢的",
        " world.": "世界。", "See ": "参见", "a note": "一条注释", " and ": "和",
        "First note.": "第一条注释。", "note": "注释", "Column": "列", "One cell.": "单元格。",
        "A blue square": "蓝色方块", "Blue square": "蓝色方块", "A sample figure.": "示例图片说明。",
        "Footnote text.": "脚注内容。", "Welcome to the second chapter.": "欢迎来到第二章。",
        "Chapter Two.": "第二章。", "A sample": "示例", ".": "。",
    }

    def __init__(self):
        self.translation_calls = []

    def translate(self, units, context, glossary, config):
        self.translation_calls.append([unit.id for unit in units])
        result = {}
        for unit in units:
            translated = unit.source_text
            for source, target in sorted(self.replacements.items(), key=lambda item: len(item[0]), reverse=True):
                translated = translated.replace(source, target)
            result[unit.id] = translated
        return result

    def review(self, source, target, context, rubric, config):
        return []


def approve_current_spotcheck(project, seeds):
    document = Document.from_dict(read_json(project / "work" / "document.json"))
    for index, seed in enumerate(seeds, 1):
        directory = create_spotcheck_round(project, document, seed)
        sample = read_json(directory / "sample.json")
        reviewers = [
            {"slot": slot, "reviewer_id": f"reviewer-{reviewer_index}",
             "scores": {unit_id: 96 for unit_id in sample["reviewer_unit_ids"][slot]},
             "style_debt_count": 0, "findings": []}
            for reviewer_index, slot in enumerate(("reviewer_1", "reviewer_2"), 1)
        ]
        write_json(directory / "reviews.json", {"run_id": sample["run_id"], "round": index, "seed": seed, "reviewers": reviewers})


class EpubPipelineTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).parents[1]
        self.root = self.repo / ".test-tmp" / self._testMethodName
        if self.root.exists():
            self.assertTrue(self.root.resolve().is_relative_to(self.repo.resolve()))
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self.root.exists():
            self.assertTrue(self.root.resolve().is_relative_to(self.repo.resolve()))
            shutil.rmtree(self.root)

    def test_epub_target_bilingual_resume_and_preservation(self):
        source = self.root / "input.epub"
        make_epub(source)
        imported = EpubAdapter().import_file(source, "en", "zh-Hans")
        self.assertTrue(any(unit.type == "formula" and not unit.translatable for unit in imported.units))
        project = create_project(source, self.root / "projects")
        provider = EpubProvider()

        outputs = run_project(project, bilingual=True, provider=provider)
        self.assertEqual([path.name for path in outputs], ["input.zh-Hans.epub", "input.en-zh-Hans.bilingual.epub"])
        target, bilingual = outputs
        adapter = EpubAdapter()
        adapter.validate(target)
        adapter.validate(bilingual)
        approve_current_spotcheck(project, [601, 602])
        self.assertEqual(validate_project(project)["target_only"], str(target))

        with zipfile.ZipFile(source) as original, zipfile.ZipFile(target) as translated, zipfile.ZipFile(bilingual) as parallel:
            self.assertEqual(translated.namelist(), original.namelist())
            self.assertEqual(parallel.namelist(), original.namelist())
            for resource in ("EPUB/styles/book.css", "EPUB/images/pixel.png", "EPUB/images/mark.svg", "EPUB/fonts/book.ttf"):
                self.assertEqual(translated.read(resource), original.read(resource))
                self.assertEqual(parallel.read(resource), original.read(resource))
            self.assertEqual(translated.infolist()[0].filename, "mimetype")
            self.assertEqual(translated.infolist()[0].compress_type, zipfile.ZIP_STORED)
            opf = etree.fromstring(translated.read("EPUB/package.opf"))
            self.assertEqual(opf.xpath("string(.//*[local-name()='title'])"), "电子书样本")
            self.assertEqual(opf.xpath("string(.//*[local-name()='language'])"), "zh-Hans")
            spine = opf.xpath(".//*[local-name()='spine']/*[local-name()='itemref']/@idref")
            self.assertEqual(spine, ["ch1", "ch2"])
            ch1 = etree.fromstring(translated.read("EPUB/content/ch1.xhtml"))
            ns = {"x": "http://www.w3.org/1999/xhtml"}
            self.assertEqual(ch1.xpath("string(.//x:h1)", namespaces=ns), "第一章")
            self.assertEqual(ch1.xpath("string(.//x:em)", namespaces=ns), "勇敢的")
            self.assertEqual(ch1.xpath("string(.//x:img/@alt)", namespaces=ns), "蓝色方块")
            self.assertEqual(ch1.xpath("string(.//x:a[1]/@href)", namespaces=ns), "#note1")
            self.assertEqual(ch1.xpath("string(.//*[@id='note1'])", namespaces=ns), "脚注内容。")
            self.assertEqual(ch1.xpath("count(.//*[local-name()='math']/*[local-name()='mi'])"), 2)
            nav = etree.fromstring(translated.read("EPUB/toc.xhtml"))
            self.assertEqual(nav.xpath("count(.//x:ol)", namespaces=ns), 2)
            self.assertIn("第一章", " ".join(nav.xpath(".//x:a//text()", namespaces=ns)))
            bilingual_nav = etree.fromstring(parallel.read("EPUB/toc.xhtml"))
            self.assertEqual(bilingual_nav.xpath("count(.//x:ol)", namespaces=ns), 2)
            self.assertIn("第一章", " ".join(bilingual_nav.xpath(".//x:a//text()", namespaces=ns)))
            bilingual_ch1 = etree.fromstring(parallel.read("EPUB/content/ch1.xhtml"))
            paragraphs = bilingual_ch1.xpath(".//x:body/x:p", namespaces=ns)
            self.assertEqual(["".join(node.itertext()) for node in paragraphs[:4]], ["Hello brave world.", "你好，勇敢的世界。", "See a note and Chapter Two.", "参见一条注释和第二章。"])
            bilingual_ids = [element.get("id") for element in bilingual_ch1.iter() if element.get("id")]
            self.assertEqual(len(bilingual_ids), len(set(bilingual_ids)))
            self.assertEqual(bilingual_ch1.xpath("string(.//x:p[@lang='zh-Hans'][1]/@lang)", namespaces=ns), "zh-Hans")
            source_ch1 = etree.fromstring(original.read("EPUB/content/ch1.xhtml"))
            self.assertEqual(bilingual_ch1.xpath("count(.//x:td)", namespaces=ns), source_ch1.xpath("count(.//x:td)", namespaces=ns))
            self.assertEqual(bilingual_ch1.xpath("string(.//x:td/x:p[@lang='zh-Hans'])", namespaces=ns), "单元格。")
            self.assertEqual(bilingual_ch1.xpath("count(.//x:li)", namespaces=ns), source_ch1.xpath("count(.//x:li)", namespaces=ns))
            self.assertEqual(bilingual_ch1.xpath("count(.//x:figcaption)", namespaces=ns), source_ch1.xpath("count(.//x:figcaption)", namespaces=ns))

        before = len(provider.translation_calls)
        rerun = run_project(project, provider=provider)
        self.assertEqual(rerun, outputs)
        self.assertEqual(read_json(project / "state" / "state.json")["status"], "spotcheck_pending")
        self.assertEqual(len(provider.translation_calls), before)
        self.assertEqual(validate_project(project)["target_only"], str(target))
        target_only_project = create_project(source, self.root / "target-only-projects")
        target_only = run_project(target_only_project, provider=EpubProvider())
        self.assertEqual([path.name for path in target_only], ["input.zh-Hans.epub"])

    def test_encrypted_epub_is_rejected(self):
        source = self.root / "encrypted.epub"
        make_epub(source, encrypted=True)
        with self.assertRaisesRegex(ValueError, "Encrypted EPUB contains unsupported"):
            EpubAdapter().import_file(source, "en", "zh-Hans")

    def test_font_obfuscation_is_preserved_without_decryption(self):
        source = self.root / "obfuscated-font.epub"
        make_epub(source, obfuscated_font=True)
        project = create_project(source, self.root / "font-projects")
        output = run_project(project, provider=EpubProvider())[0]
        EpubAdapter().validate(output)
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as translated:
            self.assertEqual(translated.read("EPUB/fonts/book.ttf"), original.read("EPUB/fonts/book.ttf"))
            self.assertEqual(translated.read("META-INF/encryption.xml"), original.read("META-INF/encryption.xml"))

    def test_epub2_ncx_toc_is_translated_and_preserved(self):
        source = self.root / "legacy.epub"
        make_epub(source, epub2=True)
        project = create_project(source, self.root / "epub2-projects")
        output = run_project(project, provider=EpubProvider())[0]
        EpubAdapter().validate(output)
        with zipfile.ZipFile(output) as archive:
            ncx = etree.fromstring(archive.read("EPUB/toc.ncx"))
            ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
            self.assertEqual(ncx.xpath("count(.//n:navPoint)", namespaces=ns), 2)
            self.assertEqual(ncx.xpath(".//n:navLabel/n:text/text()", namespaces=ns), ["第一章", "第二章"])


if __name__ == "__main__":
    unittest.main()
