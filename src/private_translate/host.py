"""Nonblocking task handoff between a local host and the existing Python core.

The host dispatches returned tasks and submits their JSON results. No Python
process waits for an Agent, and only the core applies patches and QA evidence.
"""
from pathlib import Path
import uuid

from .core.execution import validate_translation_patch, validate_review_evidence_current
from .core.glossary import validate_terminology_result, validate_terminology_task_current
from .core.providers import ProviderCapabilities
from .core.qa import (
    validate_book_consistency_result, validate_book_consistency_task_current,
    validate_book_consistency_fix_current,
)
from .core.pipeline import (
    DEFAULT_AGENT_MODEL, DEFAULT_AGENT_REVIEW_MODEL, _load_project,
    _write_run_state, assert_project_source_current, run_project,
)
from .core.spotcheck import (
    DEFAULT_AGENT_SPOTCHECK_MODEL, REVIEWERS, accept_spotcheck_evidence_pair,
    create_spotcheck_round, create_spotcheck_tasks, finalize_spotcheck_round,
    validate_spotcheck_evidence_current,
)
from .core.state import read_json, write_json


class HostTaskPending(Exception):
    """Yield control to the host instead of blocking inside executor.execute."""

    def __init__(self, task: dict, model: str):
        self.task = task
        self.model = model
        super().__init__(f"Host task pending: {task['task_id']}")


