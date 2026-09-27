from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from private_translate.core.pipeline import assert_project_source_current, build_project, run_project, validate_project
from private_translate.core.project import create_project
from private_translate.core.model import Document
from private_translate.core.spotcheck import create_spotcheck_round
from private_translate.core.state import read_json


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="translate", description="Local private book translation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a private workspace from an EPUB or Markdown file")
    init.add_argument("input_file", type=Path)
    init.add_argument("--projects-dir", type=Path, default=Path("projects"))
    init.add_argument("--source-language", default="en")
    init.add_argument("--target-language", default="zh-Hans")
    for name in ("run", "resume", "status", "validate", "build"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)
        if name in {"run", "resume"}:
            command.add_argument("--bilingual", action="store_true")
        if name in {"run", "resume"}:
            command.add_argument("--base-url")
            command.add_argument("--model")
    spotcheck = commands.add_parser("spotcheck", help="generate a stratified review sample for the current run")
    spotcheck.add_argument("project", type=Path)
    spotcheck.add_argument("--seed", type=int)
    spotcheck.add_argument("--new-run", action="store_true", help="start a new current-run review campaign")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        project = create_project(args.input_file, args.projects_dir, args.source_language, args.target_language)
        print(project)
        return 0
    if args.command == "status":
        project = args.project.resolve(strict=True)
        manifest = read_json(project / "project.json")
        document_path = project / "work" / "document.json"
        if not document_path.exists():
            print(f"{manifest['project_id']}: imported; no units yet")
            return 0
        document = read_json(document_path)
        counts: dict[str, int] = {}
        for unit in document["units"]:
            status = unit["translation_status"] if unit["translation_status"] != "translated" else f"qa_{unit['qa_status']}"
            counts[status] = counts.get(status, 0) + 1
        run_state = read_json(project / "state" / "state.json").get("status", "not run") if (project / "state" / "state.json").exists() else "not run"
        print(f"{manifest['project_id']}: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) + f"; run={run_state}")
        return 0
    try:
        if args.command == "spotcheck":
            project = args.project.resolve(strict=True)
            assert_project_source_current(project)
            document = Document.from_dict(read_json(project / "work" / "document.json"))
            print(create_spotcheck_round(project, document, args.seed, new_run=args.new_run))
            return 0
        if args.command in {"run", "resume"}:
            outputs = run_project(args.project, base_url=args.base_url, model=args.model, bilingual=args.bilingual)
            print("\n".join(str(path) for path in outputs))
        elif args.command == "build":
            print("\n".join(str(path) for path in build_project(args.project)))
        elif args.command == "validate":
            print(json.dumps(validate_project(args.project), ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, FileNotFoundError, NotImplementedError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
