import pytest

from portalc.errors import PortalDriftError
from portalc.extract import extract_table
from portalc.schemautil import (
    columns_as_dicts,
    field_rows,
    header_findings,
    raise_drift,
    schema_from_table,
    validate_rows,
)


def table(*, total_header: str = "Total (EUR)", extra: str = "", note: str = "late fee"):
    html = f"""
    <html><body>
    <table data-portalc="invoices">
      <thead><tr>
        <th data-col="number">Invoice no.</th>
        <th data-col="issued_on">Issued</th>
        <th data-col="total_eur">{total_header}</th>
        <th data-col="note">Note</th>
        {extra}
      </tr></thead>
      <tbody>
        <tr><td>2026-000001</td><td>2026-09-01</td><td>1,840.00</td><td>{note}</td>{'<td>x</td>' if extra else ''}</tr>
        <tr><td>2026-000002</td><td>2026-09-02</td><td>200.50</td><td></td>{'<td>y</td>' if extra else ''}</tr>
      </tbody>
    </table>
    </body></html>
    """
    return extract_table(html, marker="invoices")


def test_schema_is_an_array_of_objects_with_no_room_for_surprises():
    schema = schema_from_table(table(), "Invoice")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "array"
    assert schema["minItems"] == 1
    assert schema["items"]["additionalProperties"] is False
    assert schema["title"] == "Invoice"


def test_column_types_and_required_flags_come_from_the_live_rows():
    schema = schema_from_table(table(), "Invoice")
    props = schema["items"]["properties"]
    assert props["total_eur"]["type"] == "number"
    assert props["issued_on"] == {"type": "string", "format": "date"}
    assert props["note"]["type"] == ["string", "null"]
    assert "note" not in schema["items"]["required"]
    assert set(schema["items"]["required"]) == {"number", "issued_on", "total_eur"}


def test_schema_records_the_header_contract_under_x_portalc():
    schema = schema_from_table(table(), "Invoice")
    meta = schema["x-portalc"]
    assert meta["headers"]["total_eur"] == "Total (EUR)"
    assert meta["marker"] == "invoices"
    assert meta["selector"] == "[data-portalc='invoices']"


def test_field_rows_render_the_review_table_shown_after_explore():
    t = table()
    rows = field_rows(schema_from_table(t, "Invoice"), t.rows[0])
    by_name = {r["name"]: r for r in rows}
    assert by_name["issued_on"]["type"] == "date"
    assert by_name["total_eur"]["sample"] == "1840.0"
    assert by_name["note"]["required"] is False
    assert by_name["total_eur"]["header"] == "Total (EUR)"


def test_matching_headers_produce_one_green_finding():
    t = table()
    findings = header_findings(schema_from_table(t, "Invoice"), t)
    assert len(findings) == 1
    assert findings[0].ok
    assert findings[0].check == "headers"


def test_a_renamed_title_is_an_error_naming_both_titles():
    schema = schema_from_table(table(), "Invoice")
    findings = header_findings(schema, table(total_header="Amount (EUR)"))
    renamed = next(f for f in findings if f.check == "headers.renamed")
    assert not renamed.ok
    assert renamed.severity == "error"
    assert "Total (EUR)" in renamed.message and "Amount (EUR)" in renamed.message
    assert renamed.data["renamed"][0]["field"] == "total_eur"


def test_a_disappeared_column_is_an_error():
    schema = schema_from_table(table(extra='<th data-col="vat_eur">VAT (EUR)</th>'), "Invoice")
    findings = header_findings(schema, table())
    missing = next(f for f in findings if f.check == "headers.missing")
    assert not missing.ok
    assert missing.data["missing"] == ["vat_eur"]


def test_a_new_column_is_a_warning_not_a_failure():
    schema = schema_from_table(table(), "Invoice")
    findings = header_findings(schema, table(extra='<th data-col="vat_eur">VAT (EUR)</th>'))
    extra = next(f for f in findings if f.check == "headers.extra")
    assert extra.severity == "warn"
    assert extra.data["extra"] == ["vat_eur"]


def test_rows_that_match_the_schema_produce_one_green_finding():
    t = table()
    findings = validate_rows(schema_from_table(t, "Invoice"), t.rows)
    assert len(findings) == 1 and findings[0].ok
    assert findings[0].check == "schema"


def test_a_type_change_is_reported_against_the_field_that_broke():
    t = table()
    schema = schema_from_table(t, "Invoice")
    rows = [dict(t.rows[0]), dict(t.rows[1])]
    rows[0]["total_eur"] = "1 840,00"
    findings = validate_rows(schema, rows)
    assert not findings[0].ok
    assert findings[0].message.startswith("total_eur failed in 1 row(s)")
    assert findings[0].data["byField"] == {"total_eur": 1}


def test_an_undeclared_field_fails_validation():
    t = table()
    schema = schema_from_table(t, "Invoice")
    rows = [{**t.rows[0], "surprise": 1}]
    assert not validate_rows(schema, rows)[0].ok


def test_an_empty_result_set_fails_min_items():
    schema = schema_from_table(table(), "Invoice")
    assert not validate_rows(schema, [])[0].ok


def test_raise_drift_is_silent_when_nothing_failed():
    t = table()
    raise_drift(header_findings(schema_from_table(t, "Invoice"), t))


def test_raise_drift_ignores_warnings_and_raises_on_errors():
    schema = schema_from_table(table(), "Invoice")
    warn_only = header_findings(schema, table(extra='<th data-col="vat_eur">VAT (EUR)</th>'))
    raise_drift(warn_only)

    with pytest.raises(PortalDriftError) as caught:
        raise_drift(header_findings(schema, table(total_header="Amount (EUR)")))
    assert caught.value.exit_code == 6
    assert "total_eur" in caught.value.title


def test_columns_as_dicts_carries_a_sample_for_the_review_table():
    rows = columns_as_dicts(table().columns)
    assert {r["name"] for r in rows} == {"number", "issued_on", "total_eur", "note"}
    assert next(r for r in rows if r["name"] == "number")["sample"] == "2026-000001"
