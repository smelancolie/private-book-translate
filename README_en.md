# Private Long-Form Translation

[简体中文](README.md) | **English**

A translation project for EPUB and Markdown that prioritizes local files. The primary execution path is **Codex / host integration → Python core**, and the initial language pair is English → Simplified Chinese (`en` → `zh-Hans`). Source files, translation state, glossaries, QA records, and outputs are stored in separate `projects/<id>/` workspaces. Each project includes its own Git ignore rules; initialization inside a Git repository also verifies that private state is actually ignored.

Terminology proposals, translation, chapter review, book consistency review, and repairs in the primary host workflow are all performed by Codex Agents. The Python core does not construct or call an HTTP provider, and no separate provider API key needs to be configured; the host handles model requests. The internal API execution path continues to use the existing provider adapter, whose keys are read only from environment variables and are never written into the project.

## Installation and Execution

Requires Python 3.11+, Pandoc 3+ (for the Markdown AST), and lxml. Install the Python package:

```powershell
python -m pip install -e .
```

The package does not register a product CLI command. The Python package, core API, and `python -m unittest` test entry point remain available. EPUB validation always runs the built-in checks for ZIP structure, package, spine, resources, links, fragment IDs, and XML structure. If `epubcheck` is available on the system PATH, it is run as an additional check.

Use `python -m unittest discover -s tests -v` for local validation. A small long-book smoke test with 137 units remains enabled locally; the 50,100-unit large-book regression is automatically skipped and runs only in GitHub Actions. The existing `large-book-regression` job runs after changes to relevant core code or large-book tests are pushed to `main`, and also supports manual execution through `workflow_dispatch`.

After committing and pushing code that requires the large-book regression, confirm that GitHub has started the test. Once the other test jobs have completed, you may stop waiting and report each job's current status and the run URL. Leave the large-book test running on GitHub; a running test does not count as passed.

### Codex / Host Integration

The host drives the core directly through `private_translate.host`. When an Agent is needed, Python returns tasks to be executed without waiting for a model or host callback. After the host executes the tasks and submits JSON, it calls the core again to continue. Core exceptions propagate directly, and the host presents the errors.

The repository's `AGENTS.md` includes the Codex dispatch steps: execute returned tasks with real `spawn_agent` / `followup_task` workers, and bind each logical identity to its actual worker. The existing `gpt-6-luna-max` selection maps to the `luna_worker` role with `gpt-6-luna` + `max`; pass the actual model name to the host API.

```python
from pathlib import Path

from private_translate.core.project import create_project
from private_translate.core.pipeline import build_project, validate_project
from private_translate.core.providers import ProviderCapabilities
from private_translate.host import (
    advance_host_run, get_host_request, get_project_status, submit_host_result,
    start_host_spotcheck, finish_host_spotcheck,
)

capabilities = ProviderCapabilities("codex-host", "gpt-6-luna",
    max_context_tokens=8000, max_output_tokens=2000,
    supports_json_mode=True, supports_parallel_requests=True)
# Declare the task budgets here; adjust them to the host's actual capabilities.
project = create_project(Path("projects/inputs/book.epub"), Path("projects"))
status = get_project_status(project)
request = advance_host_run(project, capabilities=capabilities, bilingual=True,
                           agent_model="gpt-6-luna", review_model="gpt-6-luna")
# The Python call ends here; Codex dispatches real workers for request outside this call.
```

When `request["status"] == "awaiting_host"`, the host reads `task`, `model`, and `worker_id` from `tasks` and executes them with real host Agents. Terminology tasks return proposals according to `required_result_schema`; translation tasks return TranslationPatch; chapter reviews return ReviewEvidence; and book consistency tasks return issue evidence in the specified schema. Then call `submit_host_result(project, result)` to store the result validated by the core, followed by another call to `advance_host_run`. A `spotcheck_pending` response means candidate outputs have been built, but the two required spot-check rounds remain to be completed.

