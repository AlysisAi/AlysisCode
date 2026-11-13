"""Save observed form/editor data, never executable browser handles or authority."""

from __future__ import annotations


def form_draft(snapshot):
    data = snapshot.get("data", {})
    if snapshot.get("truncated") or not data.get("documents"):
        return {"complete": False, "reason": "The page could not provide a complete DOM snapshot."}
    strings = data["strings"]

    def text(index):
        return "" if index == -1 else strings[index]

    doc = data["documents"][0]
    nodes = doc["nodes"]
    rendered = set(doc["layout"]["nodeIndex"])
    values = dict(
        zip(
            nodes.get("inputValue", {}).get("index", []),
            nodes.get("inputValue", {}).get("value", []),
            strict=True,
        )
    )
    values.update(
        zip(
            nodes.get("textValue", {}).get("index", []),
            nodes.get("textValue", {}).get("value", []),
            strict=True,
        )
    )
    checked = set(nodes.get("inputChecked", {}).get("index", []))
    selected = set(nodes.get("optionSelected", {}).get("index", []))
    attributes = [
        {text(a[n]).lower(): text(a[n + 1]) for n in range(0, len(a), 2)}
        for a in nodes["attributes"]
    ]
    # Preserve inert, observed structure rather than guessing line breaks or
    # replaying HTML/JS into a different editor. Rendering does not prove the
    # editor's full application state was captured, so these remain partial.
    editor_nodes = {}
    editor_roots = []
    for index, name in enumerate(nodes["nodeName"]):
        tag = text(name).upper()
        parent = nodes["parentIndex"][index]
        attrs = attributes[index]
        if index not in rendered or tag in {
            "INPUT",
            "TEXTAREA",
            "SELECT",
            "IFRAME",
            "FRAME",
            "OBJECT",
            "EMBED",
            "CANVAS",
        }:
            continue
        if (
            attrs.get("contenteditable", "false").lower() in {"", "true", "plaintext-only"}
            and parent not in editor_nodes
        ):
            entry = {"tag": tag, "children": []}
            editor_nodes[index] = entry
            editor_roots.append((index, entry))
        elif parent in editor_nodes:
            if nodes["nodeType"][index] == 3:
                entry = {"text": text(nodes["nodeValue"][index])}
            elif nodes["nodeType"][index] == 1:
                entry = {"tag": tag, "children": []}
            else:
                continue
            editor_nodes[parent]["children"].append(entry)
            editor_nodes[index] = entry
    fields, omitted = [], 0
    for index, tree in editor_roots:
        fields.append(
            {
                "tag": tree["tag"],
                "type": "contenteditable",
                "identity": {
                    k: attributes[index][k]
                    for k in ("id", "name", "aria-label", "placeholder")
                    if k in attributes[index]
                },
                "content_tree": tree,
                "complete": False,
                "reason": "Observed text and element structure only; styles, embedded objects, hidden content and application state are not preserved. Reobserve before restoring; never silently replace a formatted document with plain text.",
            }
        )
    for index, name in enumerate(nodes["nodeName"]):
        tag = text(name).upper()
        attrs = attributes[index]
        if index not in rendered:
            continue
        if tag not in {"INPUT", "TEXTAREA", "SELECT"}:
            if tag in {"IFRAME", "FRAME", "OBJECT", "EMBED", "CANVAS"}:
                omitted += 1
            continue
        kind = attrs.get("type", "text").lower() if tag == "INPUT" else tag.lower()
        autocomplete = attrs.get("autocomplete", "").lower().split()
        if kind in {"hidden", "password", "file", "submit", "reset", "button", "image"} or any(
            item.startswith("cc-") or item in {"current-password", "new-password", "one-time-code"}
            for item in autocomplete
        ):
            omitted += 1
            continue
        field = {
            "tag": tag,
            "type": kind,
            "identity": {
                k: attrs[k] for k in ("id", "name", "aria-label", "placeholder") if k in attrs
            },
        }
        if kind in {"checkbox", "radio"}:
            field["checked"] = index in checked
            field["value"] = text(values[index]) if index in values else attrs.get("value", "on")
        elif tag == "SELECT":
            choices = []
            for option in selected:
                parent = nodes["parentIndex"][option]
                while parent >= 0 and parent != index:
                    parent = nodes["parentIndex"][parent]
                if parent == index:
                    option_attrs = nodes["attributes"][option]
                    option_attrs = {
                        text(option_attrs[n]): text(option_attrs[n + 1])
                        for n in range(0, len(option_attrs), 2)
                    }
                    label = "".join(
                        text(nodes["nodeValue"][n])
                        for n, p in enumerate(nodes["parentIndex"])
                        if p == option
                    )
                    choices.append({"label": label, "value": option_attrs.get("value", label)})
            field["selected_options"] = choices
        elif index in values:
            field["value"] = text(values[index])
        else:
            omitted += 1
            continue
        fields.append(field)
    return {
        "url": text(doc["documentURL"]),
        "title": text(doc["title"]),
        "fields": fields,
        "complete": omitted == 0 and not editor_roots and len(data["documents"]) == 1,
        "omitted_controls": omitted,
        "partial_controls": len(editor_roots),
        "embedded_documents": len(data["documents"]) - 1,
    }
