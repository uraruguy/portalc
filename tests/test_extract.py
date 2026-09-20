import pytest

from portalc.extract import extract_table, infer_type, slug


HTML = """
<html><body>
<table data-portalc="invoices">
  <thead><tr>
    <th data-col="number">Invoice no.</th>
    <th data-col="total_eur">Total (EUR)</th>
    <th data-col="status">Status</th>
  </tr></thead>
  <tbody>
    <tr><td>2026-000001</td><td class="num">1,840.00</td><td><span class="pill paid">paid</span></td></tr>
    <tr><td>2026-000002</td><td class="num">200.50</td><td><span class="pill open">open</span></td></tr>
  </tbody>
</table>
</body></html>
"""


def page(table_html: str) -> str:
    return f"<html><body>{table_html}</body></html>"


def test_extract_uses_data_col_and_header_text():
    table = extract_table(HTML, marker="invoices")
    assert table.field_names() == ["number", "total_eur", "status"]
    assert table.header_contract() == ["Invoice no.", "Total (EUR)", "Status"]
    assert table.columns[1].type == "number"
    assert table.rows[0]["total_eur"] == 1840.0
    assert table.rows[0]["status"] == "paid"
    assert table.rows[0]["number"] == "2026-000001"


def test_thousand_separators_are_parsed_as_numbers():
    table = extract_table(HTML, marker="invoices")
    assert [r["total_eur"] for r in table.rows] == [1840.0, 200.5]


def test_selector_and_marker_describe_where_the_rows_came_from():
    table = extract_table(HTML, marker="invoices")
    assert table.marker == "invoices"
    assert table.selector == "[data-portalc='invoices']"


def test_marker_picks_the_right_table_on_a_page_with_several():
    html = page(
        """
        <table data-portalc="berths">
          <thead><tr><th data-col="code">Berth</th></tr></thead>
          <tbody><tr><td>A-02</td></tr></tbody>
        </table>
        <table data-portalc="invoices">
          <thead><tr><th data-col="number">Invoice no.</th></tr></thead>
          <tbody><tr><td>2026-000001</td></tr></tbody>
        </table>
        """
    )
    assert extract_table(html, marker="invoices").field_names() == ["number"]
    assert extract_table(html, marker="berths").field_names() == ["code"]


def test_headers_without_data_col_fall_back_to_a_slug():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th>Invoice no.</th><th>Total (EUR)</th></tr></thead>
          <tbody><tr><td>2026-000001</td><td>10.00</td></tr></tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    assert table.field_names() == ["invoice_no", "total_eur"]
    assert table.header_contract() == ["Invoice no.", "Total (EUR)"]


def test_duplicate_headers_get_distinct_field_names():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th>Amount</th><th>Amount</th><th>Amount</th></tr></thead>
          <tbody><tr><td>1</td><td>2</td><td>3</td></tr></tbody>
        </table>
        """
    )
    assert extract_table(html, marker="invoices").field_names() == ["amount", "amount_2", "amount_3"]


def test_a_blank_cell_makes_the_column_optional_and_the_value_null():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th data-col="number">Invoice no.</th><th data-col="note">Note</th></tr></thead>
          <tbody>
            <tr><td>2026-000001</td><td>late fee</td></tr>
            <tr><td>2026-000002</td><td></td></tr>
          </tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    note = next(c for c in table.columns if c.name == "note")
    assert note.required is False
    assert table.rows[1]["note"] is None
    assert table.columns[0].required is True


def test_iso_dates_are_typed_date():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th data-col="issued_on">Issued</th></tr></thead>
          <tbody><tr><td>2026-09-01</td></tr><tr><td>2026-09-14</td></tr></tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    assert table.columns[0].type == "date"
    assert table.rows[0]["issued_on"] == "2026-09-01"


def test_a_mixed_column_stays_a_string():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th data-col="amount">Amount</th></tr></thead>
          <tbody><tr><td>12.00</td></tr><tr><td>on request</td></tr></tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    assert table.columns[0].type == "string"
    assert table.rows[0]["amount"] == "12.00"


def test_short_rows_are_padded_so_every_row_has_every_field():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th data-col="number">No.</th><th data-col="note">Note</th></tr></thead>
          <tbody><tr><td>2026-000001</td></tr></tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    assert set(table.rows[0]) == {"number", "note"}
    assert table.rows[0]["note"] is None


def test_a_missing_marker_is_an_error_not_an_empty_result():
    with pytest.raises(ValueError):
        extract_table(HTML, marker="berths")


def test_a_page_without_any_table_is_an_error():
    with pytest.raises(ValueError):
        extract_table("<html><body><p>signed out</p></body></html>", marker="invoices")


def test_a_table_with_no_rows_at_all_is_an_error():
    html = page('<table data-portalc="invoices"></table>')
    with pytest.raises(ValueError):
        extract_table(html, marker="invoices")


def test_header_only_table_extracts_the_contract_and_no_rows():
    html = page(
        """
        <table data-portalc="invoices">
          <thead><tr><th data-col="number">Invoice no.</th></tr></thead>
          <tbody></tbody>
        </table>
        """
    )
    table = extract_table(html, marker="invoices")
    assert table.header_contract() == ["Invoice no."]
    assert table.rows == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-01", "date"),
        ("1,840.00", "number"),
        ("-12", "number"),
        ("12.5", "number"),
        ("EUR", "string"),
        ("", "string"),
        ("2026-9-1", "string"),
    ],
)
def test_infer_type(text, expected):
    assert infer_type(text) == expected


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Total (EUR)", "total_eur"),
        ("Invoice no.", "invoice_no"),
        ("  Due  ", "due"),
        ("€", "field"),
    ],
)
def test_slug(header, expected):
    assert slug(header) == expected
