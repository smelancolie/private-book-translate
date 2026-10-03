from pathlib import Path
import shutil
import unittest
import zipfile

from lxml import etree

from private_translate.formats.epub import EpubAdapter


def make_minimal_epub(path: Path) -> None:
    members = {
        "META-INF/container.xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
        "EPUB/package.opf": b'''<?xml version="1.1" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="book-id">
  <metadata><dc:identifier id="book-id">urn:uuid:9dc8d4da-21ef-4978-a18f-85c7a19677ea</dc:identifier>
    <dc:title>Minimal Book</dc:title><dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-09-28T00:00:00Z</meta>
  </metadata>
  <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
  <spine toc="ncx"><itemref idref="chapter"/></spine>
</package>''',
        "EPUB/nav.xhtml": b'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
  <head><title>Navigation</title></head><body><nav epub:type="toc"><h1>Contents</h1>
    <ol><li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body>
</html>''',
        "EPUB/chapter.xhtml": b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html SYSTEM "missing.xhtml.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">
  <head><title>Chapter</title></head><body><h1>Chapter</h1><p>Hello.</p></body>
</html>''',
        "EPUB/toc.ncx": b'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:stale-source-id"/></head>
  <docTitle><text>Minimal Book</text></docTitle>
  <navMap><navPoint id="chapter" playOrder="1"><navLabel><text>Chapter</text></navLabel><content src="chapter.xhtml"/></navPoint></navMap>
</ncx>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in members.items():
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


def build_minimal_epub(source: Path, output: Path) -> Path:
    adapter = EpubAdapter()
    document = adapter.import_file(source, "en", "zh-Hans")
    for unit in document.units:
        if unit.translatable:
            unit.target_text = unit.source_text
    return adapter.render(document, output, source_path=source)


class TemporaryEpubTests(unittest.TestCase):
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


class EpubBuildRegressionTests(TemporaryEpubTests):
    def test_render_normalizes_epub_metadata_and_preserves_body_text(self):
        source = self.root / "source.epub"
        output = self.root / "built.epub"
        make_minimal_epub(source)
        with zipfile.ZipFile(source) as archive:
            source_chapter = etree.fromstring(archive.read("EPUB/chapter.xhtml"))
            source_body_text = "".join(source_chapter.xpath(".//*[local-name()='body']//text()"))
            self.assertEqual(source_chapter.getroottree().docinfo.doctype, '<!DOCTYPE html SYSTEM "missing.xhtml.dtd">')
            source_ncx = etree.fromstring(archive.read("EPUB/toc.ncx"))
            self.assertEqual(source_ncx.xpath("string(.//*[local-name()='meta'][@name='dtb:uid']/@content)"), "urn:uuid:stale-source-id")

        build_minimal_epub(source, output)

        with zipfile.ZipFile(output) as archive:
            opf_bytes = archive.read("EPUB/package.opf")
            self.assertTrue(opf_bytes.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'))
            package = etree.fromstring(opf_bytes)
            identifier_id = package.get("unique-identifier")
            identifier = package.xpath(
                ".//*[local-name()='metadata']/*[local-name()='identifier'][@id=$identifier_id]",
                identifier_id=identifier_id,
            )[0]
            ncx = etree.fromstring(archive.read("EPUB/toc.ncx"))
            self.assertEqual(
                ncx.xpath("string(.//*[local-name()='meta'][@name='dtb:uid']/@content)"),
                "".join(identifier.itertext()),
            )
            chapter = etree.fromstring(archive.read("EPUB/chapter.xhtml"))
            self.assertEqual(chapter.getroottree().docinfo.doctype, "<!DOCTYPE html>")
            self.assertEqual("".join(chapter.xpath(".//*[local-name()='body']//text()")), source_body_text)


class EpubCheckTests(TemporaryEpubTests):
    @unittest.skipUnless(shutil.which("epubcheck"), "EPUBCheck is only installed in the optional CI job")
    def test_minimal_epub_passes_epubcheck(self):
        source = self.root / "source.epub"
        output = self.root / "built.epub"
        make_minimal_epub(source)
        build_minimal_epub(source, output)


if __name__ == "__main__":
    unittest.main()
