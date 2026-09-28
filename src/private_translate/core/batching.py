from __future__ import annotations

from dataclasses import replace
import json
import math
import re
import uuid

from .model import Document, TranslationUnit, content_hash
from .providers import ProviderCapabilities


_FORMAT_MARKER = re.compile(r"⟦(/?)([PK])(\d+)⟧")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])(?=\s+)")
_REQUEST_OVERHEAD_TOKENS = 16


def estimate_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 3)


def provider_budgets(provider, language: dict[str, object]) -> dict[str, int]:
    capabilities = provider.capabilities
    if not isinstance(capabilities, ProviderCapabilities):
        raise TypeError("Providers must expose verified ProviderCapabilities")
    configured = language["provider_limits"]
    max_output = min(int(configured["max_output_tokens"]), capabilities.max_output_tokens)
    max_input = min(int(configured["max_input_tokens"]), capabilities.max_context_tokens - max_output)
    if max_input < 1 or max_output < 1:
        raise ValueError("Provider token capabilities leave no usable input or output budget")
    translation = min(int(configured["translation_batch_target_tokens"]), max_input)
    review = min(int(configured["review_batch_target_tokens"]), max_input)
    return {"max_input_tokens": max_input, "max_output_tokens": max_output,
            "translation_batch_target_tokens": translation, "review_batch_target_tokens": review}


def _markers_balanced_before(text: str, position: int) -> bool:
    stack = []
    for match in _FORMAT_MARKER.finditer(text, 0, position):
        closing, kind, ident = match.groups()
        if kind != "P":
            continue
        if closing:
            if not stack or stack[-1] != ident:
                return False
            stack.pop()
        else:
            stack.append(ident)
    return not stack


def _sentence_chunks(text: str) -> list[str]:
    cuts = [match.start() for match in _SENTENCE_BOUNDARY.finditer(text)
            if text[match.start():].strip() and _markers_balanced_before(text, match.start())]
    if not cuts:
        return [text]
    chunks = []
    start = 0
    for cut in cuts:
        if text[start:cut].strip():
            chunks.append(text[start:cut])
            start = cut
    if text[start:].strip():
        chunks.append(text[start:])
    return chunks or [text]


def _group_sentences(text: str, max_tokens: int) -> list[str]:
    sentences = _sentence_chunks(text)
    groups = []
    current = ""
    for sentence in sentences:
        if estimate_tokens(sentence) > max_tokens:
            raise ValueError("A source sentence exceeds the provider budget and cannot be split safely")
        if current and estimate_tokens(current + sentence) > max_tokens:
            groups.append(current)
            current = ""
        current += sentence
    if current:
        groups.append(current)
    return groups


def split_oversized_units(document: Document, max_tokens: int) -> None:
    units = []
    for unit in document.units:
        if not unit.translatable or estimate_tokens(unit.source_text) <= max_tokens:
            units.append(unit)
            continue
        groups = _group_sentences(unit.source_text, max_tokens)
        if len(groups) == 1:
            raise ValueError(f"Translation unit {unit.id} exceeds the provider budget without a safe sentence boundary")
        for index, source_text in enumerate(groups):
            metadata = {**unit.metadata, "_split_index": index, "_split_count": len(groups)}
            units.append(replace(
                unit, id=str(uuid.uuid4()), source_parent_id=unit.source_parent_id,
                source_text=source_text, source_hash=content_hash(source_text), metadata=metadata,
                target_text=None, translation_status="pending", qa_status="pending",
                last_qa_target_hash=None, last_error=None,
            ))
    document.units = units


