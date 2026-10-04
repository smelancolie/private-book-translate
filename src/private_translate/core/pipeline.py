from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
from typing import Protocol
from urllib.parse import unquote, urlsplit

from private_translate.formats.markdown import MarkdownAdapter
from private_translate.formats.epub import EpubAdapter
from .glossary import load_glossary, prepare_terminology
from .language import load_language_package
from .model import Document
from .batching import (
    ensure_task_budget, fit_task_batches, maximum_unit_tokens, provider_budgets,
    split_oversized_units, translation_batches,
)
from .providers import OpenAICompatibleProvider, ProviderCapabilities, TranslationProvider
from .qa import (
    _target_hash, run_book_consistency, run_book_consistency_agent,
    run_chapter_qa, run_chapter_qa_agent, validate_qa,
)
from .spotcheck import validate_spotcheck
from .execution import (
    PatchConflict,
    apply_translation_patch,
    build_translation_task,
    create_translation_task,
    load_translation_task,
    review_target_digest,
    select_translation_backend,
    validate_translation_patch,
)
from .state import merge_saved_state, read_json, write_json


DEFAULT_AGENT_MODEL = "gpt-6-luna-max"
DEFAULT_AGENT_REVIEW_MODEL = "gpt-6-luna-max"


class AgentExecutor(Protocol):
    def execute(self, task: dict, *, model: str) -> dict: ...


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
        raise ValueError("Pass model= or set PRIVATE_TRANSLATE_MODEL; set OPENAI_API_KEY for the provider adapter")
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
            for batch, batch_context, batch_glossary in translation_batches(units, provider.capabilities, language, context, glossary):
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


def _translate_agent(
    document: Document, project: Path, capabilities: ProviderCapabilities,
    language: dict, glossary: list[dict[str, str]], executor: AgentExecutor,
    model: str,
    resume_task_id: str | None = None,
) -> Document:
    owner_id = "agent:translation-owner"
    budget = provider_budgets(capabilities, language)["translation_batch_target_tokens"]
    for section, units in _pending_by_section(document):
        context = json.dumps({"section": section.title}, ensure_ascii=False)
        def pack(group):
            return [(batch, batch_context) for batch, batch_context, _ in
                    translation_batches(group, capabilities, language, context, glossary)]

        def draft(batch, batch_context):
            return build_translation_task(
                project, document, section.section_id, owner_id,
                unit_ids=[unit.id for unit in batch], batch_context=json.loads(batch_context),
            )

        if resume_task_id is not None:
            saved = load_translation_task(project, resume_task_id)
            selected = set(saved["unit_ids"])
            batch = [unit for unit in units if unit.id in selected]
            remaining = [unit for unit in units if unit.id not in selected]
            batches = [(batch, json.dumps(saved["context_capsule"]["batch"], ensure_ascii=False))]
            batches.extend(fit_task_batches(remaining, budget, pack, draft))
        else:
            batches = fit_task_batches(units, budget, pack, draft)
        for batch, batch_context in batches:
            task_path = create_translation_task(
                project, section.section_id, owner_id,
                unit_ids=[unit.id for unit in batch], batch_context=json.loads(batch_context),
                resume_task_id=resume_task_id,
            )
            resume_task_id = None
            task = load_translation_task(project, task_path.stem)
            for attempt in range(2):
                ensure_task_budget(task, budget)
                patch = executor.execute(task, model=model)
                if not isinstance(patch, dict):
                    raise ValueError("Agent executor must return a TranslationPatch object")
                patch.setdefault("model", model)
                try:
                    validate_translation_patch(project, patch)
                    document = apply_translation_patch(project, patch)
                except PatchConflict:
                    if attempt == 0:
                        task_path = create_translation_task(
                            project, section.section_id, owner_id,
                            unit_ids=[unit.id for unit in batch], batch_context=json.loads(batch_context),
                        )
                        task = load_translation_task(project, task_path.stem)
                        continue
                    raise
                write_json(project / "work" / "patches" / "translation" / f"{task['task_id']}.json", {
                    "task_id": task["task_id"], "patch_id": patch["patch_id"],
                    "chapter_id": task["chapter_id"], "owner_id": task["owner_id"],
                    "requested_backend": 1, "actual_backend": 1,
                    "model": patch.get("model", model), "patch": patch,
                    "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "base_chapter_digest": task["base_chapter_digest"],
                    "target_digest_after_merge": review_target_digest([
                        {"unit_id": unit.id, "target_text": unit.target_text or ""}
                        for unit in document.units if unit.section_id == section.section_id and unit.translatable
                    ]),
                })
                break
    return document


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
        raise ValueError("Project has not been parsed; call advance_host_run first")
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


