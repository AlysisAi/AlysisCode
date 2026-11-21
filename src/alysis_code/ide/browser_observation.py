"""Compact, source-addressable views of Chromium DOMSnapshot data.

This is structural decoding, not model-written summarization. Keep the original
snapshot available to the caller: layout membership is not a visibility proof,
and clipped values/omitted nodes may matter to the task. The browser study uses
this view before considering a change to the shipped browser tool contract.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Mapping
from functools import cache
from typing import Any
from urllib.parse import urldefrag, urlsplit


def redact_form_secrets(snapshot):
    """Remove structurally identified credential values before retaining a DOM.

    CDP uses a shared string table. Scrub the actual value strings there as well
    as duplicate entries so raw source retrieval cannot recover the field value.
    HTML field types/autocomplete tokens are protocol semantics, not word guesses.
    This cannot conceal secrets a site displays without marking their fields.
    """
    if (
        not isinstance(snapshot, Mapping)
        or "documents" not in snapshot
        or "strings" not in snapshot
    ):
        return snapshot
    strings = snapshot["strings"]
    secrets = set()
    for document in snapshot["documents"]:
        nodes = document["nodes"]
        values = dict(
            zip(
                nodes.get("inputValue", {}).get("index", []),
                nodes.get("inputValue", {}).get("value", []),
                strict=True,
            )
        )
        for node, raw in enumerate(nodes["attributes"]):
            attrs = {strings[raw[n]].lower(): strings[raw[n + 1]] for n in range(0, len(raw), 2)}
            sensitive = attrs.get("type", "").lower() == "password" or any(
                token in {"current-password", "new-password", "one-time-code"}
                or token.startswith("cc-")
                for token in attrs.get("autocomplete", "").lower().split()
            )
            if sensitive:
                if node in values and values[node] >= 0:
                    secrets.add(strings[values[node]])
                secrets.add(attrs.get("value", ""))
    secrets.discard("")
    if not secrets:
        return snapshot
    # Do not replace substrings throughout the DOM: a one-character password
    # must not rewrite every URL, attribute name and unrelated sentence.
    return {
        **snapshot,
        "strings": ["[private field]" if value in secrets else value for value in strings],
    }


# Attributes that name a control for people; searched by a focused observation.
_LABEL_ATTRIBUTES = frozenset(
    {"aria-label", "title", "placeholder", "alt", "value", "name", "aria-placeholder"}
)
# How many nodes around each match a focused view keeps (the match's control and its siblings).
_FOCUS_REGION_NODES = 40
# How many main-document nodes on each side of an element a view ``around`` it keeps. Snapshot
# nodes are in document order, so these are the content just before and after it on the page.
_AROUND_NODES = 120


# How much of an element's combined text a focused observation compares, so a label split by
# inline markup ("Sort <b>by</b> price") still matches; longer passages match by their own text.
_JOINED_TEXT_CHARS = 500
_SPACES = re.compile(r"\s+")


@cache
def _fold_tables() -> tuple[dict[int, None], dict[int, str | None]]:
    """From the Unicode database, for every script: format characters to drop (soft hyphens,
    zero-width spaces, direction marks); and, to compare without diacritics, nonspacing marks to
    drop and letters with a mark Unicode does not decompose (a stroke or bar) mapped to their
    base letter by the letter's Unicode name ("... LETTER D WITH STROKE" to "... LETTER D")."""
    formats: dict[int, None] = {}
    plain: dict[int, str | None] = {}
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        category = unicodedata.category(char)
        if category == "Cf":
            formats[code] = None
        elif category == "Mn":
            plain[code] = None
        elif category in ("Ll", "Lu") and not unicodedata.decomposition(char):
            base, _, mark = unicodedata.name(char, "").partition(" WITH ")
            if mark:
                try:
                    plain[code] = unicodedata.lookup(base)
                except KeyError:
                    pass
    return formats, plain


def _fold(text: str) -> str:
    """Text as a focused observation compares it, the same way in every script: compatibility
    forms unified (full-width letters, ligatures), case folded, invisible format characters
    removed, and each run of whitespace (non-breaking spaces too) one space."""
    if not text.isascii():
        text = unicodedata.normalize("NFKC", text).translate(_fold_tables()[0])
    return _SPACES.sub(" ", text.casefold())


def _fold_marks(text: str) -> str:
    """``_fold`` without accents or other diacritics: for a phrase typed without them, or text
    whose capitals drop them."""
    text = _fold(text)
    if text.isascii():
        return text
    return unicodedata.normalize(
        "NFC", unicodedata.normalize("NFD", text).translate(_fold_tables()[1])
    )


def _focus_matches(nodes, parents, types, string, phrase: str, shown: set[int]) -> list[int]:
    """Elements the page shows whose text or label contains ``phrase``, in document order.

    Text is compared as ``_fold`` gives it and, only when nothing matches that way, without
    diacritics too, so an exact spelling on the page wins over a looser one. A match is the
    innermost element whose text contains the phrase, even when inline markup splits it, or an
    element whose label attribute contains it.
    """
    children: dict[int, list[int]] = {}
    for index, parent in enumerate(parents):
        children.setdefault(parent, []).append(index)
    for fold in (_fold, _fold_marks):
        needle = fold(phrase).strip()
        if not needle:
            continue
        matches: set[int] = set()
        containing: set[int] = set()
        joined: dict[int, str | None] = {}
        # Children follow their parent in document order, so in reverse they come first.
        for index in range(len(types) - 1, -1, -1):
            if index not in shown:
                continue
            if types[index] == 3:
                value = fold(string(nodes["nodeValue"][index]))
                joined[index] = value if len(value) <= _JOINED_TEXT_CHARS else None
                if needle in value:
                    containing.add(index)
                    matches.add(parents[index] if parents[index] >= 0 else index)
            elif types[index] == 1:
                parts = [joined.get(child, "") for child in children.get(index, ())]
                value = None if None in parts else _SPACES.sub(" ", "".join(parts))
                joined[index] = (
                    value if value is not None and len(value) <= _JOINED_TEXT_CHARS else None
                )
                if joined[index] is not None and needle in joined[index]:
                    containing.add(index)
                    if not any(child in containing for child in children.get(index, ())):
                        matches.add(index)
                attributes = nodes["attributes"][index]
                for i in range(0, len(attributes), 2):
                    if string(attributes[i]).lower() in _LABEL_ATTRIBUTES and needle in fold(
                        string(attributes[i + 1])
                    ):
                        matches.add(index)
                        break
        if matches:
            return sorted(matches)
    return []


