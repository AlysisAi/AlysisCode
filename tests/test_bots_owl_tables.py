"""Owls answer questions about tables in their Library with one read-only SQL query."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image
from pydantic import ValidationError

from alysis_code.bots import owl_tables, owl_toolset, step_effects
from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.owl_tables import TABLE_TOOL, OwlTables, TableQuery, simple_name
from alysis_code.bots.runtime import BotRuntime, tool_schema
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken

TOOLS = [TABLE_TOOL]


def test_presented_query_reopens_and_reruns_only_against_recorded_owl_sources(tmp_path):
    settings = Settings(
        tmp_path / "api.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings)
    store, owner = app.state.store, settings.owner
    bot = store.save_bot(owner, BotInput(name="Data"))["id"]
    other = store.save_bot(owner, BotInput(name="Other"))["id"]
    chat = store.create_chat(owner, bot, "Figures")
    store.enqueue(owner, chat["id"], TaskInput(message="Compare sales", request_id="sales-review"))
    task = store.claim(owner)
    source = store.save_file(
        owner, bot, FileInput(name="Sales.csv", content="site,qty\nNorth,4\nSouth,7")
    )
    args = {
        "title": "Sales by site",
        "file_ids": [source["id"]],
        "sql": "SELECT site,qty FROM sales ORDER BY qty",
    }
    runtime = BotRuntime(store, owner, lambda _: None)

    def present():
        return runtime._tool(
            task,
            "present_table",
            args,
            ["present_table"],
            image_input=False,
            token=runtime.task_token(task["id"]),
        )

    saved = present()
    assert present()["id"] == saved["id"]
    assert store.file(owner, bot, saved["id"])["query_result"] is True
    with pytest.raises(Conflict, match="source binding"):
        store.save_file(
            owner, bot, FileInput(name=saved["name"], content="{}", previous_file_id=saved["id"])
        )
    with TestClient(app, raise_server_exceptions=True) as client:
        path = f"/v1/bots/{bot}/files/{saved['id']}/data-result"
        assert client.get(path).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get(path).json()["rows"] == [["North", 4], ["South", 7]]
        assert client.post(path, json={"sql": "SELECT SUM(qty) AS units FROM sales"}).json()[
            "rows"
        ] == [[11]]
        assert client.get(path).json()["sql"] == args["sql"]
        args.update(
            previous_file_id=saved["id"],
            title="Updated sales by site",
            sql="SELECT site, qty FROM sales ORDER BY qty DESC",
        )
        corrected = present()
        assert corrected["name"] == saved["name"]
        assert corrected["series_id"] == saved["series_id"]
        assert corrected["revision"] == 2
        assert store.file(owner, bot, saved["id"])["query_title"] == "Sales by site"
        assert store.file(owner, bot, corrected["id"])["query_title"] == "Updated sales by site"
        assert present()["id"] == corrected["id"]
        corrected_path = path.replace(saved["id"], corrected["id"])
        assert client.get(corrected_path).json()["rows"] == [["South", 7], ["North", 4]]
        assert client.get(path).json()["rows"] == [["North", 4], ["South", 7]]
        args.update(sql="SELECT qty FROM sales")
        with pytest.raises(Conflict):
            present()
        args.update(previous_file_id=source["id"])
        with pytest.raises(Conflict, match="Only a saved data result"):
            present()
        assert (
            client.post(path, json={"sql": args["sql"], "file_ids": [source["id"]]}).status_code
            == 422
        )
        assert client.get(path.replace(bot, other)).status_code == 404
        fake = store.save_file(owner, bot, FileInput(name="fake.owlquery.json", content="{}"))
        assert client.get(path.replace(saved["id"], fake["id"])).status_code == 404
        store.delete_item(owner, bot, source["id"], kind="files")
        assert client.post(path, json={"sql": args["sql"]}).status_code == 404


def owl(tmp_path, *, files_enabled=True):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "owner", BotInput(name="Λογιστής", purpose="Tables", files_enabled=files_enabled)
    )
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message="Πόσα πουλήσαμε;", request_id="tables-1"))
    return store, bot, store.claim("owner")


def query(runtime, task, file_ids, sql, **extra):
    return runtime._tool(
        task,
        TABLE_TOOL,
        {"file_ids": file_ids, "sql": sql, **extra},
        TOOLS,
        image_input=False,
        token=runtime.task_token(task["id"]),
    )


def text_file(store, bot, name, content):
    return store.save_file("owner", bot["id"], FileInput(name=name, content=content))


def test_saved_query_revision_shows_executed_rows_and_distinguishes_a_retitle(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    row = "Ginestra,5"
    source = text_file(store, bot, "Staff.csv", "site,units\n" + row)
    args = {"title": "Initial", "sql": "SELECT site,units FROM staff", "file_ids": [source["id"]]}

    def present(**changes):
        return runtime._tool(task, "present_table", {**args, **changes}, ["present_table"])

    first = present()
    assert first["executed_sample"] == {"columns": ["site", "units"], "rows": [["Ginestra", 5]]}
    with pytest.raises(Conflict, match="same rows") as unchanged:
        present(title="Ginestra 10", previous_file_id=first["id"])
    assert '"Ginestra",5' in str(unchanged.value)
    assert store.file("owner", bot["id"], first["id"])["is_latest"]
    retitled = present(title="Stato iniziale", previous_file_id=first["id"], keep_rows=True)
    assert retitled["revision"] == 2 and retitled["executed_sample"] == first["executed_sample"]
    changed_source = store.save_file(
        "owner",
        bot["id"],
        FileInput(
            name="Staff.csv",
            content="site,units\n" + row + "\nTorino,2",
            previous_file_id=source["id"],
        ),
    )
    maintained = present(
        previous_file_id=retitled["id"],
        file_ids=[changed_source["id"]],
        sql="SELECT site,units FROM staff WHERE site='Ginestra'",
    )
    assert maintained["revision"] == 3 and maintained["executed_sample"] == first["executed_sample"]


def test_visible_query_can_be_revised_past_a_withdrawn_successor_without_rebinding_its_sources(
    tmp_path,
):
    store, bot, task = owl(tmp_path)
    source = text_file(store, bot, "Units.csv", "site,units\nNorth,3\nSouth,5")
    runtime = BotRuntime(store, "owner", lambda _: None)

    def present(sql, previous=None):
        return runtime._tool(
            task,
            "present_table",
            {
                "title": "Units",
                "sql": sql,
                "file_ids": [source["id"]],
                "previous_file_id": previous,
            },
            ["present_table"],
        )

    first = present("SELECT site,units FROM units ORDER BY site")
    second = present("SELECT site,units FROM units WHERE units>3", first["id"])
    store.withdraw_task_result("owner", task["id"], second["id"])
    statement = "SELECT SUM(units) AS total FROM units"
    corrected = present(statement, first["id"])
    assert corrected["series_id"] == first["series_id"] and corrected["revision"] == 3
    assert corrected["executed_sample"]["rows"] == [[8]]
    assert present(statement, first["id"])["id"] == corrected["id"]
    assert store.query_file_sources("owner", bot["id"], second["id"]) == [source["id"]]
    with pytest.raises(Conflict):
        present("SELECT COUNT(*) FROM units", first["id"])


def workbook(sheets: dict[str, list[list]]) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()


SALES = "Πόλη;Ποσό (€);Μήνας\nΑθήνα;120.50;1\nΘεσσαλονίκη;80;1\nΑθήνα;30;2\nΒόλος;;2\n"


def test_a_delimited_text_file_is_one_table_with_exact_totals(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    sales = text_file(store, bot, "Πωλήσεις 2026.csv", SALES)
    result = query(
        runtime,
        task,
        [sales["id"]],
        "SELECT πόλη, sum(ποσό) AS total FROM πωλήσεις_2026 GROUP BY πόλη ORDER BY total DESC",
    )
    assert result["columns"] == ["πόλη", "total"]
    assert result["rows"] == [["Αθήνα", 150.5], ["Θεσσαλονίκη", 80.0], ["Βόλος", None]]
    assert result["rows_total"] == 3
    [table] = result["tables"]
    assert table["table"] == "πωλήσεις_2026"
    assert table["rows"] == 4
    # Simple names to write, with the original headers beside them.
    assert table["columns"] == [
        {"name": "πόλη", "type": "TEXT", "header": "Πόλη"},
        {"name": "ποσό", "type": "REAL", "header": "Ποσό (€)"},
        {"name": "μήνας", "type": "INTEGER", "header": "Μήνας"},
    ]
    assert "Quote numbers as they are" in result["note"]


def test_a_workbook_gives_a_table_per_sheet_and_joins_with_other_files(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    book = store.save_bytes(
        "owner",
        bot["id"],
        "Orders.xlsx",
        workbook(
            {
                "2025": [["Customer", "Amount"], ["Ana", 10], ["李", 5]],
                "2026": [["Customer", "Amount"], ["Ana", 7], [None, None], ["李", 9.5]],
            }
        ),
    )
    names = text_file(store, bot, "customers.tsv", "Customer\tCountry\nAna\tES\n李\tCN\n")
    result = query(
        runtime,
        task,
        [book["id"], names["id"]],
        "SELECT c.country, sum(o.amount) AS total FROM orders_2026 o "
        "JOIN customers c ON c.customer = o.customer GROUP BY c.country ORDER BY c.country",
    )
    assert result["rows"] == [["CN", 9.5], ["ES", 7.0]]
    tables = {table["table"]: table for table in result["tables"]}
    assert set(tables) == {"orders_2025", "orders_2026", "customers"}
    assert tables["orders_2026"]["sheet"] == "2026"
    assert tables["orders_2026"]["rows"] == 2  # the empty row is left out


def test_a_workbook_with_one_sheet_is_named_after_its_file(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    book = store.save_bytes(
        "owner",
        bot["id"],
        "Ventas Q1.xlsx",
        workbook({"Hoja1": [["Región", "Importe"], ["Norte", 3], ["Sur", 4]]}),
    )
    result = query(runtime, task, [book["id"]], "SELECT count(*), sum(importe) FROM ventas_q1")
    assert result["rows"] == [[2, 7]]
    assert result["tables"][0]["sheet"] == "Hoja1"


def test_a_json_list_of_objects_is_a_table(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    items = [{"name": "Α", "price": 2.5, "tags": ["x"]}, {"name": "B", "stock": 3}]
    data = text_file(store, bot, "items.json", json.dumps(items, ensure_ascii=False))
    result = query(runtime, task, [data["id"]], "SELECT name, price, stock, tags FROM items")
    assert result["rows"] == [["Α", 2.5, None, '["x"]'], ["B", None, 3, None]]


def test_numbers_are_numbers_only_when_every_value_reads_as_one(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    content = 'a,b,c,d\n1,1.5,1.234,x\n2,2,"1.234,56",3\n'
    data = text_file(store, bot, "mixed.csv", content)
    result = query(runtime, task, [data["id"]], "SELECT typeof(a), typeof(b), c, d FROM mixed")
    assert result["rows"][1] == ["integer", "real", "1.234,56", "3"]
    types = [column["type"] for column in result["tables"][0]["columns"]]
    assert types == ["INTEGER", "REAL", "TEXT", "TEXT"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sales",
        "INSERT INTO sales VALUES ('x', 1, 1)",
        "CREATE TABLE copy AS SELECT * FROM sales",
        "PRAGMA table_info(sales)",
        "ATTACH DATABASE 'stolen.sqlite3' AS other",
    ],
)
def test_only_reading_is_allowed(tmp_path, monkeypatch, sql):
    monkeypatch.chdir(tmp_path)
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = text_file(store, bot, "sales.csv", "city,amount,month\nA,1,1\n")
    with pytest.raises(Conflict, match="Only reading is allowed"):
        query(runtime, task, [data["id"]], sql)
    assert not (Path(tmp_path) / "stolen.sqlite3").exists()
    # The table is unchanged for the next query.
    assert query(runtime, task, [data["id"]], "SELECT count(*) FROM sales")["rows"] == [[1]]


def test_a_failed_query_names_the_tables_and_columns(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = text_file(store, bot, "Πωλήσεις.csv", SALES)
    with pytest.raises(Conflict) as failure:
        query(runtime, task, [data["id"]], "SELECT amount FROM πωλήσεις")
    message = str(failure.value)
    assert "no such column: amount" in message
    assert "πωλήσεις(πόλη, ποσό, μήνας)" in message


def test_two_statements_are_refused(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = text_file(store, bot, "t.csv", "a\n1\n")
    with pytest.raises(Conflict, match="The query failed"):
        query(runtime, task, [data["id"]], "SELECT 1; SELECT 2")


def test_long_results_are_cut_and_counted(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    rows = "\n".join(f"{n},{'κείμενο ' * 200}" for n in range(1, 1001))
    # Stored as bytes, as an upload is: its UTF-8 text is read as the table.
    data = store.save_bytes("owner", bot["id"], "long.csv", ("n,text\n" + rows).encode())
    few = query(runtime, task, [data["id"]], "SELECT n FROM long", max_rows=10)
    assert few["rows_shown"] == 10 and few["rows_total"] == 1000
    assert few["note"].startswith("Only the first 10 rows are shown")
    wide = query(runtime, task, [data["id"]], "SELECT * FROM long", max_rows=500)
    shown = json.dumps(wide["rows"], ensure_ascii=False)
    assert len(shown) <= owl_tables.MAX_RESULT_CHARS + owl_tables.MAX_VALUE_CHARS * 4
    assert wide["rows_shown"] < 500 and wide["rows_total"] == 1000
    assert wide["rows"][0][1].endswith("…")


def test_a_query_that_runs_too_long_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(owl_tables, "QUERY_SECONDS", 0.2)
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = text_file(store, bot, "t.csv", "a\n1\n")
    endless = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT max(x) FROM n"
    with pytest.raises(Conflict, match="was stopped"):
        query(runtime, task, [data["id"]], endless)


def test_stop_ends_a_running_query(tmp_path):
    store, bot, task = owl(tmp_path)
    data = text_file(store, bot, "t.csv", "a\n1\n")
    stop = threading.Event()
    tables = OwlTables(store, "owner", EventCancellationToken(stop))
    threading.Timer(0.2, stop.set).start()
    endless = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT max(x) FROM n"
    with pytest.raises(CooperativeCancellationError):
        tables.call(task, TableQuery(file_ids=[data["id"]], sql=endless))


def test_files_that_are_not_tables_are_explained(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    picture = io.BytesIO()
    Image.new("RGB", (4, 4)).save(picture, format="PNG")
    image = store.save_bytes("owner", bot["id"], "table.csv", picture.getvalue())
    with pytest.raises(Conflict, match="isn't a table this tool can read"):
        query(runtime, task, [image["id"]], "SELECT 1")
    empty = text_file(store, bot, "empty.csv", "\n\n")
    with pytest.raises(Conflict, match="has no rows"):
        query(runtime, task, [empty["id"]], "SELECT 1")


def test_files_off_refuses(tmp_path):
    store, bot, task = owl(tmp_path, files_enabled=False)
    tables = OwlTables(store, "owner", EventCancellationToken(threading.Event()))
    with pytest.raises(Conflict, match="Files are turned off"):
        tables.call(task, TableQuery(file_ids=["x"], sql="SELECT 1"))


def test_too_many_values_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(owl_tables, "MAX_CELLS", 10)
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    data = text_file(store, bot, "t.csv", "a,b\n" + "1,2\n" * 6)
    with pytest.raises(Conflict, match="more than 10 values"):
        query(runtime, task, [data["id"]], "SELECT 1")


def test_names_are_simple_in_every_script():
    assert simple_name("Πωλήσεις 2026", "t") == "πωλήσεις_2026"
    assert simple_name("2026 Umsätze (€)", "t") == "t_2026_umsätze"
    assert simple_name("销售额", "t") == "销售额"
    assert simple_name("  ---  ", "column_3") == "column_3"
    assert simple_name("ＡＢＣ", "t") == "abc"  # full-width letters, NFKC


def test_the_input_is_bounded():
    with pytest.raises(ValidationError):
        TableQuery(file_ids=[], sql="SELECT 1")
    with pytest.raises(ValidationError):
        TableQuery(file_ids=["a"] * 9, sql="SELECT 1")
    with pytest.raises(ValidationError):
        TableQuery(file_ids=["a"], sql="SELECT 1", max_rows=501)


def test_offered_with_files_counted_as_reading_and_schema_is_plain():
    with_files = owl_toolset.tool_names(owl_toolset.Turn(answers_person=True, files=True))
    without = owl_toolset.tool_names(owl_toolset.Turn(answers_person=True, files=False))
    assert TABLE_TOOL in with_files and TABLE_TOOL not in without
    review = owl_toolset.tool_names(
        owl_toolset.Turn(answers_person=False, activation=True, files=True)
    )
    assert TABLE_TOOL in review
    assert TABLE_TOOL in step_effects.READING
    schema = tool_schema(TABLE_TOOL)["function"]["parameters"]
    assert "anyOf" not in schema and schema["required"] == ["file_ids", "sql"]
