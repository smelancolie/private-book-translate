from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .model import Document, content_hash


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def merge_saved_state(fresh: Document, saved: Document | None) -> Document:
    if saved is None:
        return fresh
    previous = {unit.id: unit for unit in saved.units}
    for unit in fresh.units:
        old = previous.get(unit.id)
        if old is None:
            continue
        if old.source_hash != unit.source_hash:
            unit.translation_status = "stale"
            unit.qa_status = "pending"
            unit.target_text = None
            unit.last_qa_target_hash = None
            continue
        unit.target_text = old.target_text
        unit.translation_status = old.translation_status
        unit.qa_status = old.qa_status
        unit.last_qa_target_hash = old.last_qa_target_hash
        unit.last_error = old.last_error
        if unit.target_text is not None and content_hash(unit.target_text) != old.last_qa_target_hash:
            unit.qa_status = "pending"
            unit.last_qa_target_hash = None
    return fresh
