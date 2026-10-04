# Project rules

- This project is local-first and private by default. Never add publication, rights, release, Digest, launcher, or `private_use` overlay flows.
- Keep source files, workspace state, translations, QA records, and generated artifacts under ignored `projects/` directories.
- The only initial language pair is `en` → `zh-Hans`; language rules live in its language package, not in the format adapters or shared pipeline.
- EPUB and Markdown adapters produce the same canonical model and use the same translation, glossary, state, and QA code.
- Preserve input files and format structure. Reject encrypted content/DRM; preserve standard font obfuscation without decrypting it.
- Use provider adapters for network calls. Never put API keys in project files or send local files anywhere except the configured provider request.
- Keep changes small and run `python -m unittest discover -s tests -v` after implementation changes.

# Codex host integration

- Drive translation through `private_translate.host`: call `advance_host_run`, dispatch its returned tasks, submit worker JSON with `submit_host_result`, then advance again. Do not wait for a Codex worker inside a Python call.
- Query progress with `get_project_status`. Create workspaces with `core.project.create_project`, and finish with `core.pipeline.build_project` and `validate_project`; the package has no product CLI entry point.
- Dispatch translation and chapter review to distinct real Codex workers using `spawn_agent` or `followup_task`. Bind each task's logical `worker_id` to its actual worker; workers write only their result JSON under the ignored project, and the core applies results.
- The core's `gpt-6-luna-max` selection maps to the Codex `luna_worker` role (`gpt-6-luna`, reasoning `max`). Pass the actual model to the host API. Other model selections require a matching available host worker; surface unavailable selections instead of silently substituting a model or API backend.
- Run the two tasks from `start_host_spotcheck` on two distinct workers, submit both results, and call `finish_host_spotcheck`. Follow existing defect closure rules when findings exist; two consecutive passing rounds with different seeds are required before `build_project` and `validate_project`.
- On a later turn or a new Python process, read `get_host_request` and resume the existing task IDs. Keep provider/model/reviewer settings stable while a request is pending, and serialize core writes for one project. Provider terminology and book-consistency requests still use the configured provider adapter.
