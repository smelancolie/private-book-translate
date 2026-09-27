from __future__ import annotations

from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import unquote, urlsplit

from .state import write_json


SUPPORTED_FORMATS = {".md": "markdown", ".epub": "epub"}


def create_project(source_path: Path, projects_dir: Path, source_language: str = "en", target_language: str = "zh-Hans") -> Path:
    source_path = source_path.resolve(strict=True)
    try:
        source_format = SUPPORTED_FORMATS[source_path.suffix.lower()]
    except KeyError as error:
        raise ValueError("Input must be an EPUB or Markdown (.md) file") from error
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", source_path.stem).strip("-").lower()[:48] or "book"
    project = projects_dir / f"{slug}-{uuid.uuid4().hex[:8]}"
    projects_dir.mkdir(parents=True, exist_ok=True)
    staging = projects_dir / f".{uuid.uuid4().hex}.init"
    assets = []
    if source_format == "markdown":
        from private_translate.formats.markdown import MarkdownAdapter

        assets = MarkdownAdapter().import_file(source_path, source_language, target_language).assets
    else:
        from private_translate.formats.epub import EpubAdapter

        EpubAdapter().import_file(source_path, source_language, target_language)
    staging.mkdir()
    try:
        source_dir = staging / "source"
        source_dir.mkdir()
        for directory in ("work", "glossary", "qa", "state", "output"):
            (staging / directory).mkdir()
        source_file = source_dir / source_path.name
        shutil.copy2(source_path, source_file)
        for asset in assets:
            parsed = urlsplit(asset.path)
            if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                continue
            relative = Path(unquote(parsed.path))
            original = (source_path.parent / relative).resolve(strict=True)
            original.relative_to(source_path.parent.resolve())
            destination = (source_dir / relative).resolve()
            destination.relative_to(staging.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        write_json(staging / "project.json", {
            "project_id": project.name,
            "format": source_format,
            "source_language": source_language,
            "target_language": target_language,
            "bilingual": False,
            "source_file": f"source/{source_path.name}",
        })
        (staging / "glossary" / "terms.csv").write_text(
            "source,preferred_translation,category,approved,forbidden_translations\n", encoding="utf-8"
        )
        staging.rename(project)
        return project
    except BaseException:
        if staging.exists() and staging.resolve().is_relative_to(projects_dir.resolve()):
            shutil.rmtree(staging)
        raise
