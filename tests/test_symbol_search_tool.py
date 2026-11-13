from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.symbols import SymbolSearchError, symbol_search


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="symbol-search-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(tmp_path: Path):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_symbol_search_finds_python_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "demo.py",
        (
            "DEFAULT_TIMEOUT = 30\n"
            "\n"
            "class Parser(BaseParser):\n"
            "    def run(self, payload: str) -> str:\n"
            "        return payload\n"
            "\n"
            "async def load_config(name: str) -> dict[str, str]:\n"
            '    return {"name": name}\n'
        ),
    )

    class_result = symbol_search(root=tmp_path, query="Parser", kind="class", exact=True)
    assert class_result["backend"] == "python_ast"
    assert class_result["parsed_files"] == 1
    assert class_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 3,
            "kind": "class",
            "name": "Parser",
            "signature": "class Parser(BaseParser)",
        }
    ]

    method_result = symbol_search(root=tmp_path, query="run", kind="method", exact=True)
    assert method_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 4,
            "kind": "method",
            "name": "Parser.run",
            "signature": "def Parser.run(self, payload: str) -> str",
        }
    ]

    function_result = symbol_search(root=tmp_path, query="load", kind="function")
    assert function_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 7,
            "kind": "function",
            "name": "load_config",
            "signature": "async def load_config(name: str) -> dict[str, str]",
        }
    ]

    constant_result = symbol_search(root=tmp_path, query="DEFAULT", kind="constant")
    assert constant_result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 1,
            "kind": "constant",
            "name": "DEFAULT_TIMEOUT",
            "signature": "DEFAULT_TIMEOUT = 30",
        }
    ]


def test_symbol_search_can_include_details_snippets_and_references(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "demo.py",
        (
            "class Parser:\n"
            "    def run(self, payload: str) -> str:\n"
            "        return payload\n"
            "\n"
            "def caller(parser: Parser) -> str:\n"
            "    return parser.run('ok')\n"
        ),
    )

    result = symbol_search(
        root=tmp_path,
        query="run",
        kind="method",
        exact=True,
        include_details=True,
        include_snippet=True,
        include_references=True,
    )

    assert result["include_details"] is True
    assert result["include_snippet"] is True
    assert result["include_references"] is True
    assert result["matches"] == [
        {
            "path": "src/demo.py",
            "line": 2,
            "kind": "method",
            "name": "Parser.run",
            "signature": "def Parser.run(self, payload: str) -> str",
            "end_line": 3,
            "parent": "Parser",
            "snippet": {
                "start_line": 2,
                "end_line": 3,
                "truncated": False,
                "lines": [
                    {"line": 2, "text": "def run(self, payload: str) -> str:"},
                    {"line": 3, "text": "return payload"},
                ],
            },
            "references": [
                {
                    "path": "src/demo.py",
                    "line": 6,
                    "text": "return parser.run('ok')",
                }
            ],
        }
    ]


def test_symbol_search_finds_js_ts_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "web" / "app.ts",
        (
            "export async function fetchDoc(url: string) {\n"
            "  return url;\n"
            "}\n"
            "\n"
            "export default function buildClient() {\n"
            "  return {};\n"
            "}\n"
            "\n"
            "export class Parser {\n"
            "  run(payload: string): string {\n"
            "    return payload;\n"
            "  }\n"
            "}\n"
            "\n"
            "export const handler = (req: Request) => req;\n"
            "const buildThing = function(name: string) {\n"
            "  return name;\n"
            "};\n"
            "const DEFAULT_TIMEOUT = 30;\n"
            "const plainValue = createValue();\n"
        ),
    )

    class_result = symbol_search(root=tmp_path, query="Parser", kind="class", exact=True)
    assert class_result["backend"] == "js_ts_heuristic"
    assert class_result["parsed_files"] == 1
    assert class_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 9,
            "kind": "class",
            "name": "Parser",
            "signature": "export class Parser {",
        }
    ]

    method_result = symbol_search(root=tmp_path, query="run", kind="method", exact=True)
    assert method_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 10,
            "kind": "method",
            "name": "Parser.run",
            "signature": "run(payload: string): string {",
        }
    ]

    function_result = symbol_search(root=tmp_path, query="handler", kind="function", exact=True)
    assert function_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 15,
            "kind": "function",
            "name": "handler",
            "signature": "export const handler = (req: Request) => req;",
        }
    ]

    constant_result = symbol_search(root=tmp_path, query="plainValue", kind="constant", exact=True)
    assert constant_result["matches"] == [
        {
            "path": "web/app.ts",
            "line": 20,
            "kind": "constant",
            "name": "plainValue",
            "signature": "const plainValue = createValue();",
        }
    ]


