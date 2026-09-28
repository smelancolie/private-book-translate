from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import uuid

from .model import Document, TranslationUnit, content_hash


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _key(unit: TranslationUnit, include_structure: bool):
    base = (unit.type, unit.source_hash)
    return base + (unit.structure_hash,) if include_structure else base


def _match_key(old: list[TranslationUnit], new: list[TranslationUnit], matches: dict[int, int], include_structure: bool) -> None:
    old_used = set(matches.values())
    new_used = set(matches)
    old_groups = defaultdict(list)
    new_groups = defaultdict(list)
    for index, unit in enumerate(old):
        if index not in old_used:
            old_groups[_key(unit, include_structure)].append(index)
    for index, unit in enumerate(new):
        if index not in new_used:
            new_groups[_key(unit, include_structure)].append(index)

    def context(units, index):
        previous = _key(units[index - 1], include_structure) if index else None
        following = _key(units[index + 1], include_structure) if index + 1 < len(units) else None
        return previous, following

    matching_keys = old_groups.keys() & new_groups.keys()
    for key in matching_keys:
        old_rows, new_rows = old_groups[key], new_groups[key]
        old_contexts = defaultdict(list)
        new_contexts = defaultdict(list)
        for index in old_rows:
            old_contexts[context(old, index)].append(index)
        for index in new_rows:
            new_contexts[context(new, index)].append(index)
        for neighbor in old_contexts.keys() & new_contexts.keys():
            if len(old_contexts[neighbor]) == len(new_contexts[neighbor]) == 1:
                matches[new_contexts[neighbor][0]] = old_contexts[neighbor][0]
    matched_old = set(matches.values())
    matched_new = set(matches)
    for key in matching_keys:
        old_rows, new_rows = old_groups[key], new_groups[key]
        remaining_old = [index for index in old_rows if index not in matched_old]
        remaining_new = [index for index in new_rows if index not in matched_new]
        for old_index, new_index in zip(remaining_old, remaining_new):
            matches[new_index] = old_index


def _match_changed_text(old: list[TranslationUnit], new: list[TranslationUnit], matches: dict[int, int]) -> None:
    anchors = [(-1, -1), *sorted((old_index, new_index) for new_index, old_index in matches.items()), (len(old), len(new))]
    for (old_start, new_start), (old_end, new_end) in zip(anchors, anchors[1:]):
        old_gap = list(range(old_start + 1, old_end))
        new_gap = list(range(new_start + 1, new_end))
        if len(old_gap) != len(new_gap):
            continue
        for old_index, new_index in zip(old_gap, new_gap):
            before, after = old[old_index], new[new_index]
            if before.type == after.type and before.structure_hash == after.structure_hash:
                matches[new_index] = old_index


def reconcile_document(fresh: Document, saved: Document | None) -> Document:
    if saved is None:
        return fresh
    old, new = saved.units, fresh.units
    matches: dict[int, int] = {}
    id_mapping = {unit.id: unit.id for unit in new}
    _match_key(old, new, matches, include_structure=True)
    _match_key(old, new, matches, include_structure=False)
    _match_changed_text(old, new, matches)

    for new_index, old_index in matches.items():
        current, previous = new[new_index], old[old_index]
        fresh_id = current.id
        current.id = previous.id if _uuid(previous.id) else current.id
        id_mapping[fresh_id] = current.id
        current.source_parent_id = previous.source_parent_id if _uuid(previous.source_parent_id) else current.id
        if previous.source_hash != current.source_hash:
            current.translation_status = "stale"
            current.qa_status = "pending"
            current.target_text = None
            current.last_qa_target_hash = None
            continue
        current.target_text = previous.target_text
        current.translation_status = previous.translation_status
        current.qa_status = previous.qa_status
        current.last_qa_target_hash = previous.last_qa_target_hash
        current.last_error = previous.last_error
        if previous.structure_hash != current.structure_hash or previous.section_id != current.section_id:
            current.qa_status = "pending"
            current.last_qa_target_hash = None
        if current.target_text is not None and content_hash(current.target_text) != current.last_qa_target_hash:
            current.qa_status = "pending"
            current.last_qa_target_hash = None
    for unit in new:
        for field in ("caption_unit_ids", "surrounding_unit_ids"):
            if field in unit.metadata:
                unit.metadata[field] = [id_mapping.get(unit_id, unit_id) for unit_id in unit.metadata[field]]
    return fresh