`start_host_spotcheck(project, seed=17, reviewer_a_id=worker_a_id, reviewer_b_id=worker_b_id, model=host_spotcheck_model)` returns two SpotcheckTasks. Bind the reviewer IDs to two real, independent host workers. The host executes each task with its specified model and submits each SpotcheckEvidence result separately. Once both results are available, `finish_host_spotcheck` runs the existing evidence acceptance and scoring workflow. After completing a second round with a different seed, call `build_project` and `validate_project`.

`get_project_status(project)` returns `project_id`, the actual execution `status`, `unit_counts`, the handoff `host_status`, and `pending_task_ids`. A newly initialized workspace is `imported`; querying status does not parse the input or dispatch Agents. Units whose translation is complete are summarized by states such as `qa_pending` and `qa_passed`; other units use their translation status.

Task handoffs are saved in `work/host/request.json`, and host results in `work/host/results/<task_id>.json`, both under the ignored project directory. After a Python process exits, use `get_host_request` to resume pending tasks; submitted results appear as `results_ready`. Resubmitting an identical result only confirms its storage and does not modify the translation again. A different result cannot overwrite an existing one. Subsequent advance/finish operations merge patches and record chapter QA and spot-check completion states.

Review tasks save their `pending` lifecycle state before saving the task that recovery scans can discover. Tasks with existing evidence are not dispatched again during recovery. If terminology proposals have been written but the handoff has not yet been acknowledged, recovery checks whether the complete candidate rows match the state produced by applying the saved proposals from the original task. Approval remains manual. Book consistency repairs first save a `pending` record in the existing patch archive, including the repair count and hashes of the book's target text before and after application, then write the canonical document and mark the record as `applied`. After an interruption, recovery accepts only these two explicit states, completes archiving and counting, and continues chapter and book re-review. Manual changes to the task's candidate rows, approved terminology, or other translations are still rejected.

Chapter PASS results and task evidence are reused only when the source, translation, reviewer and translator identities, model, rubric, and complete batch context match. Changes to relevant approved terms trigger review of the affected chapters. Book PASS results also check approved terminology and review configuration; a pending book review round must retain its original configuration. QA stays pending when a new chapter review begins and returns to passed only after a review with zero defects. Final builds, validation, and spot-check creation all check the glossary hash recorded in the book report. Approving, modifying, or revoking terms requires the necessary revisions and reviews first. The internal API path also compares its model, rules, and submitted context.

A pending run must retain the same capability budgets, models, and reviewer identity settings. The host serializes core writes for each project. If the source changes before submission, the core explicitly rejects the result. Normal translation and review repair recovery reuse the original TranslationTask ID. Old task results are explicitly rejected if terminology candidate rows are edited manually while waiting, or if translations or approved terminology change during a book consistency task. The host handles actual model calls; Python does not create a new model client or wait in the background.

After the core accepts the final batch of terminology proposals, it returns `terminology_ready`; body translation tasks have not yet been created. The host can let the user inspect and approve candidate terms before advancing. Unapproved proposals are still not locked into translation. Terminology tasks use `agent:terminology-owner`, which may be bound to an existing translator worker; the consistency reviewer remains independent of the translator. Book reviews collect occurrences across chapters by term and process them in batches. A review passes only after evidence for all tasks is complete. Issues are sent back to the original translator for repair, followed by complete chapter review and a new book review round before candidate outputs are built.

`create_project` also supports `.md` files. By default, it produces only the target-language version. `advance_host_run(..., bilingual=True)` produces both target-language and bilingual versions and saves this choice in the project. Output names are stable:

```text
book.zh-Hans.epub
book.en-zh-Hans.bilingual.epub
book.zh-Hans.md
book.en-zh-Hans.bilingual.md
```

`advance_host_run` can be called repeatedly. Completed units that have not changed are skipped. Saved tasks and chapter state are retained while host tasks remain unfinished. Chapter revisions trigger a new complete chapter review, and book consistency evidence is kept aligned with the final translation revision.

## Spot Checks and Final Validation

`advance_host_run` produces candidate outputs awaiting spot checks. Final builds and validation require evidence from two consecutive spot-check rounds in the current run. In each round, the host executes two real workers outside the Python call and submits their JSON through `submit_host_result`:

