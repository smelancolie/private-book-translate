from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import unquote, urlsplit

from private_translate.formats.markdown import MarkdownAdapter
from private_translate.formats.epub import EpubAdapter
from .glossary import load_glossary, prepare_terminology
from .language import load_language_package
from .model import Document
from .batching import maximum_unit_tokens, split_oversized_units, translation_batches
from .providers import OpenAICompatibleProvider, TranslationProvider
from .qa import run_book_consistency, run_chapter_qa, validate_qa
from .spotcheck import validate_spotcheck
from .state import merge_saved_state, read_json, write_json


def _load_project(project: Path):
    project = project.resolve(strict=True)
    manifest = read_json(project / "project.json")
    document_path = project / "work" / "document.json"
    document = Document.from_dict(read_json(document_path)) if document_path.exists() else None
    return project, manifest, document


def _provider(base_url: str | None, model: str | None, language: dict) -> TranslationProvider:
    endpoint = base_url or os.environ.get("PRIVATE_TRANSLATE_BASE_URL", "https://api.openai.com/v1")
    selected_model = model or os.environ.get("PRIVATE_TRANSLATE_MODEL")
    if not selected_model:
        raise ValueError("Pass --model or set PRIVATE_TRANSLATE_MODEL; set OPENAI_API_KEY for the provider adapter")
    return OpenAICompatibleProvider(endpoint, selected_model)


def _adapter(format_name: str):
    return MarkdownAdapter() if format_name == "markdown" else EpubAdapter()


def _pending_by_section(document: Document):
    pending_by_section = {}
    for unit in document.units:
        if unit.translatable and (unit.target_text is None or unit.translation_status != "translated"):
            pending_by_section.setdefault(unit.section_id, []).append(unit)
    for section in document.sections:
        pending = pending_by_section.get(section.section_id, [])
        if pending:
            yield section, pending


def _translate(document: Document, project: Path, provider: TranslationProvider, language: dict, glossary: list[dict[str, str]]) -> None:
    for section, units in _pending_by_section(document):
        context = json.dumps({"section": section.title}, ensure_ascii=False)
        try:
            for batch, batch_context, batch_glossary in translation_batches(units, provider, language, context, glossary):
                translations = provider.translate(batch, batch_context, batch_glossary, language)
                if set(translations) != {unit.id for unit in batch}:
                    raise ValueError("Provider response must contain exactly one translation per requested unit")
                for unit in batch:
                    unit.target_text = translations[unit.id]
                    unit.translation_status = "translated"
                    unit.qa_status = "pending"
                    unit.last_qa_target_hash = None
                    unit.last_error = None
        except Exception as error:
            for unit in units:
                if unit.target_text is None or unit.translation_status != "translated":
                    unit.last_error = str(error)
            write_json(project / "work" / "document.json", document.to_dict())
            raise
        write_json(project / "work" / "document.json", document.to_dict())


def _output_paths(project: Path, manifest: dict) -> tuple[Path, Path]:
    source_name = Path(manifest["source_file"]).stem
    language = manifest["target_language"]
    extension = ".md" if manifest["format"] == "markdown" else ".epub"
    target = project / "output" / f"{source_name}.{language}{extension}"
    bilingual = project / "output" / f"{source_name}.{manifest['source_language']}-{language}.bilingual{extension}"
    return target, bilingual


def _copy_markdown_assets(project: Path, document: Document) -> None:
    source_root = project / "source"
    output_root = project / "output"
    for asset in document.assets:
        parsed = urlsplit(asset.path)
        if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
            continue
        relative = Path(unquote(parsed.path))
        source = (source_root / relative).resolve(strict=True)
        destination = (output_root / relative).resolve()
        source.relative_to(project.resolve())
        destination.relative_to(project.resolve())
        if source == destination:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _assert_source_current(project: Path, manifest: dict, document: Document, adapter) -> None:
    source = adapter.import_file(project / manifest["source_file"], manifest["source_language"], manifest["target_language"])
    if (
        source.source_file_sha256 != document.source_file_sha256
        or source.source_fingerprint != document.source_fingerprint
        or source.structure_fingerprint != document.structure_fingerprint
    ):
        raise ValueError("Source changed since the current translation state; resume before building")


