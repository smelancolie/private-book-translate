# Implementation notes

## Reference snapshot

Checked the public default branch `main` on GitHub on 2026-09-27. The repository page listed 117 commits and showed `219f452` (2026-07-13) as its latest commit. A direct `git ls-remote` attempt could not authenticate because Windows Schannel reported no credentials, so the exact live remote HEAD was not independently confirmed.

The reference `AGENTS.md` and the current `English-to-Simplified-Chinese` template were read. Its shared pipeline describes stable translation units, hashes, deterministic projections, and alignment. Its chapter gate requires a new zero-issue full review after any fix. Its random review policy uses separate paragraph/table/figure/formula/note strata, defect-family book-wide closure, a new seed after repairs, at least two independent reviewers, a 0.80 confidence target, and a default of two current-run PASS rounds. Review PASS requires each reviewer average >=92, lowest sample >=88, and no style debt.

## Reuse and omissions

- Reuse as rules: translation-unit hashes and resume invalidation, approved glossary entries, full chapter fix/recheck semantics, source-to-target alignment, stratified review, and defect-family closure.
- Rewritten here: canonical model, workspace/state, CLI, provider adapter, Markdown and EPUB adapters, output construction, and QA evidence validation. No reference code is copied.
- Excluded by design: public-domain discovery, rights evidence, publication/release gates, public/private modes, private-use overlays, Digest, launcher, covers, and compatibility paths for those features.

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
