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
