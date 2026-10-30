# Desktop open-file inspection

The `open_panels` version 1 handshake feature negotiates three optional, read-only
host tools. The desktop binds them to the saved Results file selected when the
user submits a message, including messages waiting in its composer queue. File
selection is private host state: it does not add a message, viewing event, path or
document contents to the model prompt. The agent obtains context only by calling
a tool when the user asks about the open file.

| Action | Agent tool | Result |
| --- | --- | --- |
| `panels.list` | `open_panel_list` | Selected saved-file identity, or an explicit empty selection |
| `panels.read` | `open_panel_read` | Up to 6,000 text characters and `next_offset`; exact saved hash/revision and a verified project copy |
| `panels.screenshot` | `open_panel_screenshot` | Rendered DOCX/PDF/image pixels, page identity and pagination metadata |

Reading accepts `panel_id: "results"` and an optional character offset up to
2,000,000. Text, Markdown, CSV and validated DOCX supply readable text. XLSX supplies
cell values and formulas, with cached and uncalculated results marked; charts and
print layout are not inspected. Other
saved formats supply a project-relative inspection copy for normal file tools.
The response's `path_base: "workspace_root"` prevents a changed active working
directory from redirecting subsequent reads. Copies live in `.alysis/panel-inspection`.
This copy is not an edit target. Changes should use the desktop's explicit saved
revision flow.

Screenshots accept a 1-based `page` (maximum 200) and vertical pixel `offset`
(maximum 100,000). Images have one page. `next_offset` identifies the next slice
of a tall page. A screenshot renders the bound bytes in an isolated hidden
window; it never captures the current app UI. DOCX browser pagination can differ
from Word. Screenshot tools are exposed only to image-capable models, and image
bytes use ephemeral visual delivery rather than persisted tool text.

Every call rechecks conversation/project/account authority and the saved SHA-256.
Switching Results files does not redirect an admitted request; navigation,
cancellation, changed bytes or stale ownership refuse access. No readable file
selected at submission is reported explicitly. Older engines do not advertise
these tools. Browser tabs retain their separate embedded-browser tools; external
editor buffers and unsaved edits are outside this capability.
