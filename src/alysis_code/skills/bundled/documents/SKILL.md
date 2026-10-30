---
name: documents
description: Create or revise Word documents and formatted reports, preserving source evidence and existing document structure.
---

Use when: The task requests a DOCX deliverable, changes to an existing Word file, or a formatted report from source materials.

Do not use when: The user only wants a short answer in chat, a code change, a spreadsheet, or a slide deck.

Read supplied sources and the existing document before choosing its structure. Preserve the user's template, language, units, citations, and unrelated content. Separate measured findings from assumptions and proposals. Ask only for missing information that prevents a useful result.

Prefer `document_operation` for supported document actions; it uses the installed engine libraries even when the shell is containerized. The capability command examples are for a host terminal and may be unavailable inside a shell container. Call `capability_status` and inspect its Office capability. Use `document_operation` with action `describe` and format `docx` for supported operations. Inspect before editing and bind changes to the returned source/selection hashes. Write revisions to a new file unless overwriting was requested. If a requested feature is unsupported, choose an available document library or connector that preserves it; do not flatten the original or quietly remove features to fit a helper.

Use a hierarchy of real headings, readable tables and consistent typography appropriate to the document. A report's length and sections should follow its purpose, not a fixed template. Keep editable sources with requested deliverables; put temporary scripts and previews outside the deliverable folder.

Reopen the produced file and check the requested changes, key figures, source support and preservation requirements. If a renderer is available, render and inspect every page with `asset_view`; repair clipping, broken tables and pagination before delivery. Distinguish structural checks from rendered layout review, and disclose a missing renderer rather than claiming visual validation.

Deliver the actual file with a concise account of its contents and any material unchecked requirement. File existence, successful creation, and an agent's own assertion are not evidence of content accuracy.

Before delivery, call `artifact_check` on each final DOCX, XLSX, PPTX or PDF. It binds observed structural, calculation and rendering checks to that exact file version. Inspect every returned page image with `asset_view`; repair failed checks and recheck the changed file. These checks supplement source and task-specific validation; they do not prove the content is correct.
