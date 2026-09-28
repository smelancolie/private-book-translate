from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .model import Document
from .reconcile import reconcile_document


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
    return reconcile_document(fresh, saved)
