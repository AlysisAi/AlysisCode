---
name: pdf
description: Read, create, or inspect PDF files when page content, layout, or extraction fidelity matters.
---

For text-led PDF creation, use `document_operation` with action `describe` and format `pdf`, then create from a workspace Markdown source. This uses the installed engine runtime even when shell commands run in a container. Retain the Markdown for revisions.

Use when: The user asks to process a PDF or deliver a PDF document.

Do not use when: A PDF is only mentioned, or the task is solely to change application code handling PDFs.

Check `capability_status` for available PDF operations. For supported document creation, write an editable Markdown source and use `document_operation` to create the PDF and page previews. The CLI command examples require a host terminal and may be unavailable inside shell containers. Reuse available libraries for other operations; do not handwrite a PDF generator or repeatedly probe missing converters.

For reading, extract text for search and inspect page images where tables, charts, scans or reading order affect meaning. Missing extracted text is not proof of an empty page. Preserve page references for factual claims, and distinguish OCR uncertainty from source facts.

For creation or revision, preserve source units, typography needs and the user's requested content. Keep originals unchanged unless authorized. Render and inspect every output page with `asset_view`, checking clipping, page breaks, tables, fonts and source fidelity. Recheck changed pages after repairs. A successful conversion is not a layout review.

Save final files at the requested destination and keep previews in scratch. Include the editable source when useful or requested. State unreadable input sections or unavailable checks explicitly.

Before delivery, call `artifact_check` on each final DOCX, XLSX, PPTX or PDF. It binds observed structural, calculation and rendering checks to that exact file version. Inspect every returned page image with `asset_view`; repair failed checks and recheck the changed file. These checks supplement source and task-specific validation; they do not prove the content is correct.
