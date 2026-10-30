# Office CLI

Use capability_status.office.command, then describe --format docx|xlsx|pptx for the exact JSON schema.
Inspect: <command> inspect --source "original.docx"
Edit: <command> edit --source "original.docx" --spec "edits.json" --output "revision.docx"
Create: <command> create --format docx --spec "request.json" --output "new.docx"
Substitute xlsx or pptx as required. Paths stay in the workspace. Every revision needs a new output name; no overwrite. Keep expected_source_sha256 and the exact selection hashes/locators returned by inspect. Reopen the produced file with inspect.

DOCX: bounded main-body paragraphs/tables and supported inline edits; not complete headers, footers, notes, drawings, or Word layout.
XLSX: inspect cells/formulas/tables and edit only supported structures. Formula expressions are not computed. Do not treat cached values as current results or claim recalculation.
PPTX: inspect slide/shape identifiers and use exact supported text/table/image operations. Unsupported complex edits fail; never flatten them silently.

No macros. Package validation, source hashes, and reopening prove structural checks, not arbitrary Office fidelity.
Only when capability_status.office.preview reports configured: render --source "revision.docx" --preview-dir "scratch/office-review-new"; inspect returned pages with asset_view.
No renderer searching or installation. If the task needs visual fidelity and no renderer/vision is available, state what could and could not be verified.
