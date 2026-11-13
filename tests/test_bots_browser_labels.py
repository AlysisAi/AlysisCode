"""Which nodes count as a control's own label (HTML label rules), from the DOM snapshot."""

from __future__ import annotations

from alysis_code.bots.browser import label_backend_ids


def page(elements):
    """``elements``: (name, parent index, attributes dict) rows under BODY (index 1)."""
    strings, names, parents, attributes, types, values = [], [], [-1, 0], [[], []], [9, 1], [-1, -1]

    def intern(text):
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    names += [intern("#document"), intern("BODY")]
    for name, parent, attrs in elements:
        names.append(intern(name))
        parents.append(parent)
        types.append(3 if name == "#text" else 1)
        values.append(intern(attrs.pop("text")) if name == "#text" else -1)
        attributes.append([intern(part) for key, value in attrs.items() for part in (key, value)])
    return {
        "strings": strings,
        "documents": [
            {
                "nodes": {
                    "parentIndex": parents,
                    "nodeName": names,
                    "nodeType": types,
                    "nodeValue": values,
                    "attributes": attributes,
                    "backendNodeId": [100 + i for i in range(len(parents))],
                }
            }
        ],
    }


def test_an_enclosing_label_counts_but_a_link_inside_it_does_not():
    data = page(
        [
            ("LABEL", 1, {}),  # 2
            ("INPUT", 2, {"type": "checkbox"}),  # 3 target
            ("SPAN", 2, {}),  # 4 the styled box drawn over the input
            ("#text", 2, {"text": "Hotels"}),  # 5
            ("A", 2, {"href": "/terms"}),  # 6 interactive: keeps its own click
            ("#text", 6, {"text": "terms"}),  # 7
        ]
    )
    assert sorted(label_backend_ids(data, 3)) == [102, 104, 105]


def test_a_label_for_the_targets_id_counts_wherever_it_is():
    data = page(
        [
            ("DIV", 1, {}),  # 2
            ("INPUT", 2, {"type": "radio", "id": "hotels"}),  # 3 target
            ("LABEL", 1, {"for": "hotels"}),  # 4
            ("#text", 4, {"text": "Hotels"}),  # 5
            ("LABEL", 1, {"for": "villas"}),  # 6 another control's label
        ]
    )
    assert sorted(label_backend_ids(data, 3)) == [104, 105]


def test_a_label_that_names_another_control_or_labels_an_earlier_one_does_not_count():
    other_for = page(
        [
            ("LABEL", 1, {"for": "elsewhere"}),  # 2
            ("INPUT", 2, {"type": "checkbox"}),  # 3 target: this label is for another element
        ]
    )
    assert label_backend_ids(other_for, 3) == []
    second = page(
        [
            ("LABEL", 1, {}),  # 2
            ("INPUT", 2, {"type": "checkbox"}),  # 3 the label's control (first labelable)
            ("INPUT", 2, {"type": "checkbox"}),  # 4 target
        ]
    )
    assert label_backend_ids(second, 4) == []
    hidden_first = page(
        [
            ("LABEL", 1, {}),  # 2
            ("INPUT", 2, {"type": "hidden"}),  # 3 not labelable
            ("INPUT", 2, {"type": "checkbox"}),  # 4 target
        ]
    )
    assert sorted(label_backend_ids(hidden_first, 4)) == [102]


def test_only_labelable_targets_have_labels():
    data = page([("LABEL", 1, {}), ("DIV", 2, {})])
    assert label_backend_ids(data, 3) == []
    assert label_backend_ids(data, 99) == []
