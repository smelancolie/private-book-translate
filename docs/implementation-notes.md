# Implementation notes

## Reference snapshot

reference_repository: `https://github.com/SaberOnGo/public-domain-books-translation.git`
reference_branch: `main`
reference_commit_sha: `7af61d6a485033edeb5c08b2f7e55b763ea59d30`
reference_checked_at: `2026-09-27T22:48:34Z`

The private project `main` and the reference project `main` were refreshed before this round. The reference snapshot above is the checked-out `main` commit, not a repository-page estimate. I read `translation_unit_xliff_policy.md`, `adaptive_parallel_orchestration.md`, `ai_parallel_execution_guidance.md`, `bilingual_parallel_edition_policy.md`, and the persistent-unit, chapter QA, spot-check, defect-family, and bilingual alignment implementations/tests.

## Reuse and omissions

- Adopted as rules: persistent translation-unit identity, canonical target text, source/target block alignment, full chapter re-review after fixes, stratified spot-check evidence, and defect-family closure.
- Implemented locally: UUID unit IDs with source reconciliation; source and structural fingerprints; bounded translation/review batches; chapter-scoped consistency candidates; and aligned target/bilingual rendering. The formats still share one canonical document and QA pipeline.
- Not adopted: XLIFF 2.1 exchange, because this CLI has no exchange workflow; chapter-scoped compare-and-swap and independent parallel audit ownership, because this pipeline has no concurrent worker patch model; and adaptive runtime orchestration, because it would add a worker launcher absent from the private workflow. Provider calls remain sequential and bounded.
- Excluded by design: public-domain discovery, rights evidence, publication/release gates, public/private modes, private-use overlays, Digest, launcher, covers, and compatibility paths for those features. No release/publication changes were brought over.
- No reference implementation code is copied; the private project keeps its own model, state, CLI, provider adapter, Markdown/EPUB adapters, rendering, and QA evidence validation.

The Markdown adapter uses Pandoc's AST JSON interface to preserve parsed block and inline structure. EPUB handling edits only package metadata and XHTML members in the source ZIP; other members and compression settings are carried through, with `mimetype` normalized to the required first/stored entry. Content encryption/DRM is refused; standard font obfuscation entries and font bytes are carried through unchanged. Bilingual EPUB inserts translated blocks without copying IDs; table/list structures remain intact, and navigation labels stay a single translated TOC so its nesting and links stay intact.

## Phase 3 — EPUB vertical slice

- EPUB and Markdown now share the canonical units, glossary, translation provider, state, chapter QA, consistency QA, and alignment output.
- EPUB import follows the container, OPF manifest and spine, and EPUB 3 nav or EPUB 2 NCX navigation. It creates units for titles, headings, prose blocks, footnotes/endnotes, table cells, MathML preservation checks, captions, navigation labels, and `alt` / `title` / `aria-label` values. Inline markup is carried in protected format markers. Unsupported content encryption/DRM is refused; standard IDPF font obfuscation is copied through unchanged without decryption.
- Output preserves all original archive members; only translated XHTML and OPF title/language fields are serialized. Target-only and bilingual artifacts are built separately. Structural validation checks ZIP ordering, required package references, unique IDs, XML, and local file/fragment links. Structural validation also passes for standard font-obfuscation metadata without reading or decrypting font contents. The optional `epubcheck` executable was not installed in the implementation environment.
- Synthetic EPUB coverage includes multiple spine documents, nested TOC, internal chapter/note links, note semantics, table, image, SVG and CSS resources, inline emphasis, DRM rejection, target-only and bilingual output, validation, and resume.

## Phase 4 — Full QA migration

- Chapter QA remains a state-machine gate: a repair round is `FIXED_RECHECK_REQUIRED`; a distinct new full review with zero issues is needed for `PASS`. Book-level consistency checks enforce approved glossary terms and provider review, and target-hash checks invalidate stale QA.
- `translate spotcheck` writes a deterministic seeded sample from paragraph, table, figure, formula, and caption/note strata. Reference thresholds carried into the protocol are a 0.80 confidence target at an assumed 0.10 defect rate; paragraph sample budget is 120 per reviewer/round; tables and figures are full-scan at 80 or fewer, formulas at 100 or fewer, and captions/notes at 120 or fewer, otherwise 20 per stratum/round. Two independent reviewer slots record per-unit 0–100 scores; each average must be at least 92, its lowest score at least 88, and style debt zero.
- Final validation requires two consecutive PASS rounds in the active current run, each with a different seed; the default run plan allows four rounds. A reported family requires an exact full-book audit of its declared strata, mapped fix/exception records, rerun chapter/book QA, and a new-seed review round. Generated sample plans are recomputed during validation, and source/target hashes must still match their evidence.
- `run` leaves candidate output in the workspace so a human review can happen; `build` and `validate` enforce the completed current-run sampling gate. Reviewer independence is recorded by distinct reviewer IDs but cannot be authenticated by a local CLI.

## Phase 5 — Privacy and long-book verification

- Every new project workspace contains `.gitignore` (`*` plus an exception for `.gitignore`). When initialized under a Git work tree, creation checks a representative private state path with `git check-ignore` and fails before finalizing the workspace if Git would track it.
- CI runs the full unittest suite on Python 3.11 and 3.12 with Pandoc installed. A separate optional EPUBCheck job validates a minimal EPUB fixture; the ordinary synthetic EPUB fixtures are not sent through EPUBCheck because they intentionally use placeholder binary assets.
- `test_large_book_never_sends_full_book_to_provider` creates 100 chapters and 50,000 paragraph units with a local fake provider; it exercises initialization, translation, resume, bounded translation/review requests, consistency occurrence indexing, output build, and two seeded spot-check rounds.
