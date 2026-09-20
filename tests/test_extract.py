from portalc.extract import extract_table


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


def test_extract_uses_data_col_and_header_text():
    table = extract_table(HTML, marker="invoices")
    assert table.field_names() == ["number", "total_eur", "status"]
    assert table.header_contract() == ["Invoice no.", "Total (EUR)", "Status"]
    assert table.columns[1].type == "number"
    assert table.rows[0]["total_eur"] == 1840.0
    assert table.rows[0]["status"] == "paid"
    assert table.rows[0]["number"] == "2026-000001"