def assert_project_source_current(project: Path) -> None:
    project, manifest, document = _load_project(project)
    if document is None:
        raise ValueError("Project has not been parsed; run translate run first")
    _assert_source_current(project, manifest, document, _adapter(manifest["format"]))


def _build(project: Path, manifest: dict, document: Document) -> list[Path]:
    validate_qa(project, document)
    adapter = _adapter(manifest["format"])
    _assert_source_current(project, manifest, document, adapter)
    target_path, bilingual_path = _output_paths(project, manifest)
    source_path = project / manifest["source_file"]
    built = [adapter.render(document, target_path, source_path=source_path) if manifest["format"] == "epub" else adapter.render(document, target_path)]
    if manifest.get("bilingual"):
        built.append(adapter.render(document, bilingual_path, bilingual=True, source_path=source_path) if manifest["format"] == "epub" else adapter.render(document, bilingual_path, bilingual=True))
    if manifest["format"] == "markdown":
        _copy_markdown_assets(project, document)
    write_json(project / "qa" / "alignment.json", {
        "source_language": manifest["source_language"],
        "target_language": manifest["target_language"],
        "alignments": [
            {"unit_id": unit.id, "section_id": unit.section_id, "source_hash": unit.source_hash,
             "target_hash": unit.last_qa_target_hash, "locator": unit.locator}
            for unit in document.units if unit.translatable
        ],
    })
    return built


def run_project(project: Path, *, base_url: str | None = None, model: str | None = None, bilingual: bool = False, provider: TranslationProvider | None = None) -> list[Path]:
    project, manifest, saved = _load_project(project)
    if bilingual:
        manifest["bilingual"] = True
        write_json(project / "project.json", manifest)
    language = load_language_package(manifest["source_language"], manifest["target_language"])
    active_provider = provider or _provider(base_url, model, language)
    source_path = project / manifest["source_file"]
    fresh = _adapter(manifest["format"]).import_file(source_path, manifest["source_language"], manifest["target_language"])
    split_oversized_units(fresh, maximum_unit_tokens(active_provider, language))
    if saved is not None:
        fresh = merge_saved_state(fresh, saved)
    write_json(project / "work" / "document.json", fresh.to_dict())
    prepare_terminology(project, fresh, active_provider, language)
    glossary = load_glossary(project / "glossary" / "terms.csv")
    _translate(fresh, project, active_provider, language, glossary)
    run_chapter_qa(project, fresh, active_provider, language, glossary)
    run_book_consistency(project, fresh, active_provider, language, glossary)
    result = _build(project, manifest, fresh)
    write_json(project / "state" / "state.json", {"last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "spotcheck_pending"})
    return result


def build_project(project: Path) -> list[Path]:
    project, manifest, document = _load_project(project)
    if document is None:
        raise ValueError("Project has not been parsed; run translate run first")
    spotcheck = validate_spotcheck(project, document)
    result = _build(project, manifest, document)
    write_json(project / "state" / "state.json", {"last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "complete", "spotcheck_run_id": spotcheck["run_id"]})
    return result


def validate_project(project: Path) -> dict[str, str]:
    project, manifest, document = _load_project(project)
    if document is None:
        raise ValueError("Project has not been parsed; run translate run first")
    validate_qa(project, document)
    target_path, bilingual_path = _output_paths(project, manifest)
    adapter = _adapter(manifest["format"])
    _assert_source_current(project, manifest, document, adapter)
    adapter.validate(target_path)
    if manifest.get("bilingual"):
        adapter.validate(bilingual_path)
    spotcheck = validate_spotcheck(project, document)
    write_json(project / "state" / "state.json", {"last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "complete", "spotcheck_run_id": spotcheck["run_id"]})
    return {"target_only": str(target_path), "bilingual": str(bilingual_path) if manifest.get("bilingual") else "not requested"}