def _focus_region(parents, matches: list[int], limit: int = _FOCUS_REGION_NODES) -> set[int]:
    """Nodes around each match: its parent's subtree, so a checkbox next to its label or the
    button around a text are included, bounded per match."""
    children: dict[int, list[int]] = {}
    for index, parent in enumerate(parents):
        children.setdefault(parent, []).append(index)
    region: set[int] = set()
    for match in matches:
        top = parents[match] if parents[match] >= 0 else match
        stack, kept = [top], 0
        while stack and kept < limit:
            node = stack.pop()
            region.add(node)
            kept += 1
            stack.extend(reversed(children.get(node, [])))
        region.add(match)
    return region


def compact_dom_snapshot(
    snapshot: Mapping[str, Any],
    *,
    max_chars: int = 16000,
    field_chars: int = 320,
    focus: str | None = None,
    around: int | None = None,
    around_window: int | None = None,
    focus_region: int | None = None,
    _matches: dict[int, list[int]] | None = None,
) -> dict[str, Any]:
    """Decode layout nodes and their ancestors, with source document/node indexes.

    Selectors use actual element-sibling positions, including siblings omitted
    from the compact view. Only ordinary main-document element ancestry gets a
    selector; frames, shadow roots and pseudo-elements require another surface.
    The caller must label this untrusted page data and retain the raw snapshot.

    With ``focus``, only elements whose text or label contains that phrase (and the controls
    around them) are kept, so controls far down a large page can be reached within the same
    budget. Matching works the same way in every language: it ignores case, spacing and invisible
    characters and, when nothing matches exactly, accents and other diacritics (``_fold``). Each
    match keeps up to ``focus_region`` nodes around it; by default as many as let every match fit
    the budget.

    With ``around`` (a main-document node index), only that element and the page content just
    before and after it are kept: what a reviewer needs to judge one action on a large page.
    ``around_window`` nodes are kept on each side; by default as many as fit the budget whole,
    up to ``_AROUND_NODES``, so the element is never cut off by the content before it.
    """
    if max_chars < 1000 or field_chars < 16:
        raise ValueError("Observation limits are too small.")
    if focus is not None and not focus.strip():
        raise ValueError("A focused observation needs a phrase.")
    if focus is not None and around is not None:
        raise ValueError("Observe around one element or by one phrase, not both.")
    if focus is not None and focus_region is None:
        # Many matches (a word in the menu, the cards and the filters) must not push the later
        # ones, often the control that was asked for, past the budget: keep less around each.
        region = _FOCUS_REGION_NODES
        matches: dict[int, list[int]] = {}  # found once, whatever the region
        while True:
            view = compact_dom_snapshot(
                snapshot,
                max_chars=max_chars,
                field_chars=field_chars,
                focus=focus,
                focus_region=region,
                _matches=matches,
            )
            if region <= 2 or not any(document["truncated"] for document in view["documents"]):
                return view
            region //= 2
    strings = snapshot["strings"]
    documents = snapshot["documents"]
    if around is not None:
        if focus is not None:
            raise ValueError("Observe around one element or by one phrase, not both.")
        if (
            type(around) is not int
            or not documents
            or not 0 <= around < len(documents[0]["nodes"]["parentIndex"])
        ):
            raise ValueError("That element is not in the main document.")
        if around_window is None:
            window = _AROUND_NODES
            while True:
                view = compact_dom_snapshot(
                    snapshot,
                    max_chars=max_chars,
                    field_chars=field_chars,
                    around=around,
                    around_window=window,
                )
                if window <= 4 or not (view["documents"] and view["documents"][0]["truncated"]):
                    return view
                window //= 2
        if type(around_window) is not int or around_window < 0:
            raise ValueError("The window around an element is a node count.")

    def string(index: int) -> str:
        return "" if index == -1 else strings[index]

    def sparse(values: Mapping[str, Any]) -> dict[int, Any]:
        return dict(zip(values.get("index", []), values.get("value", []), strict=True))

    output: dict[str, Any] = {
        "documents": [],
        "clipped": False,
        "omitted_documents": len(documents),
    }
    if focus is not None:
        output["focus"] = {"find": focus.strip(), "matches": 0}
    if around is not None:
        output["around"] = around

    def clip(value: str, record: dict[str, Any]) -> str:
        if len(value) > field_chars:
            record["clipped"] = True
            output["clipped"] = True
            return value[:field_chars]
        return value

    def fits() -> bool:
        # Research snapshots are already byte-bounded by ManagedBrowserService.
        # Count the complete JSON so metadata and frame boundaries count too.
        return len(json.dumps(output, ensure_ascii=False)) <= max_chars

    for doc_index, doc in enumerate(documents):
        nodes = doc["nodes"]
        parents = nodes["parentIndex"]
        types = nodes["nodeType"]
        rendered = set(doc["layout"]["nodeIndex"])
        included = set(rendered)
        for index in rendered:
            parent = parents[index]
            while parent >= 0 and parent not in included:
                included.add(parent)
                parent = parents[parent]
        # Native select menus generally give OPTION/OPTGROUP (and their text)
        # no layout boxes. Include descendants of rendered selects so the model
        # can choose a real observed option, including duplicate labels/values.
        select_descendants = set()
        for index, parent in enumerate(parents):
            if parent in select_descendants or (
                parent in rendered and string(nodes["nodeName"][parent]) == "SELECT"
            ):
                select_descendants.add(index)
        included.update(select_descendants)
        if focus is not None or around is not None:
            if focus is not None:
                if _matches is not None and doc_index in _matches:
                    found = _matches[doc_index]
                else:
                    found = _focus_matches(nodes, parents, types, string, focus, included)
                    if _matches is not None:
                        _matches[doc_index] = found
                region = _focus_region(parents, found, focus_region)
                output["focus"]["matches"] += len(found)
            else:
                region = (
                    set(range(max(0, around - around_window), around + around_window + 1))
                    if doc_index == 0
                    else set()
                )
            focused = {index for index in region if index in included}
            for index in list(focused):
                parent = parents[index]
                while parent >= 0 and parent not in focused:
                    focused.add(parent)
                    parent = parents[parent]
            included = focused
        values = sparse(nodes.get("inputValue", {})) | sparse(nodes.get("textValue", {}))
        checked = set(nodes.get("inputChecked", {}).get("index", []))
        selected = set(nodes.get("optionSelected", {}).get("index", []))
        pseudo = set(nodes.get("pseudoType", {}).get("index", []))
        shadow = set(nodes.get("shadowRootType", {}).get("index", []))
        selectors: dict[int, str] = {}
        sibling_counts: dict[int, int] = {}
        result: dict[str, Any] = {
            "document": doc_index,
            "source_nodes": len(types),
            "nodes": [],
            "omitted_nodes": len(types),
            "truncated": False,
        }
        result["url"] = clip(string(doc["documentURL"]), result)
        result["title"] = clip(string(doc["title"]), result)
        output["documents"].append(result)
        output["omitted_documents"] -= 1
        if not fits():
            output["documents"].pop()
            output["omitted_documents"] += 1
            output["clipped"] = True
            break
        for index, node_type in enumerate(types):
            parent = parents[index]
            if node_type == 1 and index not in pseudo:
                sibling_counts[parent] = sibling_counts.get(parent, 0) + 1
                if doc_index == 0 and index not in shadow:
                    if parent >= 0 and types[parent] == 9:
                        selectors[index] = ":root"
                    elif parent in selectors:
                        selectors[index] = (
                            f"{selectors[parent]} > :nth-child({sibling_counts[parent]})"
                        )
            if index not in included:
                continue
            record: dict[str, Any] = {"node": index, "parent": parent}

            record["name"] = string(nodes["nodeName"][index])
            text = string(nodes["nodeValue"][index])
            if text:
                record["text"] = clip(text, record)
            if index in selectors:
                record["selector"] = selectors[index]
            attributes = nodes["attributes"][index]
            if attributes:
                record["attributes"] = {
                    string(attributes[i]): clip(string(attributes[i + 1]), record)
                    for i in range(0, len(attributes), 2)
                }
            if index in values:
                record["value"] = clip(string(values[index]), record)
            if index in checked:
                record["checked"] = True
            if index in selected:
                record["selected"] = True
            result["nodes"].append(record)
            result["omitted_nodes"] -= 1
            if not fits():
                result["nodes"].pop()
                result["omitted_nodes"] += 1
                output["clipped"] = True
                result["truncated"] = True
                break
    return output


