from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import zipfile
from urllib.parse import unquote, urlsplit

from lxml import etree

from private_translate.core.model import AssetReference, Document, Section, TranslationUnit, content_hash


CONTAINER = "META-INF/container.xml"
ENCRYPTION = "META-INF/encryption.xml"
MIMETYPE = "application/epub+zip"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
FONT_OBFUSCATION = "http://www.idpf.org/2008/embedding"
FONT_MEDIA_TYPES = {"font/ttf", "application/font-sfnt", "font/sfnt", "font/otf", "application/vnd.ms-opentype", "font/woff", "application/font-woff", "font/woff2"}
BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "caption", "figcaption", "dt", "dd", "title", "text"}
HIDDEN = {"code", "pre", "script", "style", "svg", "math"}
TRANSLATABLE_ATTRIBUTES = {"alt", "title", "aria-label"}
MARKER = re.compile(r"⟦(/?)([PK])(\d+)⟧")


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)


def _local(element) -> str:
    return etree.QName(element).localname.lower() if isinstance(element.tag, str) else ""


def _is_block(element) -> bool:
    if _local(element) in BLOCKS:
        return True
    semantics = " ".join(value for key, value in element.attrib.items() if etree.QName(key).localname in {"type", "role", "class"})
    return _local(element) in {"aside", "div", "section"} and bool(re.search(r"(?:footnote|endnote|doc-noteref|doc-footnote|doc-endnote)", semantics, re.I))


def _text(value) -> str:
    return "".join(value.itertext()) if value is not None else ""


def _xpath(element) -> str:
    parts = []
    while element is not None and isinstance(element.tag, str):
        preceding = sum(isinstance(sibling.tag, str) for sibling in element.itersiblings(preceding=True))
        parts.append(f"/*[{preceding + 1}]")
        element = element.getparent()
    return "".join(reversed(parts))