def _response_path(project: Path, task_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(task_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Invalid host task ID") from error
    if normalized != task_id:
        raise ValueError("Invalid host task ID")
    return project / "work" / "host" / "results" / f"{task_id}.json"


class FileHostExecutor:
    def __init__(self, project: Path):
        self.project = project

    def execute(self, task: dict, *, model: str) -> dict:
        response = _response_path(self.project, task["task_id"])
        if response.exists():
            return read_json(response)
        raise HostTaskPending(task, model)


def _request_path(project: Path) -> Path:
    return project / "work" / "host" / "request.json"


def _dispatch(task: dict, model: str) -> dict:
    return {"task": task, "model": model,
            "worker_id": task.get("owner_id") or task["reviewer_id"]}


def _validate_auxiliary_task_current(project: Path, task: dict) -> None:
    if task["task_type"] == "terminology":
        validate_terminology_task_current(project, task)
    elif task["task_type"] == "book_consistency":
        validate_book_consistency_task_current(project, task)
    elif task["task_type"] == "translation" and (task["context_capsule"].get("batch") or {}).get("consistency_issues"):
        validate_book_consistency_fix_current(project, task)


def get_host_request(project: Path) -> dict:
    """Read a durable request; only return tasks whose results are still missing."""
    project = project.resolve(strict=True)
    path = _request_path(project)
    request = read_json(path) if path.exists() else {"status": "not_started", "tasks": []}
    if request["status"] == "awaiting_host":
        request["tasks"] = [item for item in request["tasks"]
                            if not _response_path(project, item["task"]["task_id"]).exists()]
        if not request["tasks"]:
            request["status"] = "results_ready"
    return request


def get_project_status(project: Path) -> dict:
    """Return workspace progress and pending host identities without dispatching."""
    project = project.resolve(strict=True)
    manifest = read_json(project / "project.json")
    document_path = project / "work" / "document.json"
    counts = {}
    if document_path.exists():
        for unit in read_json(document_path)["units"]:
            status = unit["translation_status"]
            if status == "translated":
                status = f"qa_{unit['qa_status']}"
            counts[status] = counts.get(status, 0) + 1
    state_path = project / "state" / "state.json"
    status = read_json(state_path)["status"] if state_path.exists() else (
        "not_run" if document_path.exists() else "imported"
    )
    request = get_host_request(project)
    return {"project_id": manifest["project_id"], "status": status,
            "unit_counts": counts, "host_status": request["status"],
            "pending_task_ids": [item["task"]["task_id"] for item in request["tasks"]]}


def submit_host_result(project: Path, result: dict) -> dict:
    """Validate and store a result. advance/finish commits it through the core."""
    project = project.resolve(strict=True)
    if not isinstance(result, dict):
        raise ValueError("Host result must be a JSON object")
    response = _response_path(project, result.get("task_id"))
    if response.exists():
        if read_json(response) != result:
            raise ValueError("Host task already has a different result")
        return {"status": "result_stored", "task_id": result["task_id"]}
    request = read_json(_request_path(project))
    item = next((item for item in request["tasks"]
                 if item["task"]["task_id"] == result["task_id"]), None)
    if request["status"] != "awaiting_host" or item is None:
        raise ValueError("Host result does not belong to the pending request")
    assert_project_source_current(project)
    task = item["task"]
    _validate_auxiliary_task_current(project, task)
    if task["task_type"] == "translation":
        validate_translation_patch(project, result)
    elif task["task_type"] == "chapter_review":
        validate_review_evidence_current(project, result, task)
    elif task["task_type"] == "terminology":
        validate_terminology_result(project, result, task)
    elif task["task_type"] == "book_consistency":
        validate_book_consistency_result(project, result, task)
    elif task["task_type"] == "spotcheck":
        _, _, document = _load_project(project)
        validate_spotcheck_evidence_current(project, document, project / request["directory"], task, result)
    else:
        raise ValueError("Unknown host task type")
    write_json(response, result)
    return {"status": "result_stored", "task_id": task["task_id"]}


def advance_host_run(
    project: Path, *, capabilities: ProviderCapabilities,
    bilingual: bool = False, agent_model: str = DEFAULT_AGENT_MODEL,
    review_model: str = DEFAULT_AGENT_REVIEW_MODEL, reviewer_id: str = "host:chapter-reviewer",
) -> dict:
    """Run until the next Agent task or candidate output, then return immediately.

    Use the same capabilities/model settings throughout one pending run. All
    model tasks, including terminology and consistency, execute in the host.
    """
    project = project.resolve(strict=True)
    pending = get_host_request(project)
    resume_translation = resume_fix = None
    stop_after_terminology = False
    if pending["status"] in {"awaiting_host", "results_ready"}:
        if pending["operation"] != "run":
            raise ValueError("Finish the pending host spotcheck before running the project")
        assert_project_source_current(project)
        task = read_json(_request_path(project))["tasks"][0]["task"]
        _validate_auxiliary_task_current(project, task)
        if pending["status"] == "awaiting_host":
            return pending
        stop_after_terminology = task["task_type"] == "terminology"
        if task["task_type"] == "translation":
            if (task["context_capsule"].get("batch") or {}).get("review_issues"):
                resume_fix = task["task_id"]
            elif not (task["context_capsule"].get("batch") or {}).get("consistency_issues"):
                resume_translation = task["task_id"]
    executor = FileHostExecutor(project)
    try:
        outputs = run_project(
            project, capabilities=capabilities, auxiliary_executor=executor, bilingual=bilingual,
            translation_backend=1, agent_executor=executor, agent_model=agent_model,
            review_backend=1, review_executor=executor, reviewer_id=reviewer_id,
            review_model=review_model, resume_translation_task_id=resume_translation,
            resume_fix_task_id=resume_fix,
            stop_after_terminology=stop_after_terminology,
        )
    except HostTaskPending as pending_task:
        request = {"status": "awaiting_host", "operation": "run",
                   "tasks": [_dispatch(pending_task.task, pending_task.model)]}
        _write_run_state(project, "awaiting_host", host_task_id=pending_task.task["task_id"])
    else:
        request = {"status": "terminology_ready" if stop_after_terminology else "spotcheck_pending", "operation": "run", "tasks": [],
                   "outputs": [str(path) for path in outputs]}
    write_json(_request_path(project), request)
    return request


def start_host_spotcheck(
    project: Path, *, seed: int, reviewer_a_id: str, reviewer_b_id: str,
    model: str = DEFAULT_AGENT_SPOTCHECK_MODEL, new_run: bool = False,
) -> dict:
    """Export two tasks; reviewer IDs must identify two distinct host workers."""
    project = project.resolve(strict=True)
    if get_host_request(project)["status"] in {"awaiting_host", "results_ready"}:
        raise ValueError("A host request is already pending; resume it before starting another")
    if not reviewer_a_id.strip() or not reviewer_b_id.strip() or reviewer_a_id.casefold() == reviewer_b_id.casefold():
        raise ValueError("Spotcheck requires two distinct host workers")
    assert_project_source_current(project)
    _, _, document = _load_project(project)
    directory = create_spotcheck_round(project, document, seed, new_run=new_run)
    tasks = create_spotcheck_tasks(project, document, directory, reviewer_a_id, reviewer_b_id, model=model)
    for slot, task in tasks.items():
        write_json(directory / "tasks" / f"{slot}.json", task)
    request = {"status": "awaiting_host", "operation": "spotcheck",
               "directory": directory.relative_to(project).as_posix(),
               "tasks": [_dispatch(tasks[slot], model) for slot in REVIEWERS]}
    write_json(_request_path(project), request)
    return request


def finish_host_spotcheck(project: Path) -> dict:
    project = project.resolve(strict=True)
    pending = get_host_request(project)
    if pending["status"] not in {"awaiting_host", "results_ready"} or pending["operation"] != "spotcheck":
        raise ValueError("No host spotcheck is pending")
    assert_project_source_current(project)
    if pending["status"] == "awaiting_host":
        return pending
    request = read_json(_request_path(project))
    _, _, document = _load_project(project)
    directory = project / request["directory"]
    tasks = {item["task"]["reviewer_slot"]: item["task"] for item in request["tasks"]}
    evidence = {slot: read_json(_response_path(project, tasks[slot]["task_id"])) for slot in REVIEWERS}
    accept_spotcheck_evidence_pair(project, document, directory, tasks, evidence)
    result = finalize_spotcheck_round(project, document, directory)
    finished = {"status": "spotcheck_finished", "operation": "spotcheck", "tasks": [], "result": result}
    write_json(_request_path(project), finished)
    return finished
