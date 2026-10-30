# Local document runtime

Install the optional `documents` extra to make text-led PDF creation and review
available. The desktop managed runtime includes this extra and pins its wheels.
`capability_status.pdf` reports dependency presence and available native tool routes.
The standalone CLI also reports commands for its current interpreter. Neither
capability report authorizes execution or proves visual quality.

For an agent session, prefer the built-in `document_operation` tool. It runs the
bounded document libraries in the installed engine environment, even when shell
commands run inside a separate container. It preserves workspace boundaries,
read/write permission rules, sensitive-file checks and the no-overwrite policy.
It is hidden in read-only sessions. Start with `{"action":"describe","format":"pdf"}`,
then write the editable Markdown source and call
`{"action":"create","format":"pdf","source":"draft.md","output":"report.pdf"}`.

The CLI remains available in a host terminal with the engine's interpreter. Its
absolute interpreter path is not necessarily accessible inside a shell container:

```sh
<engine-python> -m alysis_code.documents create --source draft.md --output report.pdf --preview-dir scratch/review-1
<engine-python> -m alysis_code.documents render --source report.pdf --preview-dir scratch/review-2
```

The create command lays out headings, paragraphs, lists, tables, links and code
using ReportLab font metrics. It renders every page with PDFium, checks for text
outside page/content bounds, and returns JSON with page PNG paths. The command
stages its own temporary files and publishes only after validation. It preserves
Markdown and refuses an existing PDF unless `--overwrite` is explicitly supplied
for an authorized revision. Preview directories must be new. Rendering never
counts as visual approval: inspect every returned PNG with `asset_view` before
claiming layout review, and report any checks that could not be completed.

Paths must remain inside the chosen workspace, with no symlinks. Read-only inputs
may have hardlinks, including attachment snapshots; outputs still reject hardlinks.
The PDF helper downloads nothing, executes no subprocesses and never installs
dependencies. Native operations retain filesystem authorization; direct CLI commands
retain their shell authorization.
It supports at most 1 MiB of Markdown, 50 PDF pages, 50 MiB for PDF review, and
20-inch page dimensions. Unsupported images, font glyphs and complex scripts fail
explicitly. Raw HTML is literal text. This renderer does not replace image-led
design, Word conversion, CJK fonts or complex-script shaping.

The four unmodified Liberation Sans fonts are bundled with their SIL Open Font
License in `alysis_code/documents/fonts/LICENSE_LIBERATION`. Their coverage includes
Latin, Greek and Cyrillic. ReportLab and PDFium dependency licenses ship in the
managed runtime wheels. Keep font and dependency license files when packaging.

## Editable Office files

The same `documents` extra includes DOCX, XLSX and PPTX libraries. Inspect
`capability_status.office` for supported operations. In an agent session, use
`document_operation` with `action: describe` and the desired format, then `create`,
`inspect`, `edit` or `recalculate`. Creation and editing use a workspace JSON `spec`
file; operations infer the format from `source` when appropriate. `path_base` chooses
the workspace root or active working directory. PPTX image references remain relative
to the workspace root and receive separate sensitive-file read checks. PDF supports
creation from Markdown; use `artifact_check` for PDF review.

Equivalent host-terminal commands:

```sh
<engine-python> -m alysis_code.office describe --format xlsx
<engine-python> -m alysis_code.office inspect --source forecast.xlsx
<engine-python> -m alysis_code.office recalculate --source forecast.xlsx --output forecast-calculated.xlsx
<engine-python> -m alysis_code.office render --source forecast-calculated.xlsx --preview-dir scratch/forecast-review
```

Office creation and conservative editing work without LibreOffice. Real formula
calculation and Office previews require an installed LibreOffice executable on
macOS or Linux. Discovery checks `PATH` and standard macOS Applications folders;
`ALYSIS_OFFICE_SOFFICE` can select an absolute executable path. An explicit invalid
path does not silently fall back to another program. Capability discovery never
downloads software or starts the converter. The desktop's Python dependencies do
not themselves include LibreOffice. Windows Office conversion is currently
unavailable through this helper; editable-file creation remains separate.

Recalculation clears stale caches in a private input copy, uses a fresh converter
profile with macros and active/external content disabled, and copies only typed
formula results into a new copy of the original package. It preserves formulas,
styles and unrelated package parts. Metadata that cannot round-trip through the
editing helper (including print titles and custom properties) can still be preserved
by calculation. Ordinary cell formulas are supported; shared and array formula
structures are rejected. Chart caches and embedded content are preserved but are
not refreshed. Unsupported workbook structures fail rather than being flattened. Recalculate again after any input or formula changes.
Calculation errors and expected business totals need separate attention.

XLSX creation supports `page_setup` with orientation, page scaling and a bounded
print area. Scaling a wide table onto one page can make it unreadable. Use sensible
number formats, wrap notes, render every sheet and inspect the returned page images.
Office rendering retains images when page-bound checks fail so the agent can see
and repair the problem.

## Checks attached to final artifacts

The built-in `artifact_check` tool reads an exact DOCX, XLSX, PPTX or PDF version
and returns checks bound to its SHA-256. It checks structure, compares supported
spreadsheet formula caches with fresh calculation, and renders pages when the
converter is available. `expected_sha256` can reject an unexpectedly changed file.
Previews are stored under `Task reviews/` in the workspace.
Creating these review files observes the session's write scope and approval policy;
`artifact_check` is unavailable in read-only mode. PDF creation applies the same
checks to its preview directory before publishing any output.

The engine exposes observed receipts through `session.artifactChecks`. The desktop
Results panel attaches receipts only to matching document bytes and saves a checked
PDF preview for later viewing. An edited file needs another check. Legacy or
model-written claims are not promoted to observed receipts.

Passing structure or rendering checks does not certify content accuracy or visual
quality. Inspect every page with `asset_view`, reconcile key values with sources,
and recheck any repaired file. The receipt's `visualReview: not_performed` describes
the automated checker; merely opening an image cannot prove a visual review passed.
