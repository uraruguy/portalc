#!/usr/bin/env python3
"""Bundled demo portal: a fake marina partner portal with a login wall.

It exists so the whole portalc pipeline — explore, build, typed call, verify,
drift, repair — can be demonstrated without pointing at anyone's real system.
Standard library only: no dependency has to be installed to run it.

    python3 demo/portal/server.py --port 8787

Credentials come from DEMO_MARINA_USER / DEMO_MARINA_PASS and fall back to the
public demo pair. They are demo credentials for a local process, not secrets.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import berths_for_month, invoices_for_month  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent / ".state.json"

DEFAULT_LABELS = {
    "number": "Invoice no.",
    "issued_on": "Issued",
    "due_on": "Due",
    "berth": "Berth",
    "vessel": "Vessel",
    "net_eur": "Net (EUR)",
    "vat_eur": "VAT (EUR)",
    "total_eur": "Total (EUR)",
    "currency": "Currency",
    "status": "Status",
}
INVOICE_COLUMNS = list(DEFAULT_LABELS)

BERTH_LABELS = {
    "code": "Berth",
    "length_m": "Length (m)",
    "available_from": "Available from",
    "price_per_night_eur": "Price / night (EUR)",
}

DEMO_USER = os.environ.get("DEMO_MARINA_USER", "skipper@demo-marina.test")
DEMO_PASS = os.environ.get("DEMO_MARINA_PASS", "demo-portal-password")

_sessions: set[str] = set()
_lock = threading.Lock()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"columnLabels": {}, "mutations": []}


def labels() -> dict[str, str]:
    merged = dict(DEFAULT_LABELS)
    merged.update(load_state().get("columnLabels", {}))
    return merged


CSS = """
:root { --ink:#101828; --muted:#667085; --line:#e4e7ec; --accent:#0b5cad; --bg:#f7f9fc; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       color:var(--ink); background:var(--bg); }
header { background:#fff; border-bottom:1px solid var(--line); padding:14px 28px;
         display:flex; align-items:center; gap:16px; }
header .brand { font-weight:650; letter-spacing:-.01em; }
header .brand span { color:var(--accent); }
header nav { margin-left:auto; display:flex; gap:18px; font-size:14px; }
header nav a { color:var(--muted); text-decoration:none; }
header nav a:hover { color:var(--accent); }
main { max-width:1080px; margin:32px auto; padding:0 28px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.02em; }
.sub { color:var(--muted); font-size:14px; margin:0 0 22px; }
.card { background:#fff; border:1px solid var(--line); border-radius:10px; overflow:hidden; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-weight:600; font-size:12px; letter-spacing:.04em;
     text-transform:uppercase; color:var(--muted); background:#fbfcfe;
     padding:11px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:11px 14px; border-bottom:1px solid #f1f3f7; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
th.num { text-align:right; }
.pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:12px; font-weight:550; }
.pill.paid { background:#e7f6ed; color:#0a6a37; }
.pill.open { background:#eef2ff; color:#3538cd; }
.pill.overdue { background:#fdecec; color:#b42318; }
form.login { max-width:380px; margin:64px auto; background:#fff; border:1px solid var(--line);
             border-radius:12px; padding:28px; }
form.login label { display:block; font-size:13px; color:var(--muted); margin:14px 0 5px; }
form.login input { width:100%; padding:10px 12px; border:1px solid var(--line);
                   border-radius:7px; font-size:14px; }
form.login button { margin-top:20px; width:100%; padding:11px; border:0; border-radius:7px;
                    background:var(--accent); color:#fff; font-size:15px; font-weight:550;
                    cursor:pointer; }
.filter { display:flex; gap:10px; align-items:center; margin-bottom:14px; font-size:14px; }
.filter input { padding:7px 10px; border:1px solid var(--line); border-radius:7px; }
.filter button { padding:7px 14px; border:1px solid var(--line); background:#fff;
                 border-radius:7px; cursor:pointer; }
.err { background:#fdecec; color:#b42318; padding:10px 12px; border-radius:7px;
       font-size:14px; margin-bottom:8px; }
footer { color:var(--muted); font-size:12px; text-align:center; margin:36px 0 48px; }
"""


def page(title: str, body: str, user: str | None = None) -> bytes:
    nav = ""
    if user:
        nav = (
            '<nav><a href="/invoices">Invoices</a><a href="/berths">Berths</a>'
            f'<a href="/logout">Sign out</a>'
            f'<span data-portalc="session-user">{html.escape(user)}</span></nav>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)} · Demo Marina</title>
<style>{CSS}</style></head><body>
<header><div class="brand">Demo Marina <span>Partner Portal</span></div>{nav}</header>
<main>{body}</main>
<footer>Demo portal bundled with portalc — synthetic data, no real marina.</footer>
</body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoMarina/1.0"

    def log_message(self, fmt, *args):  # quieter output
        if os.environ.get("DEMO_MARINA_VERBOSE"):
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------
    def _session(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "marina_session":
                with _lock:
                    return value if value in _sessions else None
        return None

    def _send(self, body: bytes, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, headers: dict | None = None):
        h = {"Location": to}
        h.update(headers or {})
        self._send(b"", 302, h)

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)

        if route == "/healthz":
            self._send(b'{"ok":true}', 200, {"Content-Type": "application/json"})
            return
        if route == "/login":
            self._send(self._login_page())
            return
        if route == "/logout":
            sid = self._session()
            if sid:
                with _lock:
                    _sessions.discard(sid)
            self._redirect("/login", {"Set-Cookie": "marina_session=; Max-Age=0; Path=/"})
            return

        user = self._session()
        if not user:
            self._redirect("/login")
            return

        if route == "/":
            self._redirect("/invoices")
        elif route == "/invoices":
            self._send(self._invoices_page(query.get("month", ["2026-09"])[0]))
        elif route == "/berths":
            self._send(self._berths_page(query.get("month", ["2026-09"])[0]))
        else:
            self._send(page("Not found", "<h1>404</h1><p class='sub'>No such page.</p>"), 404)

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path.rstrip("/") != "/login":
            self._send(page("Not found", "<h1>404</h1>"), 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        if username == DEMO_USER and password == DEMO_PASS:
            sid = secrets.token_urlsafe(18)
            with _lock:
                _sessions.add(sid)
            self._redirect(
                "/invoices", {"Set-Cookie": f"marina_session={sid}; Path=/; HttpOnly"}
            )
        else:
            self._send(self._login_page("Wrong e-mail or password."), 401)

    # -- pages -----------------------------------------------------------
    def _login_page(self, error: str = "") -> bytes:
        err = f'<div class="err" data-portalc="login-error">{html.escape(error)}</div>' if error else ""
        body = f"""
<form class="login" method="post" action="/login" data-portalc="login-form">
  <h1>Sign in</h1>
  <p class="sub">Partner access to berth invoices and availability.</p>
  {err}
  <label for="username">E-mail</label>
  <input id="username" name="username" type="email" autocomplete="username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit" data-portalc="login-submit">Sign in</button>
</form>"""
        return page("Sign in", body)

    def _invoices_page(self, month: str) -> bytes:
        lab = labels()
        try:
            rows = invoices_for_month(month)
        except (ValueError, IndexError):
            rows = []
        numeric = {"net_eur", "vat_eur", "total_eur"}
        head = "".join(
            f'<th class="{"num" if c in numeric else ""}" data-col="{c}">{html.escape(lab[c])}</th>'
            for c in INVOICE_COLUMNS
        )
        body_rows = []
        for r in rows:
            cells = []
            for c in INVOICE_COLUMNS:
                value = getattr(r, c)
                if c == "status":
                    cells.append(f'<td><span class="pill {value}">{value}</span></td>')
                elif c in numeric:
                    cells.append(f'<td class="num">{value:,.2f}</td>')
                else:
                    cells.append(f"<td>{html.escape(str(value))}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        empty = '<tr><td colspan="10">No invoices for this month.</td></tr>'
        body = f"""
<h1>Invoices</h1>
<p class="sub">Berth and service invoices issued to your partner account.</p>
<form class="filter" method="get" action="/invoices">
  <label for="month">Month</label>
  <input id="month" name="month" value="{html.escape(month)}" size="9">
  <button type="submit">Show</button>
</form>
<div class="card">
<table data-portalc="invoices">
  <thead><tr>{head}</tr></thead>
  <tbody>{"".join(body_rows) or empty}</tbody>
</table>
</div>"""
        return page("Invoices", body, user=DEMO_USER)

    def _berths_page(self, month: str) -> bytes:
        try:
            rows = berths_for_month(month)
        except (ValueError, IndexError):
            rows = []
        cols = list(BERTH_LABELS)
        numeric = {"length_m", "price_per_night_eur"}
        head = "".join(
            f'<th class="{"num" if c in numeric else ""}" data-col="{c}">{html.escape(BERTH_LABELS[c])}</th>'
            for c in cols
        )
        body_rows = []
        for r in rows:
            cells = []
            for c in cols:
                value = getattr(r, c)
                if c in numeric:
                    cells.append(f'<td class="num">{value:,.2f}</td>')
                else:
                    cells.append(f"<td>{html.escape(str(value))}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        body = f"""
<h1>Berth availability</h1>
<p class="sub">Availability and nightly price for {html.escape(month)}.</p>
<div class="card">
<table data-portalc="berths">
  <thead><tr>{head}</tr></thead>
  <tbody>{"".join(body_rows)}</tbody>
</table>
</div>"""
        return page("Berths", body, user=DEMO_USER)


def _addr_in_use(exc: OSError) -> bool:
    import errno

    busy = {errno.EADDRINUSE}
    if hasattr(errno, "WSAEADDRINUSE"):
        busy.add(errno.WSAEADDRINUSE)
    return exc.errno in busy


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the bundled demo marina portal.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DEMO_MARINA_PORT", "8787")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if _addr_in_use(exc):
            print(f"✗ port {args.port} is already in use", file=sys.stderr)
            print(f"  something else is bound to {args.host}:{args.port}", file=sys.stderr)
            print(file=sys.stderr)
            print(f"  try  lsof -i :{args.port}     # then kill the old process", file=sys.stderr)
            print(f"  or   portalc demo --port {args.port + 1}", file=sys.stderr)
            print(
                "       and set baseUrl in portals/demo-marina/portal.yaml to the same origin",
                file=sys.stderr,
            )
            return 5
        raise
    print(f"demo marina portal → http://{args.host}:{args.port}  (user {DEMO_USER})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