# The page view: Chromium's accessibility tree as text, one line per element a person perceives,
# with references that name elements of the main document. It reads like the page (roles, names,
# states, text and link addresses) at a fraction of the size of DOM nodes, so far more of a busy
# page fits. The DOM snapshot of the same observation supplies what the accessibility tree does
# not: which nodes can be acted on (a selector), their identity, and form values as captured
# after ``redact_form_secrets``.

# About 20 KB of page view per observation; the rest stays readable from the retained source.
PAGE_VIEW_CHARS = 20_000
_PAGE_VIEW_NAME_CHARS = 300
_PAGE_VIEW_VALUE_CHARS = 300
_PAGE_VIEW_URL_CHARS = 300
# Deeper lines keep this indentation; nesting below it adds no reading value.
_PAGE_VIEW_INDENT_LEVELS = 20
# Deeper accessibility trees are refused rather than rendered with missing parts (and rendering
# stays well within Python's recursion limit).
_PAGE_VIEW_DEPTH = 200
# Lines kept around each match of a focused (find) page view, and of a match's own subtree.
_PAGE_VIEW_CONTEXT = (8, 2, 0)
_PAGE_VIEW_MATCH_LINES = 40
_PRIVATE_FIELD = "[private field]"

# Chromium accessibility roles (protocol vocabulary, the same for every site and language).
_SKIPPED_ROLES = frozenset({"inlinetextbox", "listmarker"})
_TEXT_ROLES = frozenset({"statictext"})
_BREAK_ROLES = frozenset({"linebreak"})
_ROOT_ROLES = frozenset({"rootwebarea", "webarea"})
# Unnamed inline formatting: its text belongs to the surrounding text.
_INLINE_ROLES = frozenset(
    {
        "abbr",
        "code",
        "deletion",
        "emphasis",
        "insertion",
        "mark",
        "strong",
        "subscript",
        "superscript",
        "time",
    }
)
# Unnamed containers without meaning of their own: flattened, their content kept in place.
_WRAPPER_ROLES = frozenset(
    {
        "generic",
        "group",
        "labeltext",
        "layouttable",
        "layouttablecell",
        "layouttablerow",
        "menulistpopup",
        "none",
        "presentation",
        "rowgroup",
        "section",
        "unknown",
    }
)
_FIELD_ROLES = frozenset({"searchbox", "slider", "spinbutton", "textbox"})
_CHOICE_ROLES = frozenset({"combobox"})
_OPTION_ROLES = frozenset({"menulistoption", "option"})
# Native text controls show their value; their accessibility children are the editor inside.
_NATIVE_FIELDS = frozenset({"INPUT", "TEXTAREA"})
_HIDDEN_URL_SCHEMES = frozenset({"javascript", "data"})


