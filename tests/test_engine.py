"""The deterministic HTTP engine — the path every daily run takes, with no model in it."""

from __future__ import annotations

import pytest

from portalc.engine import HttpEngine, dump_evidence, rows_fingerprint
from portalc.errors import PortalAuthError, PortalDownError

from conftest import DEMO_PASS, DEMO_USER


@pytest.fixture
def engine(demo_portal: str) -> HttpEngine:
    return HttpEngine(demo_portal)


@pytest.fixture
def signed_in(engine: HttpEngine) -> HttpEngine:
    engine.login(DEMO_USER, DEMO_PASS)
    return engine


def test_a_signed_in_session_reads_the_invoice_table(signed_in: HttpEngine):
    result = signed_in.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")
    assert result.status == 200
    assert result.table.header_contract()[0] == "Invoice no."
    assert len(result.table.rows) >= 6


def test_the_session_cookie_is_reused_across_requests(signed_in: HttpEngine):
    assert [c.name for c in signed_in.jar] == ["marina_session"]
    first = signed_in.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")
    second = signed_in.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")
    assert first.table.rows == second.table.rows


def test_a_second_flow_on_the_same_session_reads_a_different_table(signed_in: HttpEngine):
    berths = signed_in.fetch_table("/berths", query={"month": "2026-09"}, marker="berths")
    assert berths.table.field_names() == ["code", "length_m", "available_from", "price_per_night_eur"]


def test_reading_without_signing_in_is_an_auth_error_not_an_empty_table(engine: HttpEngine):
    with pytest.raises(PortalAuthError) as caught:
        engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")
    assert caught.value.exit_code == 3
    assert "not authenticated" in caught.value.title


def test_the_wrong_password_is_an_auth_error(engine: HttpEngine):
    with pytest.raises(PortalAuthError) as caught:
        engine.login(DEMO_USER, "not-the-password")
    assert caught.value.exit_code == 3
    assert "DEMO_MARINA_PASS" in caught.value.hint


def test_signing_out_ends_the_session(signed_in: HttpEngine):
    signed_in.get("/logout")
    with pytest.raises(PortalAuthError):
        signed_in.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")


def test_a_page_without_the_expected_table_is_reported_as_down(signed_in: HttpEngine):
    with pytest.raises(PortalDownError) as caught:
        signed_in.fetch_table("/nowhere", marker="invoices")
    assert caught.value.exit_code == 5
    assert "could not find the expected table" in caught.value.title


def test_a_closed_port_is_reported_as_down_with_the_url(demo_portal: str):
    with pytest.raises(PortalDownError) as caught:
        HttpEngine("http://127.0.0.1:1").get("/invoices")
    assert caught.value.exit_code == 5
    assert "http://127.0.0.1:1" in caught.value.detail
    assert "portalc demo" in caught.value.hint


def test_urls_are_built_from_the_base_the_same_way_every_run(demo_portal: str):
    engine = HttpEngine(demo_portal + "/")
    assert engine._url("/invoices") == f"{demo_portal}/invoices"
    assert engine._url("invoices") == f"{demo_portal}/invoices"
    assert engine._url("/invoices", {"month": "2026-09"}) == f"{demo_portal}/invoices?month=2026-09"
    assert engine._url("http://elsewhere.test/x") == "http://elsewhere.test/x"


# -- evidence and determinism -------------------------------------------


def test_evidence_keeps_the_table_and_drops_the_styling():
    html = "<html><head><style>body{color:red}</style></head><body><table><tr><td>1</td></tr></table></body></html>"
    assert dump_evidence(html) == "<table><tr><td>1</td></tr></table>"


def test_evidence_falls_back_to_the_head_of_a_page_with_no_table():
    assert dump_evidence("<html>no table here</html>") == "<html>no table here</html>"


def test_the_fingerprint_ignores_key_order_but_not_row_order():
    a = [{"x": 1, "y": 2}]
    assert rows_fingerprint(a) == rows_fingerprint([{"y": 2, "x": 1}])
    assert rows_fingerprint([{"x": 1}, {"x": 2}]) != rows_fingerprint([{"x": 2}, {"x": 1}])


def test_the_same_month_fingerprints_identically_twice(signed_in: HttpEngine):
    runs = [
        signed_in.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices").table.rows
        for _ in range(2)
    ]
    assert rows_fingerprint(runs[0]) == rows_fingerprint(runs[1])


def test_different_months_are_different_data(signed_in: HttpEngine):
    def rows(month: str):
        return signed_in.fetch_table("/invoices", query={"month": month}, marker="invoices").table.rows

    assert rows_fingerprint(rows("2026-09")) != rows_fingerprint(rows("2026-10"))
