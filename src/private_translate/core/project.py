from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import uuid
from urllib.parse import unquote, urlsplit

from .glossary import FIELDS
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
        (staging / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
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
        glossary_header = ",".join(FIELDS) + "\n"
        (staging / "glossary" / "terms.csv").write_text(glossary_header, encoding="utf-8")
        (staging / "glossary" / "candidates.csv").write_text(glossary_header, encoding="utf-8")
        if shutil.which("git"):
            git_root = subprocess.run(
                ["git", "-C", str(staging), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True,
            )
            if git_root.returncode == 0:
                relative_private_path = (staging / "work" / "private-state.json").relative_to(git_root.stdout.strip())
                ignored = subprocess.run(
                    ["git", "-C", git_root.stdout.strip(), "check-ignore", "--quiet", "--no-index", "--", relative_private_path.as_posix()]
                )
                if ignored.returncode == 1:
                    raise ValueError(f"Private project workspace is not ignored by Git: {project}")
                ignored.check_returncode()
        staging.rename(project)
        return project
    except BaseException:
        if staging.exists() and staging.resolve().is_relative_to(projects_dir.resolve()):
            shutil.rmtree(staging)
        raise
