from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from private_translate.core.model import AssetReference, Document, Section, TranslationUnit, content_hash


BLOCK_TEXT_TYPES = {"Header", "Para", "Plain"}
OPAQUE_INLINE_TYPES = {"Code", "Math", "RawInline", "Note"}
INLINE_CHILD_PATHS = {
    "Emph": [], "Strong": [], "Strikeout": [], "Superscript": [], "Subscript": [],
    "SmallCaps": [], "Underline": [], "Quoted": [1], "Span": [1],
    "Link": [1], "Image": [1], "Cite": [1],
}
TOKEN = re.compile(r"⟦(/?)([PK])(\d+)⟧")
HIDDEN_HTML = {"script", "style", "pre", "code", "svg", "math"}
HUMAN_HTML_ATTRIBUTES = {"alt", "title", "aria-label"}


def _set_at(value: Any, path: list[int], replacement: Any) -> None:
    current = value
    for index in path[:-1]:
        current = current[index]
    current[path[-1]] = replacement


def _get_at(value: Any, path: list[int]) -> Any:
    current = value
    for index in path:
        current = current[index]
    return current


def _set_inline_children(node: dict[str, Any], path: list[int], children: list[dict[str, Any]]) -> None:
    if path:
        _set_at(node["c"], path, children)
    else:
        node["c"] = children


def _text_inlines(inlines: list[dict[str, Any]]) -> str:
    out = []
    for node in inlines:
        kind = node.get("t")
        if kind == "Str":
            out.append(node["c"])
        elif kind in {"Space", "SoftBreak", "LineBreak"}:
            out.append(" ")
        elif kind in INLINE_CHILD_PATHS:
            out.append(_text_inlines(_get_at(node["c"], INLINE_CHILD_PATHS[kind])))
        elif kind == "Note":
            out.append("[footnote]")
    return "".join(out)


def _wrap_path(kind: str) -> list[int] | None:
    return INLINE_CHILD_PATHS.get(kind)


