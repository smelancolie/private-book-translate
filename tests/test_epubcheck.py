from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from private_translate.formats.epub import EpubAdapter


@unittest.skipUnless(shutil.which("epubcheck"), "EPUBCheck is only installed in the optional CI job")
class EpubCheckTests(unittest.TestCase):
    def test_minimal_epub_passes_epubcheck(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            path = Path(temporary) / "minimal.epub"
            members = {
                "META-INF/container.xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
                "EPUB/package.opf": b'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0" unique-identifier="book-id">
  <metadata><dc:identifier id="book-id">urn:uuid:9dc8d4da-21ef-4978-a18f-85c7a19677ea</dc:identifier>
    <dc:title>Minimal Book</dc:title><dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-09-28T00:00:00Z</meta>
  </metadata>
  <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>''',
                "EPUB/nav.xhtml": b'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
  <head><title>Navigation</title></head><body><nav epub:type="toc"><h1>Contents</h1>
    <ol><li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body>
</html>''',
                "EPUB/chapter.xhtml": b'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">
  <head><title>Chapter</title></head><body><h1>Chapter</h1><p>Hello.</p></body>
</html>''',
            }
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                for name, data in members.items():
                    archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
            EpubAdapter().validate(path)


if __name__ == "__main__":
    unittest.main()
