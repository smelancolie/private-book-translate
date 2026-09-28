from pathlib import Path
import shutil
import zipfile
import unittest

from lxml import etree

from private_translate.core.pipeline import assert_project_source_current, run_project, validate_project
from private_translate.core.project import create_project
from private_translate.core.model import Document
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json, write_json
from private_translate.core.providers import ProviderCapabilities
from private_translate.formats.epub import EpubAdapter


def make_epub(path: Path, encrypted: bool = False, obfuscated_font: bool = False, epub2: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "META-INF/container.xml": b'''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''',
        "EPUB/package.opf": b'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="bookid"><metadata><dc:identifier id="bookid">urn:test:book</dc:identifier><dc:title>EPUB Sample</dc:title><dc:creator id="author-one">Author One</dc:creator><dc:creator id="author-two">Author Two</dc:creator><dc:language>en</dc:language><dc:language>en</dc:language><dc:language>fr</dc:language></metadata><manifest><item id="ch1" href="content/ch1.xhtml" media-type="application/xhtml+xml"/><item id="ch2" href="content/ch2.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="toc.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="styles/book.css" media-type="text/css"/><item id="pixel" href="images/pixel.png" media-type="image/png"/><item id="svg" href="images/mark.svg" media-type="image/svg+xml"/><item id="font" href="fonts/book.ttf" media-type="font/ttf"/></manifest><spine><itemref idref="ch1"/><itemref idref="ch2"/></spine></package>''',
        "EPUB/content/ch1.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>Chapter One</title><link rel="stylesheet" href="../styles/book.css"/></head><body><h1 id="start">Chapter One</h1><p>Hello <em class="voice">brave</em> world.</p><p>See <a href="#note1">a note</a> and <a href="ch2.xhtml#second">Chapter Two</a>.</p><ol><li id="list-item">First note.<a href="#note1">note</a></li></ol><table><tr><th>Column</th><td>One cell.</td></tr></table><math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mi>y</mi></math><figure><a href="../images/mark.svg#mark"><img src="../images/pixel.png" alt="A blue square" title="Blue square"/></a><figcaption>A sample figure.</figcaption></figure><aside id="note1" epub:type="footnote">Footnote text.</aside><div id="direct">Direct text outside a child block. <span id="inline-id" lang="en">Localized span</span> Tail text.</div><p><ruby>Ruby term<rt>pronunciation</rt></ruby>.</p><section epub:type="chapter"><aside id="nested-note" epub:type="footnote"><p>Nested footnote text.<a href="#nested-ref">Ref</a></p><p id="nested-ref">Note back-reference.</p></aside></section></body></html>''',
        "EPUB/content/ch2.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Chapter Two</title></head><body><h1 id="second">Chapter Two</h1><p>Welcome to the second chapter.</p></body></html>''',
        "EPUB/toc.xhtml": b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Contents</title></head><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Contents</h1><ol><li><a href="content/ch1.xhtml#start">Chapter One</a><ol><li><a href="content/ch2.xhtml#second">Second chapter</a></li></ol></li></ol></nav></body></html>''',
        "EPUB/styles/book.css": b"body { font-family: serif; } .voice { font-style: italic; }",
        "EPUB/images/pixel.png": b"synthetic-pixel-image",
        "EPUB/images/mark.svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2"><title>Blue mark</title><circle id="mark" cx="1" cy="1" r="1"/></svg>',
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


def edit_epub_xml(path: Path, member: str, edit) -> None:
    temporary = path.with_name(f".{path.name}.edited")
    with zipfile.ZipFile(path) as archive:
        entries = []
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == member:
                root = etree.fromstring(data)
                edit(root)
                data = etree.tostring(root, encoding="utf-8", xml_declaration=True)
            entries.append((info, data))
    with zipfile.ZipFile(temporary, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    temporary.replace(path)


class EpubProvider:
    capabilities = ProviderCapabilities("fake", "test", 116000, 16000, True, False)
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
        self.review_calls = []

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
        self.review_calls.append((source, target, context))
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
            self.assertEqual(ch1.xpath("string(.//x:figure/x:a/@href)", namespaces=ns), "../images/mark.svg#mark")
            self.assertEqual(ch1.xpath("string(.//x:a[1]/@href)", namespaces=ns), "#note1")
            self.assertEqual(ch1.xpath("string(.//*[@id='note1'])", namespaces=ns), "脚注内容。")
            self.assertIn("Direct text outside a child block。", "".join(ch1.xpath(".//x:div[@id='direct']//text()", namespaces=ns)))
            self.assertEqual(ch1.xpath("count(.//x:span[@id='inline-id'][@lang='en'])", namespaces=ns), 1)
            self.assertEqual(ch1.xpath("count(.//x:ruby/x:rt)", namespaces=ns), 1)
            self.assertEqual(ch1.xpath("count(.//x:aside[@id='nested-note'])", namespaces=ns), 1)
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
            self.assertEqual("".join(paragraphs[1].itertext()), "".join(etree.fromstring(translated.read("EPUB/content/ch1.xhtml")).xpath(".//x:body/x:p[1]", namespaces=ns)[0].itertext()))
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

    def test_bilingual_epub_declares_source_and_target_languages(self):
        source = self.root / "languages.epub"
        make_epub(source)
        project = create_project(source, self.root / "projects")
        target, bilingual = run_project(project, bilingual=True, provider=EpubProvider())

        with zipfile.ZipFile(target) as archive:
            target_package = etree.fromstring(archive.read("EPUB/package.opf"))
            target_languages = target_package.xpath(".//*[local-name()='metadata']/*[local-name()='language']/text()")
        with zipfile.ZipFile(bilingual) as archive:
            bilingual_package = etree.fromstring(archive.read("EPUB/package.opf"))
            bilingual_languages = bilingual_package.xpath(".//*[local-name()='metadata']/*[local-name()='language']/text()")

        self.assertEqual(target_languages, ["zh-Hans", "fr"])
        self.assertEqual(target_languages.count("zh-Hans"), 1)
        self.assertEqual(bilingual_languages, ["en", "zh-Hans", "fr"])
        self.assertEqual(len(bilingual_languages), len(set(bilingual_languages)))
        self.assertEqual(target_package.xpath(".//*[local-name()='metadata']/*[local-name()='creator']/text()"), ["Author One", "Author Two"])
        self.assertEqual(bilingual_package.xpath(".//*[local-name()='metadata']/*[local-name()='creator']/text()"), ["Author One", "Author Two"])

    def test_bilingual_epub_does_not_duplicate_chapter_heading(self):
        source = self.root / "heading.epub"
        make_epub(source)
        project = create_project(source, self.root / "projects")
        _, bilingual = run_project(project, bilingual=True, provider=EpubProvider())

        with zipfile.ZipFile(bilingual) as archive:
            chapter = etree.fromstring(archive.read("EPUB/content/ch1.xhtml"))
        headings = chapter.xpath(".//*[local-name()='body']/*[local-name()='h1']")
        heading_ids = [element.get("id") for element in chapter.iter() if element.get("id")]

        self.assertEqual(len(headings), 1)
        self.assertEqual("".join(headings[0].itertext()), "Chapter One — 第一章")
        self.assertEqual(len(heading_ids), len(set(heading_ids)))

    def test_epub_import_creates_figure_audit_unit(self):
        from private_translate.core.spotcheck import _eligible_units

        source = self.root / "figure.epub"
        make_epub(source)
        document = EpubAdapter().import_file(source, "en", "zh-Hans")
        figure = next(unit for unit in document.units if unit.type == "figure")

        self.assertFalse(figure.translatable)
        self.assertEqual(figure.metadata["asset_path"], "EPUB/images/pixel.png")
        self.assertTrue(figure.metadata["asset_hash"])
        self.assertEqual(figure.metadata["alt"], "A blue square")
        self.assertTrue(figure.metadata["caption_unit_ids"])
        self.assertIn(figure, _eligible_units(document)["figure"])

    def test_epub_persistent_ids_survive_sibling_insertion(self):
        source = self.root / "input.epub"
        make_epub(source)
        project = create_project(source, self.root / "projects")
        source_copy = project / "source" / "input.epub"
        provider = EpubProvider()
        run_project(project, provider=provider)
        before = Document.from_dict(read_json(project / "work" / "document.json"))
        old_ids = {unit.source_text: unit.id for unit in before.units}
        old_targets = {unit.source_text: unit.target_text for unit in before.units}
        old_call_count = len(provider.translation_calls)

        def insert_paragraph(root):
            body = root.xpath(".//*[local-name()='body']")[0]
            paragraph = etree.Element("{http://www.w3.org/1999/xhtml}p")
            paragraph.text = "Inserted before the first paragraph."
            first_paragraph = body.xpath("./*[local-name()='p']")[0]
            body.insert(body.index(first_paragraph), paragraph)

        edit_epub_xml(source_copy, "EPUB/content/ch1.xhtml", insert_paragraph)
        run_project(project, provider=provider)
        after = Document.from_dict(read_json(project / "work" / "document.json"))
        by_text = {unit.source_text: unit for unit in after.units}
        for text, unit_id in old_ids.items():
            self.assertEqual(by_text[text].id, unit_id)
            self.assertEqual(by_text[text].target_text, old_targets[text])
        self.assertIn("Inserted before the first paragraph.", by_text)
        self.assertEqual(len(provider.translation_calls), old_call_count + 1)

    def test_epub_href_change_requires_resume_and_preserves_translation(self):
        source = self.root / "input.epub"
        make_epub(source)
        project = create_project(source, self.root / "projects")
        provider = EpubProvider()
        run_project(project, provider=provider)
        before = Document.from_dict(read_json(project / "work" / "document.json"))
        old = next(unit for unit in before.units if "Chapter Two" in unit.source_text and "See " in unit.source_text)
        old_fingerprint = before.structure_fingerprint
        source_copy = project / "source" / "input.epub"

        def change_href(root):
            link = root.xpath(".//*[local-name()='a'][@href='ch2.xhtml#second']")[0]
            link.set("href", "../content/ch2.xhtml#second")

        edit_epub_xml(source_copy, "EPUB/content/ch1.xhtml", change_href)
        with self.assertRaisesRegex(ValueError, "Source changed"):
            assert_project_source_current(project)
        old_call_count = len(provider.translation_calls)
        run_project(project, provider=provider)
        after = Document.from_dict(read_json(project / "work" / "document.json"))
        new = next(unit for unit in after.units if "Chapter Two" in unit.source_text and "See " in unit.source_text)

        self.assertEqual(new.id, old.id)
        self.assertEqual(new.target_text, old.target_text)
        self.assertNotEqual(after.structure_fingerprint, old_fingerprint)
        self.assertEqual(len(provider.translation_calls), old_call_count)

    def test_epub_id_change_requires_resume_and_refreshes_mapping(self):
        source = self.root / "input.epub"
        make_epub(source)
        project = create_project(source, self.root / "projects")
        provider = EpubProvider()
        run_project(project, provider=provider)
        before = Document.from_dict(read_json(project / "work" / "document.json"))
        old = next(unit for unit in before.units if unit.type == "heading" and unit.source_text == "Chapter One")
        old_fingerprint = before.structure_fingerprint
        source_copy = project / "source" / "input.epub"

        def rename_heading_id(root):
            root.xpath(".//*[local-name()='h1'][@id='start']")[0].set("id", "start-new")

        edit_epub_xml(source_copy, "EPUB/content/ch1.xhtml", rename_heading_id)
        edit_epub_xml(
            source_copy,
            "EPUB/toc.xhtml",
            lambda root: root.xpath(".//*[local-name()='a'][contains(@href, '#start')]")[0].set("href", "content/ch1.xhtml#start-new"),
        )
        with self.assertRaisesRegex(ValueError, "Source changed"):
            assert_project_source_current(project)
        old_call_count = len(provider.translation_calls)
        run_project(project, provider=provider)
        after = Document.from_dict(read_json(project / "work" / "document.json"))
        new = next(unit for unit in after.units if unit.type == "heading" and unit.source_text == "Chapter One")

        self.assertEqual(new.id, old.id)
        self.assertNotEqual(after.structure_fingerprint, old_fingerprint)
        self.assertEqual(len(provider.translation_calls), old_call_count)


if __name__ == "__main__":
    unittest.main()