def test_symbol_search_js_ts_exact_and_kind_filters(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "handlers.ts",
        (
            "export default function buildClient() {\n"
            "  return {};\n"
            "}\n"
            "export const buildHandler = (req: Request) => req;\n"
            "const buildCount = 3;\n"
        ),
    )

    exact_result = symbol_search(root=tmp_path, query="build", kind="function", exact=True)
    assert exact_result["matches"] == []

    filtered_result = symbol_search(root=tmp_path, query="build", kind="function")
    assert [match["name"] for match in filtered_result["matches"]] == [
        "buildClient",
        "buildHandler",
    ]

    constant_result = symbol_search(root=tmp_path, query="build", kind="constant")
    assert [match["name"] for match in constant_result["matches"]] == ["buildCount"]


@pytest.mark.parametrize(
    ("source", "query", "expected"),
    [
        pytest.param(
            "const buildThing: (name: string) => string = (name) => name;\n",
            "buildThing",
            {
                "path": "ui/typed.ts",
                "line": 1,
                "kind": "function",
                "name": "buildThing",
                "signature": "const buildThing: (name: string) => string = (name) => name;",
            },
            id="typed-top-level-const-arrow-function",
        ),
        pytest.param(
            "const buildThing: (name: string) => string =\n  (name) => name;\n",
            "buildThing",
            {
                "path": "ui/typed_multiline.ts",
                "line": 1,
                "kind": "function",
                "name": "buildThing",
                "signature": "const buildThing: (name: string) => string = (name) => name;",
            },
            id="typed-multiline-top-level-const-arrow-function",
        ),
        pytest.param(
            "const buildThing: (name: string) => string = function(name) { return name; };\n",
            "buildThing",
            {
                "path": "ui/typed_function.ts",
                "line": 1,
                "kind": "function",
                "name": "buildThing",
                "signature": "const buildThing: (name: string) => string = function(name) { return name; };",
            },
            id="typed-top-level-const-function-expression",
        ),
        pytest.param(
            "class View {\n  private renderItem: (item: Item) => Item =\n    (item) => item;\n}\n",
            "renderItem",
            {
                "path": "ui/view_typed.tsx",
                "line": 2,
                "kind": "method",
                "name": "View.renderItem",
                "signature": "private renderItem: (item: Item) => Item = (item) => item;",
            },
            id="typed-class-field-arrow-as-method",
        ),
        pytest.param(
            "class View {\n  onClick?: () => void = () => {};\n}\n",
            "onClick",
            {
                "path": "ui/view_optional.tsx",
                "line": 2,
                "kind": "method",
                "name": "View.onClick",
                "signature": "onClick?: () => void = () => {};",
            },
            id="optional-typed-class-field-arrow-as-method",
        ),
        pytest.param(
            "const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;\n",
            "Table",
            {
                "path": "ui/nested_generic.ts",
                "line": 1,
                "kind": "function",
                "name": "Table",
                "signature": "const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;",
            },
            id="generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "export const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n",
            "Table",
            {
                "path": "ui/nested_export.ts",
                "line": 1,
                "kind": "function",
                "name": "Table",
                "signature": "export const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;",
            },
            id="exported-generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "const Table: <T extends Record<string, number>>(rows: T[]) => number = <T extends Record<string, number>>(rows) => rows.length;\n",
            "Table",
            {
                "path": "ui/nested_typed.ts",
                "line": 1,
                "kind": "function",
                "name": "Table",
                "signature": "const Table: <T extends Record<string, number>>(rows: T[]) => number = <T extends Record<string, number>>(rows) => rows.length;",
            },
            id="typed-generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "const Table =\n  <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n",
            "Table",
            {
                "path": "ui/nested_multiline.ts",
                "line": 1,
                "kind": "function",
                "name": "Table",
                "signature": "const Table = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;",
            },
            id="multiline-generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "class View {\n  render = <T extends Record<string, number>>(rows: T[]) => rows.length;\n}\n",
            "render",
            {
                "path": "ui/nested_method.tsx",
                "line": 2,
                "kind": "method",
                "name": "View.render",
                "signature": "render = <T extends Record<string, number>>(rows: T[]) => rows.length;",
            },
            id="class-field-generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "class View {\n  private render: <T extends Foo<Bar<Baz>>>(rows: T[]) => number =\n    <T extends Foo<Bar<Baz>>>(rows) => rows.length;\n}\n",
            "render",
            {
                "path": "ui/nested_typed_method.tsx",
                "line": 2,
                "kind": "method",
                "name": "View.render",
                "signature": "private render: <T extends Foo<Bar<Baz>>>(rows: T[]) => number = <T extends Foo<Bar<Baz>>>(rows) => rows.length;",
            },
            id="typed-class-field-generic-arrow-with-nested-constraints",
        ),
        pytest.param(
            "const buildThing =\n  function(\n    name: string,\n  ) {\n    return name;\n  };\n",
            "buildThing",
            {
                "path": "ui/builders.ts",
                "line": 1,
                "kind": "function",
                "name": "buildThing",
                "signature": "const buildThing = function( name: string, ) {",
            },
            id="multiline-top-level-function-expression-assigned-to-const",
        ),
    ],
)
def test_symbol_search_finds_typed_and_generic_js_ts_assignments(
    tmp_path: Path, source: str, query: str, expected: dict
) -> None:
    _write(tmp_path / expected["path"], source)

    result = symbol_search(root=tmp_path, query=query, kind=expected["kind"], exact=True)

    assert result["matches"] == [expected]