def _resolve_href(base: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"EPUB package reference must be local: {href}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise ValueError(f"EPUB package reference escapes the archive: {href}")
    return resolved


def _obfuscated_font_paths(archive: zipfile.ZipFile) -> set[str]:
    if ENCRYPTION not in archive.namelist():
        return set()
    try:
        root = etree.fromstring(archive.read(ENCRYPTION), _parser())
    except etree.XMLSyntaxError as error:
        raise ValueError("Encrypted EPUB has an invalid encryption descriptor") from error
    encrypted = root.xpath(".//*[local-name()='EncryptedData']")
    if not encrypted:
        raise ValueError("Encrypted EPUB has an unsupported encryption descriptor")
    paths = set()
    for entry in encrypted:
        algorithm = entry.xpath("string(.//*[local-name()='EncryptionMethod'][1]/@Algorithm)")
        uri = entry.xpath("string(.//*[local-name()='CipherReference'][1]/@URI)")
        parsed = urlsplit(uri)
        path = posixpath.normpath(unquote(parsed.path))
        if algorithm != FONT_OBFUSCATION or parsed.scheme or parsed.netloc or not path or path == ".." or path.startswith("../"):
            raise ValueError("Encrypted EPUB contains unsupported encryption or DRM; no decryption is attempted")
        if path not in archive.namelist():
            raise ValueError(f"Encrypted EPUB references a missing font resource: {path}")
        paths.add(path)
    return paths


def _validate_font_references(paths: set[str], manifest: dict[str, dict]) -> None:
    media_by_path = {item["path"]: item["media_type"] for item in manifest.values()}
    for path in paths:
        if media_by_path.get(path) not in FONT_MEDIA_TYPES:
            raise ValueError(f"Encrypted EPUB obfuscation reference is not a manifest font: {path}")


def _append_text(parent, value: str) -> None:
    if not value:
        return
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + value
    else:
        parent.text = (parent.text or "") + value


def _encode_block(element) -> tuple[str, list[dict]]:
    marks: list[dict] = []

    def check_text(value: str | None) -> str:
        value = value or ""
        if "⟦" in value or "⟧" in value:
            raise ValueError("EPUB source text contains reserved translation marker characters")
        return value

    def encode(current) -> str:
        result = [check_text(current.text)]
        for child in current:
            kind = _local(child)
            opaque = kind in HIDDEN or _is_block(child) or not len(child) and not (child.text or "").strip()
            ident = str(len(marks))
            if opaque:
                marks.append({"id": ident, "kind": "keep", "xpath": _xpath(child)})
                result.append(f"⟦K{ident}⟧")
            else:
                template = deepcopy(child)
                template.text = None
                template.tail = None
                for descendant in list(template):
                    template.remove(descendant)
                marks.append({"id": ident, "kind": "wrap", "node": etree.tostring(template, encoding="unicode")})
                result.append(f"⟦P{ident}⟧{encode(child)}⟦/P{ident}⟧")
            result.append(check_text(child.tail))
        return "".join(result)

    return encode(element), marks


def _decode_block(text: str, marks: list[dict], references: dict[str, object], *, copy_kept: bool = False):
    by_id = {mark["id"]: mark for mark in marks}
    counts: dict[tuple[str, str], int] = {}
    for match in MARKER.finditer(text):
        key = (match.group(1) + match.group(2), match.group(3))
        counts[key] = counts.get(key, 0) + 1
    for mark in marks:
        ident = mark["id"]
        valid = counts.get(("K", ident), 0) == 1 if mark["kind"] == "keep" else counts.get(("P", ident), 0) == 1 and counts.get(("/P", ident), 0) == 1
        if not valid:
            raise ValueError(f"Provider changed or lost EPUB format marker {ident}")

    def parse(position: int, closing: str | None = None):
        holder = etree.Element("holder")
        plain = []

        def flush():
            if plain:
                _append_text(holder, "".join(plain))
                plain.clear()

        while position < len(text):
            match = MARKER.search(text, position)
            if match is None:
                plain.append(text[position:])
                position = len(text)
                break
            plain.append(text[position:match.start()])
            flush()
            closing_marker, kind, ident = match.groups()
            position = match.end()
            if closing_marker:
                if closing != ident:
                    raise ValueError("Unbalanced EPUB translation markers")
                return holder, position
            mark = by_id[ident]
            if kind == "K":
                node = references[ident]
                if copy_kept:
                    node = deepcopy(node)
                node.tail = None
                holder.append(node)
            else:
                child_holder, position = parse(position, ident)
                wrapper = etree.fromstring(mark["node"].encode("utf-8"), _parser())
                wrapper.text = child_holder.text
                for child in list(child_holder):
                    holder_child_tail = child.tail
                    child.tail = None
                    wrapper.append(child)
                    child.tail = holder_child_tail
                holder.append(wrapper)
        flush()
        if closing is not None:
            raise ValueError("Missing closing EPUB translation marker")
        return holder, position

    return parse(0)[0]


def _replace_block(block, translated: str, marks: list[dict], references: dict[str, object], *, copy_kept=False):
    holder = _decode_block(translated, marks, references, copy_kept=copy_kept)
    block.text = holder.text
    for child in list(block):
        block.remove(child)
    for child in list(holder):
        tail = child.tail
        child.tail = None
        block.append(child)
        child.tail = tail


def _set_language(root, language: str) -> None:
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    root.set("lang", language)
    root.set(xml_lang, language)


class EpubAdapter:
    def import_file(self, path: Path, source_language: str, target_language: str) -> Document:
        with zipfile.ZipFile(path) as archive:
            names = [entry.filename for entry in archive.infolist()]
            if len(names) != len(set(names)):
                raise ValueError("EPUB contains duplicate archive member names")
            obfuscated_fonts = _obfuscated_font_paths(archive)
            if "mimetype" not in names or CONTAINER not in names:
                raise ValueError("EPUB is missing mimetype or META-INF/container.xml")
            container = etree.fromstring(archive.read(CONTAINER), _parser())
            package_path = container.xpath("string(.//*[local-name()='rootfile'][1]/@full-path)")
            if not package_path or package_path not in names:
                raise ValueError("EPUB container does not reference an existing package document")
            package = etree.fromstring(archive.read(package_path), _parser())
            manifest = {}
            for item in package.xpath(".//*[local-name()='manifest']/*[local-name()='item']"):
                item_id, href = item.get("id"), item.get("href")
                resolved = _resolve_href(package_path, href)
                if not item_id or resolved not in names:
                    raise ValueError(f"EPUB manifest references a missing resource: {href}")
                manifest[item_id] = {"href": href, "path": resolved, "media_type": item.get("media-type"), "properties": item.get("properties", "")}
            _validate_font_references(obfuscated_fonts, manifest)
            spine_node = package.xpath(".//*[local-name()='spine'][1]")[0]
            spine_ids = spine_node.xpath("./*[local-name()='itemref']/@idref")
            if not spine_ids or any(item_id not in manifest for item_id in spine_ids):
                raise ValueError("EPUB spine is empty or references a missing manifest item")
            nav_ids = [item_id for item_id, item in manifest.items() if "nav" in item["properties"].split()]
            nav_id = nav_ids[0] if len(nav_ids) == 1 else spine_node.get("toc")
            if len(nav_ids) > 1 or nav_id not in manifest or (not nav_ids and manifest[nav_id]["media_type"] != NCX_MEDIA_TYPE):
                raise ValueError("EPUB must reference exactly one EPUB 3 nav document or EPUB 2 NCX")

            xhtml = {}
            for item_id, item in manifest.items():
                if item["media_type"] in {"application/xhtml+xml", NCX_MEDIA_TYPE}:
                    xhtml[item["path"]] = etree.fromstring(archive.read(item["path"]), _parser())
            sections: list[Section] = []
            units: list[TranslationUnit] = []
            spine_path_ids = {manifest[item_id]["path"]: index for index, item_id in enumerate(spine_ids)}

            def add(text: str, section_id: str, locator: dict, kind: str, metadata=None, translatable: bool | None = None):
                translatable = any(char.isalpha() for char in text) if translatable is None else translatable
                unit = TranslationUnit.create(section_id=section_id, type=kind, source_text=text, translatable=translatable,
                    source_language=source_language, target_language=target_language, locator=locator, metadata=metadata)
                if not translatable:
                    unit.target_text = text
                    unit.translation_status = "translated"
                    unit.qa_status = "passed"
                    unit.last_qa_target_hash = content_hash(text)
                units.append(unit)

            for index, item_id in enumerate(spine_ids):
                item = manifest[item_id]
                root = xhtml[item["path"]]
                headings = root.xpath(".//*[local-name()='h1' or local-name()='h2' or local-name()='h3']")
                title = _text(headings[0]).strip() if headings else Path(item["path"]).stem
                section_id = f"spine-{index + 1:04d}"
                sections.append(Section(section_id, title, {"item_href": item["path"]}))
            processed = set()
            nav_path = manifest[nav_id]["path"]
            content_paths = list(dict.fromkeys([manifest[item_id]["path"] for item_id in spine_ids] + [nav_path]))
            for content_index, item_path in enumerate(content_paths):
                if item_path in processed:
                    continue
                processed.add(item_path)
                root = xhtml[item_path]
                section_index = spine_path_ids.get(item_path)
                if section_index is None:
                    section_id = f"supplement-{content_index + 1:04d}"
                    title = Path(item_path).stem
                    sections.append(Section(section_id, title, {"item_href": item_path}))
                else:
                    section_id = f"spine-{section_index + 1:04d}"
                for element in root.iter():
                    tag = _local(element)
                    if tag == "math":
                        add(etree.tostring(element, encoding="unicode"), section_id,
                            {"kind": "epub_opaque", "item_path": item_path, "xpath": _xpath(element)}, "formula", translatable=False)
                        continue
                    if tag in HIDDEN or not isinstance(element.tag, str):
                        continue
                    if _is_block(element) and any(char.isalpha() for char in _text(element)):
                        encoded, marks = _encode_block(element)
                        plain = re.sub(r"⟦/?[PK]\d+⟧", "", encoded).strip()
                        if plain:
                            add(encoded, section_id, {"kind": "epub_block", "item_path": item_path, "xpath": _xpath(element)},
                                "heading" if tag.startswith("h") or tag == "title" else "table" if tag in {"td", "th", "caption"} else "caption_note" if tag == "figcaption" else "paragraph",
                                {"marks": marks, "tag": tag}, any(char.isalpha() for char in plain))
                    for attribute in TRANSLATABLE_ATTRIBUTES:
                        value = element.get(attribute)
                        if value and any(char.isalpha() for char in value):
                            add(value, section_id, {"kind": "epub_attribute", "item_path": item_path, "xpath": _xpath(element), "attribute": attribute}, "caption_note")

            dc_titles = package.xpath(".//*[local-name()='metadata']/*[local-name()='title']")
            if dc_titles:
                metadata_section = "package-metadata"
                sections.insert(0, Section(metadata_section, "Book metadata", {"item_path": package_path}))
                for index, element in enumerate(dc_titles):
                    value = _text(element).strip()
                    if value:
                        add(value, metadata_section, {"kind": "epub_title", "item_path": package_path, "xpath": _xpath(element), "index": index}, "heading")

            assets = [AssetReference(item["path"], item["media_type"] or "application/octet-stream", content_hash(item["path"]))
                for item in manifest.values() if item["media_type"] != "application/xhtml+xml"]
            return Document("epub", source_language, target_language,
                {"title": _text(dc_titles[0]).strip() if dc_titles else Path(path).stem, "format": "epub", "package_path": package_path},
                sections, units, assets, {"spine": spine_ids, "nav_path": nav_path, "manifest": manifest})

    def render(self, document: Document, output_path: Path, bilingual: bool = False, source_path: Path | None = None) -> Path:
        if source_path is None:
            raise ValueError("EPUB rendering requires the preserved source EPUB path")
        target_language = document.target_language
        by_path = {}
        for unit in document.units:
            kind = unit.locator.get("kind")
            if kind in {"epub_block", "epub_attribute"} and unit.target_text is not None:
                by_path.setdefault(unit.locator["item_path"], []).append(unit)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source_path) as source:
            names = [entry.filename for entry in source.infolist()]
            _validate_font_references(_obfuscated_font_paths(source), document.structure["manifest"])
            updates = {}
            package_path = document.metadata["package_path"]
            for item_path, units in by_path.items():
                root = etree.fromstring(source.read(item_path), _parser())
                tree = root.getroottree()
                resolved = {}
                for unit in units:
                    locator = unit.locator
                    element = tree.xpath(locator["xpath"])[0]
                    if locator["kind"] == "epub_block":
                        refs = {mark["id"]: tree.xpath(mark["xpath"])[0] for mark in unit.metadata["marks"] if mark["kind"] == "keep"}
                        resolved[unit.id] = (element, refs)
                    else:
                        resolved[unit.id] = (element, {})

                for unit in units:
                    element, refs = resolved[unit.id]
                    kind = unit.locator["kind"]
                    if kind == "epub_attribute":
                        if not bilingual:
                            element.set(unit.locator["attribute"], unit.target_text)
                    elif not bilingual:
                        _replace_block(element, unit.target_text, unit.metadata["marks"], refs)
                    elif item_path == document.structure["nav_path"]:
                        _replace_block(element, unit.target_text, unit.metadata["marks"], refs)
                    elif _local(element) == "title":
                        element.text = f"{unit.source_text} — {unit.target_text}"
                    else:
                        clone = deepcopy(element)
                        _replace_block(clone, unit.target_text, unit.metadata["marks"], refs, copy_kept=True)
                        nested_blocks = [child for child in clone.iterdescendants() if _is_block(child)]
                        insert_inside = bool(nested_blocks) or _local(element) in {"li", "td", "th", "caption", "figcaption", "dt", "dd"}
                        if nested_blocks:
                            for nested in [child for child in clone.iterdescendants() if _is_block(child) or _local(child) in {"ol", "ul"}]:
                                parent = nested.getparent()
                                if parent is not None:
                                    parent.remove(nested)
                        if insert_inside:
                            target_block = etree.Element("span" if _local(element) == "dt" else "p")
                            target_block.text = clone.text
                            for child in list(clone):
                                clone.remove(child)
                                target_block.append(child)
                            clone = target_block
                        for copied in [clone, *clone.iterdescendants()]:
                            for attribute in list(copied.attrib):
                                if etree.QName(attribute).localname == "id":
                                    copied.attrib.pop(attribute, None)
                        _set_language(clone, target_language)
                        parent = element.getparent()
                        if insert_inside:
                            index = next((index for index, child in enumerate(element) if _is_block(child) or _local(child) in {"ol", "ul"}), len(element))
                            element.insert(index, clone)
                        elif parent is not None:
                            parent.insert(parent.index(element) + 1, clone)
                is_xhtml = any(item["path"] == item_path and item["media_type"] == "application/xhtml+xml" for item in document.structure["manifest"].values())
                if not bilingual and is_xhtml:
                    _set_language(root, target_language)
                updates[item_path] = etree.tostring(root, encoding="utf-8", xml_declaration=True, doctype=root.getroottree().docinfo.doctype or None)

            if package_path in names and any(unit.locator.get("kind") == "epub_title" for unit in document.units):
                package = etree.fromstring(source.read(package_path), _parser())
                for unit in document.units:
                    if unit.locator.get("kind") != "epub_title" or unit.target_text is None:
                        continue
                    title = package.xpath(unit.locator["xpath"])[0]
                    title.text = f"{unit.source_text} — {unit.target_text}" if bilingual else unit.target_text
                if not bilingual:
                    for language in package.xpath(".//*[local-name()='metadata']/*[local-name()='language']"):
                        if (language.text or "").strip() == document.source_language:
                            language.text = target_language
                updates[package_path] = etree.tostring(package, encoding="utf-8", xml_declaration=True, doctype=package.getroottree().docinfo.doctype or None)

            with zipfile.ZipFile(output_path, "w") as result:
                ordered = source.infolist()
                ordered.sort(key=lambda entry: (entry.filename != "mimetype", entry.header_offset))
                for entry in ordered:
                    data = updates.get(entry.filename, source.read(entry.filename))
                    if entry.filename == "mimetype":
                        entry.compress_type = zipfile.ZIP_STORED
                        data = MIMETYPE.encode("ascii")
                    result.writestr(entry, data)
        self.validate(output_path)
        return output_path

    def validate(self, path: Path) -> None:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or entries[0].filename != "mimetype" or entries[0].compress_type != zipfile.ZIP_STORED:
                raise ValueError("EPUB mimetype must be the first uncompressed ZIP member")
            if archive.read("mimetype") != MIMETYPE.encode("ascii"):
                raise ValueError("EPUB mimetype entry is invalid")
            obfuscated_fonts = _obfuscated_font_paths(archive)
            container = etree.fromstring(archive.read(CONTAINER), _parser())
            package_path = container.xpath("string(.//*[local-name()='rootfile'][1]/@full-path)")
            package = etree.fromstring(archive.read(package_path), _parser())
            names = set(archive.namelist())
            manifest = {}
            ids = set()
            for item in package.xpath(".//*[local-name()='manifest']/*[local-name()='item']"):
                item_id = item.get("id")
                resolved = _resolve_href(package_path, item.get("href"))
                if item_id in ids or resolved not in names:
                    raise ValueError(f"EPUB manifest has duplicate ids or a missing resource: {item_id}")
                ids.add(item_id)
                manifest[item_id] = (resolved, item.get("media-type"), item.get("properties", ""))
            _validate_font_references(obfuscated_fonts, {item_id: {"path": resource, "media_type": media_type} for item_id, (resource, media_type, _) in manifest.items()})
            spine_node = package.xpath(".//*[local-name()='spine'][1]")[0]
            spine = spine_node.xpath("./*[local-name()='itemref']/@idref")
            if not spine or any(item_id not in manifest for item_id in spine):
                raise ValueError("EPUB spine references a missing manifest item")
            nav = [entry for entry in manifest.values() if "nav" in entry[2].split()]
            toc_id = spine_node.get("toc")
            ncx = manifest.get(toc_id)
            if len(nav) > 1 or (not nav and (not ncx or ncx[1] != NCX_MEDIA_TYPE)):
                raise ValueError("EPUB must reference exactly one EPUB 3 nav document or EPUB 2 NCX")
            ids_by_path = {}
            for item_id, (resource, media_type, _) in manifest.items():
                if media_type == "application/xhtml+xml" or (media_type or "").endswith("+xml") or resource.endswith((".opf", ".xml", ".svg")):
                    tree = etree.fromstring(archive.read(resource), _parser())
                    all_ids = [element.get("id") for element in tree.iter() if element.get("id")]
                    if len(all_ids) != len(set(all_ids)):
                        raise ValueError(f"EPUB resource contains duplicate IDs: {resource}")
                    ids_by_path[resource] = set(all_ids)
            for resource in ids_by_path:
                root = etree.fromstring(archive.read(resource), _parser())
                for element in root.iter():
                    for attribute in ("href", "src"):
                        href = element.get(attribute)
                        if not href:
                            continue
                        parsed = urlsplit(href)
                        if parsed.scheme or parsed.netloc or href.startswith("data:"):
                            continue
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(resource), unquote(parsed.path))) if parsed.path else resource
                        if parsed.path and target not in names:
                            raise ValueError(f"EPUB link points to a missing resource: {resource} -> {href}")
                        if parsed.fragment and target in ids_by_path and unquote(parsed.fragment) not in ids_by_path[target]:
                            raise ValueError(f"EPUB link points to a missing fragment: {resource} -> {href}")
        epubcheck = shutil.which("epubcheck")
        if epubcheck:
            result = subprocess.run([epubcheck, str(path)], capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f"EPUBCheck failed: {result.stderr.strip() or result.stdout.strip()}")
