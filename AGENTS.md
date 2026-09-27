# Project rules

- This project is local-first and private by default. Never add publication, rights, release, Digest, launcher, or `private_use` overlay flows.
- Keep source files, workspace state, translations, QA records, and generated artifacts under ignored `projects/` directories.
- The only initial language pair is `en` → `zh-Hans`; language rules live in its language package, not in the format adapters or shared pipeline.
- EPUB and Markdown adapters produce the same canonical model and use the same translation, glossary, state, and QA code.
- Preserve input files and format structure. Reject encrypted content/DRM; preserve standard font obfuscation without decrypting it.
- Use provider adapters for network calls. Never put API keys in project files or send local files anywhere except the configured provider request.
- Keep changes small and run `python -m unittest discover -s tests -v` after implementation changes.