def _write_run_state(project: Path, status: str, **updates) -> None:
    path = project / "state" / "state.json"
    state = read_json(path) if path.exists() else {}
    state.update({"last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status, **updates})
    write_json(path, state)


def run_project(
    project: Path, *, base_url: str | None = None, model: str | None = None,
    bilingual: bool = False, provider: TranslationProvider | None = None,
    translation_backend: int = 2, agent_executor: AgentExecutor | None = None,
    agent_model: str = DEFAULT_AGENT_MODEL, review_backend: int = 2,
    review_executor: AgentExecutor | None = None, reviewer_id: str = "agent:chapter-reviewer",
    review_model: str = DEFAULT_AGENT_REVIEW_MODEL, allow_review_fallback: bool = False,
    resume_translation_task_id: str | None = None, resume_fix_task_id: str | None = None,
    auxiliary_executor: AgentExecutor | None = None, capabilities: ProviderCapabilities | None = None,
    stop_after_terminology: bool = False,
) -> list[Path]:
    backend = select_translation_backend(translation_backend)
    if backend == "agent" and agent_executor is None:
        raise RuntimeError("AGENT_BACKEND_UNAVAILABLE: provide a host-native AgentExecutor; API fallback is disabled")
    selected_review_backend = select_translation_backend(review_backend)
    fallback_reason = None
    if selected_review_backend == "agent" and review_executor is None:
        if not allow_review_fallback:
            raise RuntimeError("AGENT_REVIEW_UNAVAILABLE: API fallback requires explicit allow_review_fallback")
        selected_review_backend = "api"
        fallback_reason = "AGENT_REVIEW_UNAVAILABLE"
    translation_owner_id = "agent:translation-owner" if backend == "agent" else "api:translation-owner"
    if selected_review_backend == "agent" and reviewer_id == translation_owner_id:
        raise ValueError("reviewer_id must differ from translation_owner_id")
    project, manifest, saved = _load_project(project)
    _write_run_state(
        project, "running", requested_backend=translation_backend,
        actual_backend=1 if backend == "agent" else 2,
        requested_review_backend=review_backend,
        actual_review_backend=1 if selected_review_backend == "agent" else 2,
        fallback_reason=fallback_reason,
        translation_owner_id=translation_owner_id,
        reviewer_id=reviewer_id if selected_review_backend == "agent" else "api:reviewer",
    )
    if bilingual:
        manifest["bilingual"] = True
        write_json(project / "project.json", manifest)
    language = load_language_package(manifest["source_language"], manifest["target_language"])
    active_provider = None if auxiliary_executor is not None else (provider or _provider(base_url, model, language))
    active_capabilities = capabilities if auxiliary_executor is not None else active_provider.capabilities
    source_path = project / manifest["source_file"]
    fresh = _adapter(manifest["format"]).import_file(source_path, manifest["source_language"], manifest["target_language"])
    split_oversized_units(fresh, maximum_unit_tokens(active_capabilities, language))
    if saved is not None:
        fresh = merge_saved_state(fresh, saved)
    write_json(project / "work" / "document.json", fresh.to_dict())
    prepare_terminology(project, fresh, active_provider, language,
                        executor=auxiliary_executor, model=agent_model, capabilities=active_capabilities)
    if stop_after_terminology:
        _write_run_state(project, "terminology_ready")
        return []
    glossary = load_glossary(project / "glossary" / "terms.csv")
    if backend == "agent":
        fresh = _translate_agent(fresh, project, active_capabilities, language, glossary, agent_executor, agent_model,
                                 resume_task_id=resume_translation_task_id)
    else:
        _translate(fresh, project, active_provider, language, glossary)
    if selected_review_backend == "agent":
        consistency_path = project / "qa" / "book-consistency.json"
        if auxiliary_executor is not None and consistency_path.exists():
            active_round = read_json(consistency_path).get("active_round")
            if active_round is not None and active_round["phase"] == "fix":
                fresh = run_book_consistency_agent(
                    project, fresh, active_capabilities, language, glossary, auxiliary_executor,
                    reviewer_id=reviewer_id, translation_owner_id=translation_owner_id,
                    review_model=review_model, translation_executor=agent_executor, translation_model=agent_model,
                )
        for _ in range(3):
            if auxiliary_executor is None:
                run_book_consistency(project, fresh, active_provider, language, glossary)
            fresh = run_chapter_qa_agent(
                project, fresh, active_provider, language, glossary, review_executor,
                reviewer_id=reviewer_id, translation_owner_id=translation_owner_id,
                review_model=review_model, translation_backend=1 if backend == "agent" else 2,
                translation_executor=agent_executor if backend == "agent" else None,
                translation_model=agent_model,
                resume_fix_task_id=resume_fix_task_id,
                capabilities=active_capabilities,
            )
            resume_fix_task_id = None
            if auxiliary_executor is not None:
                fresh = run_book_consistency_agent(
                    project, fresh, active_capabilities, language, glossary, auxiliary_executor,
                    reviewer_id=reviewer_id, translation_owner_id=translation_owner_id,
                    review_model=review_model, translation_executor=agent_executor, translation_model=agent_model,
                )
            report = read_json(project / "qa" / "book-consistency.json")
            if report.get("status") == "PASS" and report.get("target_hash") == _target_hash(
                [unit for unit in fresh.units if unit.translatable and unit.target_text is not None]
            ):
                break
        else:
            raise RuntimeError("Agent chapter QA and book consistency did not converge on one target revision")
        result = _build(project, manifest, fresh)
    else:
        run_chapter_qa(project, fresh, active_provider, language, glossary)
        run_book_consistency(project, fresh, active_provider, language, glossary)
        result = _build(project, manifest, fresh)
    _write_run_state(project, "spotcheck_pending")
    return result


def build_project(project: Path) -> list[Path]:
    project, manifest, document = _load_project(project)
    if document is None:
        raise ValueError("Project has not been parsed; call advance_host_run first")
    spotcheck = validate_spotcheck(project, document)
    result = _build(project, manifest, document)
    _write_run_state(project, "complete", spotcheck_run_id=spotcheck["run_id"])
    return result


def validate_project(project: Path) -> dict[str, str]:
    project, manifest, document = _load_project(project)
    if document is None:
        raise ValueError("Project has not been parsed; call advance_host_run first")
    validate_qa(project, document)
    target_path, bilingual_path = _output_paths(project, manifest)
    adapter = _adapter(manifest["format"])
    _assert_source_current(project, manifest, document, adapter)
    adapter.validate(target_path)
    if manifest.get("bilingual"):
        adapter.validate(bilingual_path)
    spotcheck = validate_spotcheck(project, document)
    _write_run_state(project, "complete", spotcheck_run_id=spotcheck["run_id"])
    return {"target_only": str(target_path), "bilingual": str(bilingual_path) if manifest.get("bilingual") else "not requested"}