def maximum_unit_tokens(provider, language: dict[str, object]) -> int:
    budgets = provider_budgets(provider, language)
    return max(1, min(
        budgets["max_input_tokens"], budgets["translation_batch_target_tokens"],
        budgets["review_batch_target_tokens"], budgets["max_output_tokens"],
    ) // 3)


def _glossary_slice(units: list[TranslationUnit], glossary: list[dict[str, str]]) -> list[dict[str, str]]:
    source = " ".join(unit.source_text for unit in units).casefold()
    return [term for term in glossary if term["source"].casefold() in source]


def _neighbor_context(units: list[TranslationUnit], start: int, end: int, base_context: str) -> str:
    neighbors = []
    if start:
        previous = units[start - 1]
        neighbors.append({"position": "previous", "source": previous.source_text[-120:], "target": (previous.target_text or "")[-120:]})
    if end < len(units):
        following = units[end]
        neighbors.append({"position": "next", "source": following.source_text[:120], "target": (following.target_text or "")[:120]})
    return json.dumps({"section_context": base_context, "neighbors": neighbors}, ensure_ascii=False)


def translation_batches(units: list[TranslationUnit], provider, language: dict[str, object], context: str, glossary: list[dict[str, str]]):
    max_tokens = provider_budgets(provider, language)["translation_batch_target_tokens"]
    system_cost = estimate_tokens(language["translation_prompt"]) + _REQUEST_OVERHEAD_TOKENS
    result = []
    start = 0
    batch = []
    terms: list[dict[str, str]] = []
    batch_context = ""
    for index, unit in enumerate(units):
        candidate = batch + [unit]
        candidate_terms = _glossary_slice(candidate, glossary)
        candidate_context = _neighbor_context(units, start, index + 1, context)
        user_payload = {
            "context": candidate_context,
            "glossary": candidate_terms,
            "units": [{"id": row.id, "text": row.source_text} for row in candidate],
            "required_output": {"translations": [{"id": "unit id", "target_text": "translation"}]},
        }
        if system_cost + estimate_tokens(json.dumps(user_payload, ensure_ascii=False)) > max_tokens:
            if not batch:
                raise ValueError(f"Translation unit {unit.id} exceeds the configured input budget")
            result.append((batch, batch_context, terms))
            start = index
            batch = [unit]
            terms = _glossary_slice(batch, glossary)
            candidate_context = _neighbor_context(units, start, index + 1, context)
            user_payload = {
                "context": candidate_context,
                "glossary": terms,
                "units": [{"id": unit.id, "text": unit.source_text}],
                "required_output": {"translations": [{"id": "unit id", "target_text": "translation"}]},
            }
            if system_cost + estimate_tokens(json.dumps(user_payload, ensure_ascii=False)) > max_tokens:
                raise ValueError(f"Translation unit {unit.id} exceeds the configured input budget")
        else:
            batch.append(unit)
            terms = candidate_terms
        batch_context = candidate_context
    if batch:
        result.append((batch, batch_context, terms))
    return result


def review_batches(units: list[TranslationUnit], provider, language: dict[str, object], context: str, rubric: str):
    max_tokens = provider_budgets(provider, language)["review_batch_target_tokens"]
    system_cost = estimate_tokens(rubric) + _REQUEST_OVERHEAD_TOKENS
    result = []
    start = 0
    batch = []
    batch_context = ""
    for index, unit in enumerate(units):
        local_context = _neighbor_context(units, start, index + 1, context)
        candidate = batch + [unit]
        source = "\n\n".join(f"[{row.id}] {row.source_text}" for row in candidate if row.translatable)
        target = json.dumps({row.id: row.target_text for row in candidate}, ensure_ascii=False)
        payload = {
            "source": source, "target": target, "context": local_context,
            "required_output": {"issues": [{"unit_id": "unit id", "severity": "P1", "description": "issue"}]},
        }
        if system_cost + estimate_tokens(json.dumps(payload, ensure_ascii=False)) > max_tokens:
            if not batch:
                raise ValueError(f"Review unit {unit.id} exceeds the configured input budget")
            result.append((batch, batch_context))
            start = index
            batch = [unit]
            local_context = _neighbor_context(units, start, index + 1, context)
            payload = {
                "source": f"[{unit.id}] {unit.source_text}" if unit.translatable else "",
                "target": json.dumps({unit.id: unit.target_text}, ensure_ascii=False),
                "context": local_context,
                "required_output": {"issues": [{"unit_id": "unit id", "severity": "P1", "description": "issue"}]},
            }
            if system_cost + estimate_tokens(json.dumps(payload, ensure_ascii=False)) > max_tokens:
                raise ValueError(f"Review unit {unit.id} exceeds the configured input budget")
        else:
            batch.append(unit)
        batch_context = local_context
    if batch:
        result.append((batch, batch_context))
    return result