```python
request = start_host_spotcheck(project, seed=17,
    reviewer_a_id=worker_a_id, reviewer_b_id=worker_b_id, model="gpt-6-luna")
# After the host executes both tasks and submits the workers' JSON results:
round_result = finish_host_spotcheck(project)
# Repeat the handoff with a new seed=18; after the latest two rounds pass:
outputs = build_project(project)
validation = validate_project(project)
```

Each reviewer's `scores` must cover every unit assigned by their SpotcheckTask, with scores from 0–100. Each reviewer must have an average of at least 92 and a minimum individual score of at least 88; style debt must be 0, and the reviewer IDs must differ. Set `findings` to an empty array when there are no issues. Otherwise, provide `unit_id`, `family_id`, `category`, `description`, and `reusable_rule` according to the task schema. Each round uses a different seed. The default spot-check plan allows up to four rounds, and final validation requires the latest two rounds in the current run to pass.

When a defect family is found, the host uses the existing `core.execution.create_translation_task` and `apply_translation_patch` interfaces to have the original translator make revisions, then calls `advance_host_run` for chapter and book review. The host reads the current canonical document and submits closure records through `core.spotcheck.close_spotcheck_defect_families`. Records must cover the defect definition, the book-wide audit method, and the audit result for every unit in all declared strata (`clear` / `fixed` / `exception`). They must also include the target hashes before and after each repair with an explanation, or a justified reason for each exception. The core validates the records and saves `closure.json`; the next round then starts with a new seed. Fixing only the sampled unit cannot close a defect family.

When no handoff is pending, `advance_host_run` reparses the source and preserves state that still matches. If the source for a queued task changes, submission and advancement are explicitly rejected. After reparsing/translating and completing QA, use `start_host_spotcheck(..., new_run=True)` to create a new current spot-check run. Use the same parameter when only spot checks need to restart; old evidence remains in the QA directory.

Candidate outputs, sample lists, worker evidence, `reviews.json`, and `closure.json` all remain in the ignored local project workspace. The host binds reviewer identities, and real workers provide scores and findings. The validator checks that the supplied evidence matches the current project state.

## Workspace

```text
projects/<id>/
├── project.json
├── source/       # Original input copies; never overwritten
├── work/         # Canonical document and resume state
├── glossary/     # candidates.csv proposals, terms.csv locked terms, occurrences.json local index
├── qa/           # Chapter, book, spot checks, defect closure, alignment
├── state/        # Latest run state
└── output/       # Target-language and optional bilingual candidate/final outputs
```

On the first advance, repeated proper names and terms are discovered locally before translation, and host Agents generate proposals in `glossary/candidates.csv`. After a user manually changes an entry's `status` to `approved`, the next run locks it into `terms.csv`. Only `approved` entries enter the mandatory glossary; `proposed` and `rejected` entries do not affect translation. Batching in the primary host workflow uses explicitly supplied `ProviderCapabilities` and the existing language-package budgets, without relying on an HTTP provider client.

Native tasks estimate input tokens from the complete JSON, including rules, IDs, hashes, schemas, terminology, and context. They include the existing request overhead and reserve the configured output budget. Body translation, chapter review, book consistency, and repairs are split into smaller batches as needed; repair tasks carry only issues relevant to their batch. If the complete task for a single unit still exceeds the budget, an explicit error is raised and the task is not dispatched. Estimation uses the project's existing algorithm; the host still determines actual model token counts. Saved pending tasks retain their original IDs and content during recovery and are not rebatched.

Initial limitations: only `en` → `zh-Hans` is supported; there is no OCR, and image pixels, code, formula syntax, URLs, CSS, and JavaScript are not translated. EPUB 3 navigation and EPUB 2 NCX are supported. Unreadable encrypted EPUB content / DRM is explicitly rejected; standard EPUB font obfuscation is preserved unchanged without decryption. EPUB XHTML translations are written into the original DOM, preserving the original CSS and other ZIP resources; bilingual versions insert the target language by block. Markdown uses the Pandoc AST with Markdown header attributes enabled to preserve explicit heading anchors. The `epubcheck` executable was not detected on the current machine, so only built-in structural validation results can be reported; EPUBCheck cannot be claimed to have passed.