def test_symbol_search_applies_exact_and_kind_filters_to_typed_ts_assignment_patterns(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "typed_filters.tsx",
        (
            "export const Table: <T>(rows: T[]) => number = <T>(rows) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  onClick?: () => void = () => {};\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="onClick", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.onClick"]
    assert constant_result["matches"] == []


def test_symbol_search_applies_exact_and_kind_filters_to_nested_generic_arrow_patterns(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ui" / "nested_filters.tsx",
        (
            "export const Table = <T extends Record<string, number>>(rows: T[]) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  render = <T extends Foo<Bar<Baz>>>(rows: T[]) => rows.length;\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="render", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.render"]
    assert constant_result["matches"] == []


def test_symbol_search_finds_multiline_top_level_arrow_function(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "app.tsx",
        ("export const App = (\n  props: Props,\n) => {\n  return props;\n};\n"),
    )

    result = symbol_search(root=tmp_path, query="App", kind="function", exact=True)

    assert result["backend"] == "js_ts_heuristic"
    assert result["matches"] == [
        {
            "path": "ui/app.tsx",
            "line": 1,
            "kind": "function",
            "name": "App",
            "signature": "export const App = ( props: Props, ) => {",
        }
    ]


def test_symbol_search_finds_generic_ts_arrow_functions(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "table.tsx",
        (
            "export const Table = <T,>({ rows }: Props<T>) => rows.length;\n"
            "const mapValues = <T>(items: T[]) => items;\n"
        ),
    )

    table_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    map_values_result = symbol_search(root=tmp_path, query="mapValues", kind="function", exact=True)

    assert [match["name"] for match in table_result["matches"]] == ["Table"]
    assert [match["name"] for match in map_values_result["matches"]] == ["mapValues"]
    assert table_result["matches"][0]["signature"] == (
        "export const Table = <T,>({ rows }: Props<T>) => rows.length;"
    )
    assert map_values_result["matches"][0]["signature"] == (
        "const mapValues = <T>(items: T[]) => items;"
    )


def test_symbol_search_finds_class_field_function_patterns_as_methods(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "view.tsx",
        (
            "export class View {\n"
            "  handleClick = () => {\n"
            "    return true;\n"
            "  };\n"
            "\n"
            "  private renderItem = function(\n"
            "    item: Item,\n"
            "  ) {\n"
            "    return item;\n"
            "  };\n"
            "}\n"
        ),
    )

    result = symbol_search(root=tmp_path, query="View.", kind="method")

    assert result["matches"] == [
        {
            "path": "ui/view.tsx",
            "line": 2,
            "kind": "method",
            "name": "View.handleClick",
            "signature": "handleClick = () => {",
        },
        {
            "path": "ui/view.tsx",
            "line": 6,
            "kind": "method",
            "name": "View.renderItem",
            "signature": "private renderItem = function( item: Item, ) {",
        },
    ]


def test_symbol_search_applies_exact_and_kind_filters_to_new_js_ts_patterns(tmp_path: Path) -> None:
    _write(
        tmp_path / "ui" / "mixed.tsx",
        (
            "export const Table = <T,>({ rows }: Props<T>) => rows.length;\n"
            "const TableValue = createValue();\n"
            "class View {\n"
            "  handleClick = () => true;\n"
            "}\n"
        ),
    )

    function_result = symbol_search(root=tmp_path, query="Table", kind="function", exact=True)
    method_result = symbol_search(root=tmp_path, query="handleClick", kind="method", exact=True)
    constant_result = symbol_search(root=tmp_path, query="Table", kind="constant", exact=True)

    assert [match["name"] for match in function_result["matches"]] == ["Table"]
    assert [match["name"] for match in method_result["matches"]] == ["View.handleClick"]
    assert constant_result["matches"] == []


def test_symbol_search_reports_mixed_static_backend(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo.py", "def build_tools() -> None:\n    pass\n")
    _write(tmp_path / "web" / "demo.ts", "export function buildTools() {}\n")

    result = symbol_search(root=tmp_path, query="build")

    assert [match["name"] for match in result["matches"]] == ["build_tools", "buildTools"]
    assert result["backend"] == "mixed_static"
    assert result["parsed_files"] == 2


def test_symbol_search_finds_java_symbols(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "main" / "java" / "com" / "example" / "CSVParser.java",
        (
            "package com.example;\n"
            "\n"
            "import java.io.IOException;\n"
            "\n"
            "@Deprecated\n"
            "public final class CSVParser<T> {\n"
            "  public CSVParser() {}\n"
            "  public static <R> R parseLine(String input) throws IOException { return null; }\n"
            "  private final void reset() {}\n"
            "}\n"
            "\n"
            "interface Loader<T> {\n"
            "  T load(String name) throws IOException;\n"
            "}\n"
            "\n"
            "enum Mode {\n"
            "  FAST, SLOW;\n"
            "  public boolean enabled() { return true; }\n"
            "}\n"
            "\n"
            "record UserRecord(String id) {\n"
            "  public String label() { return id; }\n"
            "}\n"
        ),
    )

    class_result = symbol_search(root=tmp_path, query="CSVParser", kind="class", exact=True)
    constructor_result = symbol_search(root=tmp_path, query="CSVParser", kind="method", exact=True)
    method_result = symbol_search(root=tmp_path, query="parseLine", kind="method", exact=True)
    interface_result = symbol_search(root=tmp_path, query="Loader", kind="class", exact=True)
    enum_result = symbol_search(root=tmp_path, query="Mode", kind="class", exact=True)
    record_result = symbol_search(root=tmp_path, query="UserRecord", kind="class", exact=True)

    assert class_result["backend"] == "java_heuristic"
    assert class_result["parsed_files"] == 1
    assert [match["name"] for match in class_result["matches"]] == ["CSVParser"]
    assert [match["name"] for match in constructor_result["matches"]] == ["CSVParser.CSVParser"]
    assert [match["name"] for match in method_result["matches"]] == ["CSVParser.parseLine"]
    assert [match["name"] for match in interface_result["matches"]] == ["Loader"]
    assert [match["name"] for match in enum_result["matches"]] == ["Mode"]
    assert [match["name"] for match in record_result["matches"]] == ["UserRecord"]
    assert method_result["matches"][0]["signature"] == (
        "public static <R> R parseLine(String input) throws IOException { return null; }"
    )


def test_symbol_search_respects_scope_globs_exact_and_max_results(tmp_path: Path) -> None:
    _write(
        tmp_path / "pkg" / "alpha.py",
        "def build_index() -> None:\n    pass\n",
    )
    _write(
        tmp_path / "pkg" / "beta.py",
        "def build_graph() -> None:\n    pass\n",
    )
    _write(
        tmp_path / "pkg" / "nested" / "gamma.py",
        "def build_service() -> None:\n    pass\n",
    )

    exact_result = symbol_search(
        root=tmp_path,
        query="build_graph",
        root_path="pkg",
        globs=["pkg/*.py"],
        exact=True,
    )
    assert [match["name"] for match in exact_result["matches"]] == ["build_graph"]

    limited = symbol_search(
        root=tmp_path,
        query="build",
        root_path="pkg",
        max_results=2,
    )
    assert len(limited["matches"]) == 2
    assert limited["truncated"] is True


def test_symbol_search_reports_only_truly_unsupported_files(tmp_path: Path) -> None:
    _write(tmp_path / "web" / "app.rb", "def handler\n  :ok\nend\n")

    result = symbol_search(root=tmp_path, query="handler", root_path="web/app.rb")

    assert result["matches"] == []
    assert result["parsed_files"] == 0
    assert result["backend"] == "none"
    assert "Skipped unsupported file(s): web/app.rb" in result["notes"]
    assert result["coverage"]["unsupported_files"] == 1
    assert any("lexical fallback" in note for note in result["notes"])


def test_symbol_search_rejects_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(SymbolSearchError, match="query must be a non-empty string"):
        symbol_search(root=tmp_path, query="")

    with pytest.raises(SymbolSearchError, match="Unsupported kind"):
        symbol_search(root=tmp_path, query="x", kind="variable")

    with pytest.raises(SymbolSearchError, match="root_path escapes root"):
        symbol_search(root=tmp_path, query="x", root_path="../outside.py")


def test_build_tools_registers_symbol_search(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    assert "symbol_search" in tools
    schema = tools["symbol_search"].as_openai_tool()["function"]["parameters"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["kind"]["enum"] == [
        "function",
        "class",
        "method",
        "constant",
    ]
    assert schema["properties"]["max_results"]["default"] == 100
    assert schema["properties"]["exact"]["default"] is False
    assert schema["properties"]["include_details"]["default"] is False
    assert schema["properties"]["include_snippet"]["default"] is False
    assert schema["properties"]["include_references"]["default"] is False


@pytest.mark.parametrize(
    "name", ["μετρητής", "计数", "عدد", "गिनती", "cafe\u0301", "$résultat$", "a\u200cb"]
)
@pytest.mark.parametrize("suffix", ["js", "ts", "java"])
def test_symbol_search_keeps_unicode_identifier_spelling(
    tmp_path: Path, name: str, suffix: str
) -> None:
    class_name = f"容器{name}"
    if suffix == "java":
        source = (
            f"public class {class_name} {{\n"
            f"  public {class_name}() {{}}\n"
            f"  public int {name}() {{ return 1; }}\n"
            "}\n"
        )
    else:
        source = (
            f"function {name}() {{ return 1; }}\n"
            f"class {class_name} {{\n"
            f"  {name}() {{ return 2; }}\n"
            f"  {name}Field = () => 3;\n"
            "}\n"
            f"const {name}Arrow = () => 4;\n"
        )
    _write(tmp_path / f"fixture.{suffix}", source)
    result = symbol_search(root=tmp_path, query=name, exact=True, include_details=True)
    assert [m["name"] for m in result["matches"]] == (
        [f"{class_name}.{name}"] if suffix == "java" else [name, f"{class_name}.{name}"]
    )
    classes = symbol_search(root=tmp_path, query=class_name, kind="class", exact=True)
    assert [m["name"] for m in classes["matches"]] == [class_name]
    assert result["coverage"]["heuristic_files"] == 1
    assert any("not a complete parser" in note for note in result["notes"])
    if suffix == "java":
        constructors = symbol_search(root=tmp_path, query=class_name, kind="method", exact=True)
        assert [m["name"] for m in constructors["matches"]] == [f"{class_name}.{class_name}"]
    else:
        for ending, kind in [("Field", "method"), ("Arrow", "function")]:
            found = symbol_search(root=tmp_path, query=f"{name}{ending}", kind=kind, exact=True)
            assert len(found["matches"]) == 1
            assert found["matches"][0]["name"].endswith(f"{name}{ending}")


@pytest.mark.parametrize("name", ["3bad", "\u0301bad", "bad—name", "bad⚠"])
@pytest.mark.parametrize("suffix", ["js", "java"])
def test_unicode_candidates_do_not_turn_invalid_tokens_into_names(
    tmp_path: Path, name: str, suffix: str
) -> None:
    source = (
        f"class {name} {{}}\nfunction {name}() {{}}\n"
        if suffix == "js"
        else f"class {name} {{}}\nclass Good {{\n  int {name}() {{ return 1; }}\n}}\n"
    )
    _write(tmp_path / f"fixture.{suffix}", source)
    result = symbol_search(root=tmp_path, query="bad")
    assert result["matches"] == []
    assert result["truncated"] is False
    assert result["coverage"]["heuristic_files"] == 1


def test_unicode_whitespace_is_a_boundary_not_part_of_the_identifier(tmp_path: Path) -> None:
    _write(tmp_path / "fixture.js", "class 容器\u00a0{}\nfunction μέτρο\u00a0() {}\n")
    for name in ("容器", "μέτρο"):
        result = symbol_search(root=tmp_path, query=name, exact=True)
        assert [m["name"] for m in result["matches"]] == [name]


@pytest.mark.parametrize("suffix", ["js", "java"])
def test_undecoded_escaped_names_do_not_report_a_partial_identifier(
    tmp_path: Path, suffix: str
) -> None:
    _write(
        tmp_path / f"fixture.{suffix}",
        "class Fo\\u006f {}\n" + ("const Fo\\u006fArrow = () => 1;\n" if suffix == "js" else ""),
    )
    result = symbol_search(root=tmp_path, query="Fo")
    assert result["matches"] == []
    assert result["coverage"]["heuristic_files"] == 1
    assert any("valid declarations can be missed" in note for note in result["notes"])


def test_language_specific_unicode_identifier_punctuation_remains_distinct(tmp_path: Path) -> None:
    _write(
        tmp_path / "Fixture.java",
        "class €値 {\n  int €額() { return 1; }\n  int ℘value() { return 2; }\n}\n",
    )
    _write(tmp_path / "fixture.js", "function €額() {}\nfunction ℘value() {}\n")
    result = symbol_search(root=tmp_path, query="€額", exact=True)
    assert [m["name"] for m in result["matches"]] == ["€値.€額"]
    other_identifier = symbol_search(root=tmp_path, query="℘value", exact=True)
    assert [m["path"] for m in other_identifier["matches"]] == ["fixture.js"]


def test_mixed_supported_unsupported_scope_reports_bounded_paths_and_full_counts(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "a.py", "def counter():\n    return 1\n")
    for index in range(5):
        _write(tmp_path / f"unsupported{index}.rs", "fn target_rust() {}\n")
    result = symbol_search(root=tmp_path, query="target_rust", exact=True)
    assert result["matches"] == []
    assert result["backend"] == "python_ast"
    assert result["truncated"] is False
    assert result["coverage"] == {
        "candidate_files": 6,
        "examined_files": 6,
        "heuristic_files": 0,
        "unsupported_files": 5,
        "large_or_unreadable_files": 0,
        "unparsable_files": 0,
        "unexamined_files": 0,
    }
    skipped = next(note for note in result["notes"] if "Skipped unsupported" in note)
    assert "unsupported0.rs" in skipped and "unsupported2.rs" in skipped
    assert "unsupported3.rs" not in skipped
    assert any("paths are samples" in note for note in result["notes"])


def test_result_limit_does_not_claim_unexamined_files_were_supported(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "def target_one(): pass\ndef target_two(): pass\n")
    _write(tmp_path / "z.rs", "fn target_three() {}\n")
    result = symbol_search(root=tmp_path, query="target", max_results=1)
    assert result["truncated"] is True
    assert len(result["matches"]) == 1
    assert result["coverage"]["examined_files"] == 1
    assert result["coverage"]["unexamined_files"] == 1
    assert result["coverage"]["unsupported_files"] == 0
    assert any("stopped scanning" in note for note in result["notes"])


def test_coverage_distinguishes_skipped_source_from_successful_python_parse(tmp_path: Path) -> None:
    _write(tmp_path / "valid.py", "def target(): pass\n")
    _write(tmp_path / "broken.py", "def invalid(\n")
    _write(tmp_path / "large.py", "#" * (512 * 1024 + 1))
    result = symbol_search(root=tmp_path, query="target", exact=True)
    assert result["parsed_files"] == 1
    assert result["coverage"]["unparsable_files"] == 1
    assert result["coverage"]["large_or_unreadable_files"] == 1
    assert result["coverage"]["unexamined_files"] == 0
    assert any("unparsable" in note for note in result["notes"])
    assert any("large or unreadable" in note for note in result["notes"])
