---
name: presentations
description: Create or edit PowerPoint decks with source-grounded content, editable elements, and rendered slide review.
---

Use when: The user requests a PPTX deck or edits to an existing presentation.

Do not use when: The user only wants an outline in chat or is asking to implement presentation software.

Determine the audience, purpose and constraints from the request and sources. Inspect an existing deck or template before editing. Preserve masters, branding, speaker notes and unrelated slides when required; use a capable library or connector if the bounded helper cannot preserve them.

Check `capability_status.office`, use `document_operation` with action `describe` and format `pptx`, then choose supported creation or editing operations. This native tool uses the installed engine libraries even when shell commands run in a separate container. Bind edits to the inspected source and selection hashes. Keep the deck editable, including text, tables and charts where practical; do not substitute screenshots for editable content unless requested or needed for a source visual.

Build a coherent argument with clear slide titles, meaningful visual hierarchy and readable text. Choose layouts for the actual content, avoiding repetitive decoration, dense paragraphs and tiny labels. Use editable tables or charts for comparisons across services or categories instead of repeating long metric-filled sentences. Ground factual claims and charts in sources, including units and dates. Keep citations usable without overwhelming the slide.

Render the deck when a renderer is available and inspect every slide with `asset_view`. Check overflow, overlap, alignment, contrast, labels and image quality, then revise and rerender affected slides. Character-count estimates cannot prove text fits with the selected fonts. If rendering is unavailable, perform structural checks and explicitly leave visual review unverified.

Deliver the actual PPTX and any requested exports, with a brief description and material limitations. Keep scratch images and authoring scripts outside the deliverable folder.

Before delivery, call `artifact_check` on each final DOCX, XLSX, PPTX or PDF. It binds observed structural, calculation and rendering checks to that exact file version. Inspect every returned page image with `asset_view`; repair failed checks and recheck the changed file. These checks supplement source and task-specific validation; they do not prove the content is correct.