class PageViewUnavailable(ValueError):
    """The accessibility tree cannot give a faithful page view of this observation.

    ``reason`` is a fixed sentence without page data, for the observation and diagnostics.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _secret_field(attributes: Mapping[str, str]) -> bool:
    """A credential field by HTML semantics: the same rule as ``redact_form_secrets``."""
    return attributes.get("type", "").lower() == "password" or any(
        token in {"current-password", "new-password", "one-time-code"} or token.startswith("cc-")
        for token in attributes.get("autocomplete", "").lower().split()
    )


def _squash(text: str) -> str:
    return "".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _PageDocument:
    """The main document of a DOM snapshot, as the page view and its references need it."""

    def __init__(self, snapshot: Mapping[str, Any]):
        documents = snapshot.get("documents") if isinstance(snapshot, Mapping) else None
        if not documents:
            raise PageViewUnavailable("the page has no main document")
        self.strings = snapshot["strings"]
        self.document = documents[0]
        nodes = self.nodes = self.document["nodes"]
        self.parents = nodes["parentIndex"]
        self.types = nodes["nodeType"]
        self.backend = nodes.get("backendNodeId") or []
        self.pseudo = set(nodes.get("pseudoType", {}).get("index", []))
        self.shadow = set(nodes.get("shadowRootType", {}).get("index", []))
        self.index_of = {node: index for index, node in enumerate(self.backend)}
        # Actionable elements: those compact_dom_snapshot gives a selector, by the same rule.
        self.position = [0] * len(self.types)
        self.selectable = [False] * len(self.types)
        counts: dict[int, int] = {}
        for index, parent in enumerate(self.parents):
            if self.types[index] != 1 or index in self.pseudo:
                continue
            counts[parent] = counts.get(parent, 0) + 1
            self.position[index] = counts[parent]
            self.selectable[index] = (
                index not in self.shadow
                and parent >= 0
                and (self.types[parent] == 9 or self.selectable[parent])
            )

    def string(self, index) -> str:
        return self.strings[index] if type(index) is int and 0 <= index < len(self.strings) else ""

    def name(self, index: int) -> str:
        return self.string(self.nodes["nodeName"][index])

    def attributes(self, index: int) -> dict[str, str]:
        raw = self.nodes["attributes"][index]
        return {
            self.string(raw[i]).lower(): self.string(raw[i + 1]) for i in range(0, len(raw) - 1, 2)
        }

    def identity(self) -> dict[str, Any]:
        """The document itself: a navigation, even to the same address, makes a new one."""
        return {
            "backend_node_id": self.backend[0] if self.backend else None,
            "url": self.string(self.document.get("documentURL")),
        }

    def selector(self, index: int) -> str | None:
        if not self.selectable[index]:
            return None
        parts = []
        while self.types[self.parents[index]] != 9:
            parts.append(f":nth-child({self.position[index]})")
            index = self.parents[index]
        return " > ".join([":root", *reversed(parts)])


def accessibility_page_view(
    tree: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    max_chars: int = PAGE_VIEW_CHARS,
    find: str | None = None,
    identity_refs: bool = False,
) -> dict[str, Any]:
    """Render Chromium's full accessibility tree as an indented text view of the page.

    One line per meaningful element: ``- role "name" [states] [ref=eN]: text or value -> link``.
    Unnamed generic wrappers are flattened and their text kept in place; inline text is joined
    as written (every script alike); a field shows its value, and a credential field (by the
    ``redact_form_secrets`` rule) only ``[private field]``. ``eN`` refers to an element of the
    main document that an action can address (it has a ``compact_dom_snapshot`` selector),
    through its ``backendDOMNodeId`` in ``snapshot`` (the DOM snapshot of the same observation).

    With ``identity_refs`` (for a browser that addresses an element by its observed identity in
    any frame), elements no selector reaches get refs too: in open shadow roots of the main
    document, and in the other frames that the tree's ``frames`` describe, same- or
    out-of-process. Those refs are in ``frame_refs``: ref -> (frame id, or None for the main
    document; that frame's document loader; backend node id; the element's tag in the main
    document or its role in a frame). Frames the main tree does not hold are shown after it,
    each under a ``- frame "title" -> address`` line; a frame the browser could no longer read
    (``unavailable`` in ``frames``) as ``- frame [unavailable] -> address``.

    ``previous`` is an earlier result for the same browser page: elements of the same document
    keep their refs, and new refs continue its numbering, so a ref never names two elements.

    The view is cut at a line boundary within about ``max_chars`` with a final line saying what
    was left out; ``full_text`` keeps every line. With ``find``, only lines whose name, text or
    value contains the phrase (compared as ``compact_dom_snapshot``'s focus does) are shown, with
    their ancestors and nearby lines.

    Raises ``PageViewUnavailable`` when the tree is missing, cut short or not of this document.
    """
    if max_chars < 1000:
        raise ValueError("Observation limits are too small.")
    if find is not None and not find.strip():
        raise ValueError("A focused observation needs a phrase.")
    nodes = tree.get("nodes") if isinstance(tree, Mapping) else None
    if not isinstance(nodes, list) or not nodes:
        raise PageViewUnavailable("the browser did not return an accessibility tree for this page")
    by_id: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("nodeId"), str):
            raise PageViewUnavailable("the browser returned an invalid accessibility tree")
        by_id[node["nodeId"]] = node
    root = next((node for node in nodes if not node.get("parentId")), None)
    if root is None:
        raise PageViewUnavailable("the browser returned an invalid accessibility tree")
    page = _PageDocument(snapshot)
    main_frame = root.get("frameId")
    document_frame = page.string(page.document.get("frameId"))
    if main_frame and document_frame and main_frame != document_frame:
        raise PageViewUnavailable("the accessibility tree is not of this page's main document")
    page_url = page.string(page.document.get("documentURL"))
    values = dict(
        zip(
            page.nodes.get("inputValue", {}).get("index", []),
            page.nodes.get("inputValue", {}).get("value", []),
            strict=True,
        )
    ) | dict(
        zip(
            page.nodes.get("textValue", {}).get("index", []),
            page.nodes.get("textValue", {}).get("value", []),
            strict=True,
        )
    )

    def scalar(field) -> Any:
        return field.get("value") if isinstance(field, Mapping) else None

    def text_of(field) -> str:
        value = scalar(field)
        return value if isinstance(value, str) else "" if value is None else str(value)

    def properties(node) -> dict[str, Any]:
        return {
            item["name"]: scalar(item.get("value"))
            for item in node.get("properties") or []
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }

    # The other frames an element can be addressed in: id -> description (address, origin and
    # the loader of the document it shows).
    frames = {
        item["id"]: item
        for item in (tree.get("frames") or [] if identity_refs else [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]
    }
    # Frames whose content the main tree holds (same-process ones, in place).
    inline_frames: set[str] = set()

    def mapped(node, where: str | None) -> int | None:
        """The main-document DOM node of an accessibility node, when it has one."""
        backend = node.get("backendDOMNodeId")
        return page.index_of.get(backend) if where is None and type(backend) is int else None

    def reach_of(node, where: str | None, index: int | None, kind: str):
        """How an action reaches this element: its main-document node (a selector reaches it),
        or with ``identity_refs`` its identity in its frame; None when it cannot be reached."""
        if where is None:
            if index is None or page.types[index] != 1 or index in page.pseudo:
                return None
            if page.selectable[index]:
                return index
            return (None, None, page.backend[index], page.name(index)) if identity_refs else None
        backend = node.get("backendDOMNodeId")
        if where not in frames or type(backend) is not int or backend <= 0:
            return None
        return (where, str(frames[where].get("loader") or "") or None, backend, kind)

    # What a credential field holds in the accessibility tree (Chromium shows a password as
    # bullets, but a one-time code or card number as typed). Like the DOM redaction, the same
    # whole strings elsewhere on the page are hidden too, never substrings.
    secrets: set[str] = set()
    for node in nodes:
        index = mapped(node, None)
        if index is not None and _secret_field(page.attributes(index)):
            secret = " ".join(text_of(node.get("value")).split())
            if secret:
                secrets.add(secret)

    def scrub(text: str) -> str:
        return _PRIVATE_FIELD if secrets and " ".join(text.split()) in secrets else text

    known: dict[int, int] = {}
    known_identities: dict[tuple, int] = {}
    next_ref = 1
    if previous:
        next_ref = int(previous["next_ref"])
        if previous["document"] == page.identity():
            known = {backend: ref for ref, (backend, _) in previous["refs"].items()}
            known_identities = {
                tuple(item[:3]): ref for ref, item in (previous.get("frame_refs") or {}).items()
            }
    refs: dict[int, tuple[int, str]] = {}
    frame_refs: dict[int, tuple[str | None, str | None, int, str]] = {}
    assigned: dict[Any, int] = {}

    def ref_for(where) -> int | None:
        """The ref of an element's address (see ``reach_of``)."""
        nonlocal next_ref
        if isinstance(where, tuple):
            # Its frame's document is part of the key: a frame that loads another document
            # numbers its elements anew, and a ref never names an element of another document.
            key = where[:3]
            ref = assigned.get(key)
            if ref is None:
                ref = known_identities.get(key)
                if ref is None:
                    ref, next_ref = next_ref, next_ref + 1
                assigned[key] = ref
                frame_refs[ref] = where
            return ref
        if where is None or not page.selectable[where]:
            return None
        backend = page.backend[where]
        ref = assigned.get(backend)
        if ref is None:
            ref = known.get(backend)
            if ref is None:
                ref, next_ref = next_ref, next_ref + 1
            assigned[backend] = ref
            refs[ref] = (backend, page.name(where))
        return ref

    def children(node, depth: int, owner, where: str | None) -> list:
        parts: list = []
        for child_id in node.get("childIds") or []:
            child = by_id.get(child_id)
            if child is None:
                raise PageViewUnavailable("the browser's accessibility tree was cut short")
            parts.extend(collect(child, depth + 1, owner, where))
        return parts

    def options(node, depth: int, where: str | None) -> list:
        """A dropdown's options, wherever they sit among its accessibility descendants."""
        if depth > _PAGE_VIEW_DEPTH:
            raise PageViewUnavailable("the page is nested too deeply for the page view")
        found: list = []
        for child_id in node.get("childIds") or []:
            child = by_id.get(child_id)
            if child is None:
                raise PageViewUnavailable("the browser's accessibility tree was cut short")
            if not child.get("ignored") and text_of(child.get("role")).casefold() in _OPTION_ROLES:
                found.extend(collect(child, depth + 1, None, where))
            else:
                found.extend(options(child, depth + 1, where))
        return found

    def collect(node, depth: int, owner, where: str | None) -> list:
        """The parts one accessibility node contributes: elements, text pieces and breaks.
        ``where``: the frame whose document holds the node (None: the main document)."""
        if depth > _PAGE_VIEW_DEPTH:
            raise PageViewUnavailable("the page is nested too deeply for the page view")
        frame = node.get("frameId")
        if frame and main_frame and frame != main_frame:
            # Another frame's document: readable, and addressed by identity only (if at all).
            where = frame
            inline_frames.add(frame)
        if node.get("ignored"):
            return children(node, depth, owner, where)
        role = text_of(node.get("role"))
        kind = role.casefold()
        if kind in _SKIPPED_ROLES:
            return []
        if kind in _TEXT_ROLES:
            text = scrub(text_of(node.get("name")))
            return [(text, owner)] if text.strip() else []
        if kind in _BREAK_ROLES:
            return [None]
        if kind in _ROOT_ROLES:
            return children(node, depth, owner, where)
        name = scrub(" ".join(text_of(node.get("name")).split()))
        index = mapped(node, where)
        reach = reach_of(node, where, index, role)
        props = properties(node)
        parent = by_id.get(node.get("parentId") or "")
        editing_root = bool(props.get("editable")) and not (
            parent is not None and properties(parent).get("editable")
        )
        if not name and not editing_root and kind in _INLINE_ROLES:
            return children(node, depth, owner, where)
        if not name and not editing_root and kind in _WRAPPER_ROLES:
            # Its text takes this element's ref, so text-only controls stay addressable.
            return [None, *children(node, depth, reach, where), None]
        secret = index is not None and _secret_field(page.attributes(index))
        native = index is not None and page.name(index).upper() in _NATIVE_FIELDS
        value = None
        raw = " ".join(text_of(node.get("value")).split())
        if secret:
            dom_value = page.string(values.get(index, -1))
            value = _PRIVATE_FIELD if raw or dom_value else None
        elif "value" in node and index is not None and not (editing_root and not native):
            # Only what the browser reports as a value (a checkbox's form value is not one).
            # Values as the DOM snapshot captured them (after its credential redaction).
            value = " ".join(page.string(values[index]).split()) if index in values else scrub(raw)
            value = _clip(value, _PAGE_VIEW_VALUE_CHARS) if value else None
        if secret or native or (index is None and kind in _FIELD_ROLES):
            parts: list = []
        elif kind in _CHOICE_ROLES:
            parts = options(node, depth, where)
        else:
            parts = children(node, depth, None, where)
        states = []
        for key in ("checked", "pressed"):
            state = str(props.get(key, "")).casefold()
            if state in {"true", "mixed"}:
                states.append(key if state == "true" else f"{key}=mixed")
        for key in ("selected", "expanded", "disabled", "required"):
            if props.get(key) is True:
                states.append(key)
        if str(props.get("invalid", "")).casefold() == "true":
            states.append("invalid")
        if kind == "heading" and type(props.get("level")) is int:
            states.append(f"level={props['level']}")
        if editing_root and not native and kind not in _FIELD_ROLES:
            states.append("editable")
        url = None
        if kind == "link" and isinstance(props.get("url"), str) and props["url"].strip():
            address = props["url"].strip()
            target = urlsplit(address)
            if target.scheme.lower() not in _HIDDEN_URL_SCHEMES:
                if target.fragment and urldefrag(address)[0] == urldefrag(page_url)[0]:
                    address = "#" + target.fragment
                url = _clip(address, _PAGE_VIEW_URL_CHARS)
        merged = _merge(parts)
        if not (name or merged or value or url or states or editing_root or props.get("focusable")):
            return []  # nothing to read or act on (a decorative image, an empty paragraph)
        return [
            {
                "role": role,
                "name": name,
                "states": states,
                "reach": reach,
                "value": value,
                "url": url,
                "children": merged,
            }
        ]

    items = _merge(children(root, 0, None, None))
    # Frames whose content the main tree does not hold (out-of-process ones): after it, each
    # under a line with its title and address, its elements addressed by identity.
    for other in nodes:
        frame = other.get("frameId")
        if (
            other is root
            or other.get("parentId")
            or not isinstance(frame, str)
            or frame == main_frame
            or frame in inline_frames
            or frame not in frames
        ):
            continue
        inline_frames.add(frame)
        content = _merge(children(other, 0, None, frame))
        if content:
            items.append(
                {
                    "role": "frame",
                    "name": scrub(" ".join(text_of(other.get("name")).split())),
                    "states": [],
                    "reach": None,
                    "value": None,
                    "url": _clip(str(frames[frame].get("url") or ""), _PAGE_VIEW_URL_CHARS) or None,
                    "children": content,
                }
            )
    # Frames that were gone by the time their tree was read (removed, or moved to another
    # site's process): named, so the reader knows the page holds more there than the view shows.
    for described in tree.get("frames") or []:
        if (
            isinstance(described, Mapping)
            and described.get("unavailable") is True
            and isinstance(described.get("id"), str)
            and described["id"] not in inline_frames
        ):
            items.append(
                {
                    "role": "frame",
                    "name": "",
                    "states": ["unavailable"],
                    "reach": None,
                    "value": None,
                    "url": _clip(str(described.get("url") or ""), _PAGE_VIEW_URL_CHARS) or None,
                    "children": [],
                }
            )
    lines: list[tuple[int, str, str]] = []  # (depth, line, searchable text)

    def emit(item, depth: int) -> None:
        if isinstance(item, tuple):
            text, owner = item
            ref = ref_for(owner)
            lines.append((depth, f"- text{f' [ref=e{ref}]' if ref else ''}: {text}", text))
            return
        kids = item["children"]
        name = item["name"]
        inline = None
        if kids and all(isinstance(kid, tuple) for kid in kids):
            joined = " ".join(text for text, _ in kids)
            if not name or _squash(joined) != _squash(name):
                inline = joined
            kids = []
        elif kids and name and _squash(name) == _squash(" ".join(_texts(kids))):
            name = ""  # named from its content, which the lines below show
        head = "- " + item["role"]
        if name:
            head += " " + json.dumps(_clip(name, _PAGE_VIEW_NAME_CHARS), ensure_ascii=False)
        head += "".join(f" [{state}]" for state in item["states"])
        ref = ref_for(item["reach"])
        if ref:
            head += f" [ref=e{ref}]"
        tail = item["value"] or inline
        if tail:
            head += ": " + tail
        if item["url"]:
            head += " -> " + item["url"]
        lines.append((depth, head, " ".join(part for part in (name, tail) if part)))
        for kid in kids:
            emit(kid, depth + 1)

    for item in items:
        emit(item, 0)
    rendered = ["  " * min(depth, _PAGE_VIEW_INDENT_LEVELS) + line for depth, line, _ in lines]
    full_text = "\n".join(rendered)
    result: dict[str, Any] = {
        "url": page_url,
        "title": page.string(page.document.get("title")),
        "document": page.identity(),
        "refs": refs,
        "frame_refs": frame_refs,
        "next_ref": next_ref,
        "full_text": full_text,
        "lines": len(rendered),
    }
    if find is None:
        result["text"], result["complete"] = _cut_page_view(rendered, max_chars)
        return result
    depths = [depth for depth, _, _ in lines]
    matches = _match_lines([search for _, _, search in lines], find)
    result["find"] = {"find": find.strip(), "matches": len(matches)}
    result["complete"] = False
    if not matches:
        result["text"] = (
            f"[Nothing on this page matches {json.dumps(find.strip(), ensure_ascii=False)}. "
            "Observe without find to read the page from the top, or use other words from the "
            "control's visible text or label.]"
        )
        return result
    parents, ends = _line_tree(depths)
    for context in _PAGE_VIEW_CONTEXT:
        keep: set[int] = set()
        for match in matches:
            parent = parents[match]
            low = parent + 1 if parent >= 0 else 0
            high = ends[parent] if parent >= 0 else len(rendered)
            keep.update(range(max(low, match - context), match))
            keep.update(range(match, min(ends[match], match + _PAGE_VIEW_MATCH_LINES)))
            keep.update(range(ends[match], min(high, ends[match] + context)))
        for line in list(keep):
            parent = parents[line]
            while parent >= 0 and parent not in keep:
                keep.add(parent)
                parent = parents[parent]
        shown = [rendered[line] for line in sorted(keep)]
        if len("\n".join(shown)) <= max_chars:
            break
    text, complete = _cut_page_view(shown, max_chars, focused=True)
    result["text"] = text
    return result


def _merge(parts: list) -> list:
    """Join adjacent text pieces as the page writes them (inline text, no added spaces); a
    break (``None``: a flattened block or line break) or an element ends a text run."""
    merged: list = []
    pieces: list[str] = []
    owner = None

    def flush() -> None:
        text = " ".join("".join(pieces).split())
        if text:
            merged.append((text, owner))
        pieces.clear()

    for part in parts:
        if part is None:
            flush()
        elif isinstance(part, tuple):
            if pieces and part[1] != owner:
                flush()
            pieces.append(part[0])
            owner = part[1]
        else:
            flush()
            merged.append(part)
    flush()
    return merged


def _texts(items: list) -> list[str]:
    """The text a list of page view items shows, in order: text runs, and the names of elements
    without content of their own (an image's description), as a name from content joins them."""
    found: list[str] = []
    for item in items:
        if isinstance(item, tuple):
            found.append(item[0])
        elif item["children"]:
            found.extend(_texts(item["children"]))
        elif item["name"]:
            found.append(item["name"])
    return found


def _match_lines(searches: list[str], phrase: str) -> list[int]:
    """Lines whose name, text or value contains ``phrase``, folded as a focused DOM view folds
    text: exact spelling first, then without diacritics when nothing matches exactly."""
    for fold in (_fold, _fold_marks):
        needle = fold(phrase).strip()
        if not needle:
            continue
        found = [index for index, search in enumerate(searches) if needle in fold(search)]
        if found:
            return found
    return []


def _line_tree(depths: list[int]) -> tuple[list[int], list[int]]:
    """Each line's parent line (-1 at the top) and the end (exclusive) of its subtree."""
    parents = [-1] * len(depths)
    ends = [len(depths)] * len(depths)
    stack: list[int] = []
    for index, depth in enumerate(depths):
        while stack and depths[stack[-1]] >= depth:
            ends[stack.pop()] = index
        parents[index] = stack[-1] if stack else -1
        stack.append(index)
    return parents, ends


def _cut_page_view(lines: list[str], max_chars: int, *, focused: bool = False) -> tuple[str, bool]:
    """The lines that fit in ``max_chars``, and a final line saying what was left out."""
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text, not focused
    cut = text.rfind("\n", 0, max_chars + 1)
    if focused:
        shown = text[: max(cut, 0)]
        left = len(lines) - shown.count("\n") - 1 if shown else len(lines)
        return (
            shown + f"\n[{left} more matching lines are left out. Observe with a longer phrase.]",
            False,
        )
    if cut < max_chars // 2:
        # One very long line (a long paragraph): cut inside it rather than show almost nothing.
        shown, offset = text[:max_chars] + "…", max_chars
    else:
        shown, offset = text[:cut], cut + 1
    left_lines = text.count("\n", offset) + 1
    return (
        shown
        + f"\n[Page view cut for length: {left_lines} more lines ({len(text) - offset} characters) "
        f"are left out. Read on with browser_source from offset {offset}, or observe with find "
        "and a word from the control you need.]",
        False,
    )


def page_document(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """The identity of a DOM snapshot's main document, as a page view records it in
    ``document``; None without one. Any navigation, even to the same address, changes it."""
    try:
        return _PageDocument(snapshot).identity()
    except PageViewUnavailable:
        return None


def page_view_element(
    snapshot: Mapping[str, Any],
    document: Mapping[str, Any],
    backend_node_id: int,
    name: str,
    *,
    by_identity: bool = False,
) -> tuple[int, list[dict[str, Any]]] | None:
    """The element behind a page view ref, in a later DOM snapshot of the same page.

    Returns its node index and ``compact_dom_snapshot`` records (same fields, clipping and
    selector) for it and its descendants in that view, so actions keep today's selector and
    identity checks. None when the page shows another document (``document`` is the page
    view's), or the element is gone, is another element now, or can no longer be addressed.
    ``by_identity``: the element is addressed by its identity (an open shadow root's element),
    so it needs no selector.
    """
    try:
        page = _PageDocument(snapshot)
    except PageViewUnavailable:
        return None
    if page.identity() != dict(document):
        return None
    index = page.index_of.get(backend_node_id)
    if (
        index is None
        or page.name(index) != name
        or page.types[index] != 1
        or (page.selector(index) is None and not by_identity)
    ):
        return None
    end = index + 1
    inside = {index}
    while end < len(page.parents) and page.parents[end] in inside:
        inside.add(end)
        end += 1
    # The same membership as compact_dom_snapshot: rendered nodes, their ancestors, and the
    # options of rendered dropdowns (which have no layout of their own).
    rendered = set(page.document["layout"]["nodeIndex"])
    included = set()
    for node in range(end - 1, index - 1, -1):
        if node in rendered or node in included:
            included.add(node)
            if page.parents[node] in inside:
                included.add(page.parents[node])

    def rendered_select(node: int) -> bool:
        return node in rendered and page.name(node) == "SELECT"

    ancestor = page.parents[index]
    while ancestor >= 0 and not rendered_select(ancestor):
        ancestor = page.parents[ancestor]
    in_select = {index} if ancestor >= 0 else set()
    for node in range(index + 1, end):
        parent = page.parents[node]
        if parent in in_select or rendered_select(parent):
            in_select.add(node)
    included |= in_select
    included.add(index)

    def sparse(name: str) -> dict[int, Any]:
        values = page.nodes.get(name, {})
        return dict(zip(values.get("index", []), values.get("value", []), strict=True))

    values = sparse("inputValue") | sparse("textValue")
    checked = set(page.nodes.get("inputChecked", {}).get("index", []))
    selected = set(page.nodes.get("optionSelected", {}).get("index", []))
    records = []
    for node in range(index, end):
        if node not in included:
            continue
        record: dict[str, Any] = {"node": node, "parent": page.parents[node]}

        def clip(value: str, record=record) -> str:
            if len(value) > 320:
                record["clipped"] = True
                return value[:320]
            return value

        record["name"] = page.name(node)
        text = page.string(page.nodes["nodeValue"][node])
        if text:
            record["text"] = clip(text)
        selector = page.selector(node)
        if selector:
            record["selector"] = selector
        attributes = page.nodes["attributes"][node]
        if attributes:
            record["attributes"] = {
                page.string(attributes[i]): clip(page.string(attributes[i + 1]))
                for i in range(0, len(attributes), 2)
            }
        if node in values:
            record["value"] = clip(page.string(values[node]))
        if node in checked:
            record["checked"] = True
        if node in selected:
            record["selected"] = True
        records.append(record)
    return index, records


def frame_element(
    tree: Mapping[str, Any], frame_id: str, loader: str | None, backend_node_id: int
) -> dict[str, Any] | None:
    """The element behind a page view ref in another frame, in a later accessibility snapshot.

    Returns its role, accessible name, whether it is editable, and its frame's address and
    origin; None when that frame is gone or shows another document now (another ``loader``),
    or holds no element with that identity. Its frame is found as the page view finds it: a
    node's frame is its own ``frameId`` or else its parent's.
    """
    nodes = tree.get("nodes") if isinstance(tree, Mapping) else None
    described = tree.get("frames") if isinstance(tree, Mapping) else None
    if not isinstance(nodes, list) or not isinstance(described, list):
        return None
    info = next(
        (item for item in described if isinstance(item, Mapping) and item.get("id") == frame_id),
        None,
    )
    if info is None or (str(info.get("loader") or "") or None) != loader:
        return None
    by_id = {
        node["nodeId"]: node for node in nodes if isinstance(node, Mapping) and "nodeId" in node
    }
    pending = [(node, node.get("frameId")) for node in by_id.values() if not node.get("parentId")]
    seen: set[Any] = set()
    while pending:
        node, where = pending.pop()
        if node["nodeId"] in seen:
            continue
        seen.add(node["nodeId"])
        where = node.get("frameId") or where
        if (
            where == frame_id
            and node.get("backendDOMNodeId") == backend_node_id
            and not node.get("ignored")
        ):
            role = node.get("role")
            name = node.get("name")
            properties = {
                item.get("name"): item.get("value", {}).get("value")
                for item in node.get("properties") or []
                if isinstance(item, Mapping) and isinstance(item.get("value"), Mapping)
            }
            return {
                "role": str(role.get("value") or "") if isinstance(role, Mapping) else "",
                "name": " ".join(
                    str(name.get("value") or "").split() if isinstance(name, Mapping) else []
                ),
                "editable": bool(properties.get("editable")),
                "url": str(info.get("url") or ""),
                "origin": str(info.get("origin") or ""),
            }
        for child in node.get("childIds") or []:
            if child in by_id:
                pending.append((by_id[child], where))
    return None