def _encode_inlines(inlines: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    marks: list[dict[str, Any]] = []

    def encode(nodes: list[dict[str, Any]], prefix: list[int | str]) -> str:
        result = []
        for index, node in enumerate(nodes):
            kind = node.get("t")
            node_path = prefix + [index]
            if kind == "Str":
                value = node["c"]
                if "⟦" in value or "⟧" in value:
                    raise ValueError("Source text contains reserved translation marker characters")
                result.append(value)
            elif kind in {"Space", "SoftBreak", "LineBreak"}:
                result.append(" " if kind == "Space" else "\n")
            elif kind in OPAQUE_INLINE_TYPES or _wrap_path(kind) is None:
                ident = str(len(marks))
                marks.append({"id": ident, "kind": "keep", "node": deepcopy(node), "relative_path": node_path})
                result.append(f"⟦K{ident}⟧")
            else:
                ident = str(len(marks))
                child_path = _wrap_path(kind)
                template = deepcopy(node)
                children = _get_at(template["c"], child_path)
                _set_inline_children(template, child_path, [])
                marks.append({"id": ident, "kind": "wrap", "node": template, "child_path": child_path})
                result.append(f"⟦P{ident}⟧{encode(children, node_path + ["c"] + child_path)}⟦/P{ident}⟧")
        return "".join(result)

    return encode(inlines, []), marks


def _text_to_inlines(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in re.split(r"(\s+)", text):
        if not part:
            continue
        if part.isspace():
            for char in part:
                result.append({"t": "SoftBreak" if char == "\n" else "Space"})
        else:
            result.append({"t": "Str", "c": part})
    return result


def _decode_inlines(text: str, marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {mark["id"]: mark for mark in marks}
    counts: dict[tuple[str, str], int] = {}
    for match in TOKEN.finditer(text):
        key = (match.group(1) + match.group(2), match.group(3))
        counts[key] = counts.get(key, 0) + 1
    for mark in marks:
        ident = mark["id"]
        if mark["kind"] == "keep":
            valid = counts.get(("K", ident), 0) == 1
        else:
            valid = counts.get(("P", ident), 0) == 1 and counts.get(("/P", ident), 0) == 1
        if not valid:
            raise ValueError(f"Provider changed or lost format marker {ident}")

    def parse(position: int, closing: str | None = None):
        nodes: list[dict[str, Any]] = []
        plain = []

        def flush() -> None:
            if plain:
                nodes.extend(_text_to_inlines("".join(plain)))
                plain.clear()

        while position < len(text):
            match = TOKEN.search(text, position)
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
                    raise ValueError("Unbalanced translation format markers")
                return nodes, position
            mark = by_id[ident]
            if kind == "K":
                nodes.append(deepcopy(mark["node"]))
            else:
                children, position = parse(position, ident)
                wrapper = deepcopy(mark["node"])
                _set_inline_children(wrapper, mark["child_path"], children)
                nodes.append(wrapper)
        flush()
        if closing is not None:
            raise ValueError("Missing closing translation format marker")
        return nodes, position

    result, _ = parse(0)
    return result


def _walk_html(markup: str):
    wrapper = html.fragment_fromstring(markup, create_parent="div")
    rows = []

    def visit(element, hidden=False):
        tag = etree.QName(element).localname.lower() if isinstance(element.tag, str) else ""
        hidden = hidden or tag in HIDDEN_HTML
        path = wrapper.getroottree().getpath(element)
        if not hidden and element.text and element.text.strip():
            rows.append((path, "text", element.text, "paragraph"))
        if not hidden:
            for attribute in HUMAN_HTML_ATTRIBUTES:
                value = element.get(attribute)
                if value and value.strip():
                    rows.append((path, f"attr:{attribute}", value, "caption_note"))
        for child in element:
            visit(child, hidden)
            if not hidden and child.tail and child.tail.strip():
                rows.append((wrapper.getroottree().getpath(child), "tail", child.tail, "paragraph"))

    visit(wrapper)
    return wrapper, rows


def _render_html(wrapper, rows: list[tuple[TranslationUnit, str]]) -> str:
    tree = wrapper.getroottree()
    for unit, target in rows:
        element = tree.xpath(unit.locator["html_xpath"])[0]
        slot = unit.locator["html_slot"]
        if slot == "text":
            element.text = target
        elif slot == "tail":
            element.tail = target
        else:
            element.set(slot[5:], target)
    out = wrapper.text or ""
    for child in wrapper:
        out += html.tostring(child, encoding="unicode", with_tail=True)
    return out


class MarkdownAdapter:
    def __init__(self, pandoc: str | None = None):
        self.pandoc = pandoc or shutil.which("pandoc")
        if not self.pandoc:
            raise RuntimeError("Pandoc 3 or newer is required for Markdown AST parsing")

    def _pandoc(self, args: list[str], data: str) -> str:
        result = subprocess.run([self.pandoc, *args], input=data, text=True, encoding="utf-8", capture_output=True)
        if result.returncode:
            raise RuntimeError(f"Pandoc failed: {result.stderr.strip()}")
        return result.stdout

    def import_file(self, path: Path, source_language: str, target_language: str) -> Document:
        source = path.read_text(encoding="utf-8")
        ast = json.loads(self._pandoc(["--from=gfm", "--to=json"], source))
        sections: list[Section] = []
        units: list[TranslationUnit] = []
        assets: list[AssetReference] = []
        active_section = "section-0000"
        active_title = ""
        heading_number = 0

        def add_unit(text: str, locator: dict[str, Any], kind: str, translatable: bool, metadata=None):
            unit = TranslationUnit.create(
                section_id=active_section, type=kind, source_text=text, translatable=translatable,
                source_language=source_language, target_language=target_language, locator=locator, metadata=metadata,
            )
            if not translatable:
                unit.target_text = text
                unit.translation_status = "translated"
                unit.qa_status = "passed"
                unit.last_qa_target_hash = content_hash(text)
            units.append(unit)

        metadata_fields = ast.get("meta", {})
        if any(metadata_fields.get(key) for key in ("title", "description", "abstract")):
            active_section = "frontmatter"
            sections.append(Section(active_section, "Metadata", {"kind": "metadata"}))
        for key in ("title", "description", "abstract"):
            value = metadata_fields.get(key)
            if not value:
                continue
            if value.get("t") == "MetaString":
                text, marks = value.get("c", ""), []
                meta_kind = "MetaString"
            elif value.get("t") == "MetaInlines":
                text, marks = _encode_inlines(value.get("c", []))
                meta_kind = "MetaInlines"
            else:
                continue
            add_unit(text, {"kind": "metadata", "key": key}, "heading", any(char.isalpha() for char in (_text_inlines(value.get("c", [])) if meta_kind == "MetaInlines" else text)), {"meta_kind": meta_kind, "marks": marks})
        active_section = "section-0000"

        def walk(value: Any, path: list[str | int], *, in_table=False, in_note=False):
            nonlocal active_section, active_title, heading_number
            if isinstance(value, list):
                for index, child in enumerate(value):
                    child_path = path + [index]
                    if path == ["blocks"] and isinstance(child, dict) and child.get("t") == "Header":
                        heading_number += 1
                        active_section = f"section-{heading_number:04d}"
                        active_title = _text_inlines(child["c"][2])
                        sections.append(Section(active_section, active_title, {"path": child_path}))
                    walk(child, child_path, in_table=in_table, in_note=in_note)
                return
            if not isinstance(value, dict):
                return
            kind = value.get("t")
            if kind == "Table":
                in_table = True
            if kind == "Note":
                in_note = True
            if kind == "Image":
                target = value.get("c", [None, None, [""]])[2][0]
                parsed = urlsplit(target)
                if target and not parsed.scheme and not parsed.netloc and not target.startswith("/"):
                    suffix = Path(unquote(parsed.path)).suffix.lower()
                    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
                    assets.append(AssetReference(target, media, content_hash(target), {"path": path}))
            if kind in {"Image", "Link"}:
                title = value.get("c", [None, None, [None, ""]])[2][1]
                if title and any(char.isalpha() for char in title):
                    add_unit(title, {"kind": "inline-attribute", "path": path, "field": "title"}, "caption_note", True, {"ast_type": kind})
            if kind == "Header":
                text, marks = _encode_inlines(value["c"][2])
                plain = _text_inlines(value["c"][2])
                add_unit(text, {"kind": "block", "path": path}, "heading", any(char.isalpha() for char in plain), {"ast_type": kind, "marks": marks})
            elif kind in {"Para", "Plain"}:
                text, marks = _encode_inlines(value["c"])
                plain = _text_inlines(value["c"])
                unit_type = "caption_note" if in_note else "table" if in_table else "paragraph"
                add_unit(text, {"kind": "block", "path": path}, unit_type, any(char.isalpha() for char in plain), {"ast_type": kind, "marks": marks})
            elif kind == "CodeBlock":
                add_unit(value["c"][1], {"kind": "code", "path": path}, "code", False, {"ast_type": kind})
                return
            elif kind == "Math":
                add_unit(value["c"][1], {"kind": "formula", "path": path}, "formula", False, {"ast_type": kind})
                return
            elif kind == "RawBlock" and value["c"][0].lower() == "html":
                _, rows = _walk_html(value["c"][1])
                for xpath, slot, text, unit_type in rows:
                    add_unit(text, {"kind": "html", "path": path, "html_xpath": xpath, "html_slot": slot}, unit_type, any(char.isalpha() for char in text), {"ast_type": kind})
                return
            for key, child in value.items():
                if key != "t":
                    walk(child, path + [key], in_table=in_table, in_note=in_note)

        blocks = ast.get("blocks", [])
        if blocks and not any(block.get("t") == "Header" for block in blocks if isinstance(block, dict)):
            sections.append(Section(active_section, active_title, {"path": ["blocks"]}))
        walk(blocks, ["blocks"])
        if not sections:
            sections.append(Section(active_section, active_title, {"path": ["blocks"]}))
        metadata = {"title": _meta_text(metadata_fields.get("title")), "format": "markdown"}
        return Document("markdown", source_language, target_language, metadata, sections, units, assets, {"pandoc": ast})

    def render(self, document: Document, output_path: Path, bilingual: bool = False) -> Path:
        ast = deepcopy(document.structure["pandoc"])
        units = {tuple(unit.locator["path"]): unit for unit in document.units if unit.locator.get("kind") == "block"}
        inline_attributes = {tuple(unit.locator["path"]): unit for unit in document.units if unit.locator.get("kind") == "inline-attribute"}
        metadata_units = {unit.locator["key"]: unit for unit in document.units if unit.locator.get("kind") == "metadata"}

        for key, unit in metadata_units.items():
            if unit.target_text is None:
                continue
            node = ast["meta"][key]
            if node["t"] == "MetaString":
                node["c"] = f"{node['c']} — {unit.target_text}" if bilingual else unit.target_text
            elif node["t"] == "MetaInlines":
                translated = _decode_inlines(unit.target_text, unit.metadata["marks"])
                node["c"] = deepcopy(node["c"]) + [{"t": "Space"}, {"t": "Str", "c": "—"}, {"t": "Space"}] + translated if bilingual else translated

        def transform(value: Any, path: tuple[str | int, ...] = ()):
            if isinstance(value, list):
                output = []
                for index, child in enumerate(value):
                    updated = transform(child, path + (index,))
                    if isinstance(child, dict) and child.get("t") in BLOCK_TEXT_TYPES and isinstance(updated, list):
                        output.extend(updated)
                    else:
                        output.append(updated)
                return output
            if isinstance(value, dict):
                attribute_unit = inline_attributes.get(path)
                if attribute_unit and attribute_unit.target_text is not None:
                    value = deepcopy(value)
                    value["c"][2][1] = attribute_unit.target_text
                unit = units.get(path)
                if unit and unit.translatable and unit.target_text is not None:
                    translated = deepcopy(value)
                    inline_path = path + ("c", 2) if value["t"] == "Header" else path + ("c",)
                    marks = deepcopy(unit.metadata["marks"])
                    for mark in marks:
                        if mark["kind"] == "keep" and mark["node"].get("t") == "Note":
                            mark["node"] = transform(mark["node"], inline_path + tuple(mark["relative_path"]))
                    if value["t"] == "Header":
                        translated["c"][2] = _decode_inlines(unit.target_text, marks)
                    else:
                        translated["c"] = _decode_inlines(unit.target_text, marks)
                    translated["c"] = transform(translated["c"], path + ("c",))
                    if bilingual:
                        return [deepcopy(value), translated]
                    return translated
                if value.get("t") == "RawBlock" and value["c"][0].lower() == "html":
                    html_units = [(unit, unit.target_text) for unit in document.units if unit.locator.get("kind") == "html" and tuple(unit.locator["path"]) == path and unit.target_text is not None]
                    if html_units:
                        wrapper, _ = _walk_html(value["c"][1])
                        value = deepcopy(value)
                        value["c"][1] = _render_html(wrapper, [(unit, target) for unit, target in html_units])
                    return value
                result = {}
                for key, child in value.items():
                    result[key] = transform(child, path + (key,)) if key != "t" else child
                return result
            return value

        ast = transform(ast)
        rendered = self._pandoc(["--from=json", "--to=gfm", "--standalone"], json.dumps(ast, ensure_ascii=False))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8", newline="\n")
        return output_path

    def validate(self, path: Path) -> None:
        self._pandoc(["--from=gfm", "--to=json"], path.read_text(encoding="utf-8"))


def _meta_text(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    if value.get("t") == "MetaString":
        return value.get("c", "")
    if value.get("t") == "MetaInlines":
        return _text_inlines(value.get("c", []))
    return ""
