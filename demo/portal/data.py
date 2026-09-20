"""Deterministic data for the bundled demo marina portal.

Every value is derived from the month string, so two runs of the same flow on
the same month always produce byte-identical rows. That is what makes
`portalc explore`'s twice-over dry run a meaningful determinism proof.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

BERTHS = ["A-02", "A-07", "B-11", "C-03", "C-19", "D-14", "D-21", "E-05"]
VESSELS = [
    "Gulet Anima Maris",
    "Gulet Lady Gita",
    "Gulet Navilux",
    "S/Y Perla",
    "M/Y Adriana",
    "Gulet Malena",
]
STATUSES = ["paid", "open", "open", "paid", "overdue"]
VAT_RATE = 0.22


def _digest(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "big")


@dataclass(frozen=True)
class Invoice:
    number: str
    issued_on: str
    due_on: str
    berth: str
    vessel: str
    net_eur: float
    vat_eur: float
    total_eur: float
    currency: str
    status: str


def invoices_for_month(month: str) -> list[Invoice]:
    """Return the invoice rows a partner account sees for `month` (YYYY-MM)."""
    year, mon = (int(p) for p in month.split("-"))
    seed = _digest("invoices", month)
    count = 6 + seed % 5

    rows: list[Invoice] = []
    for i in range(count):
        s = _digest(month, str(i))
        day = 1 + (s % 26)
        issued = date(year, mon, day)
        due = issued + timedelta(days=14)
        net = round(320 + (s >> 8) % 4200 + ((s >> 20) % 100) / 100, 2)
        vat = round(net * VAT_RATE, 2)
        total = round(net + vat, 2)
        rows.append(
            Invoice(
                number=f"{year}-{(seed % 400) + i * 7:06d}",
                issued_on=issued.isoformat(),
                due_on=due.isoformat(),
                berth=BERTHS[(s >> 3) % len(BERTHS)],
                vessel=VESSELS[(s >> 11) % len(VESSELS)],
                net_eur=net,
                vat_eur=vat,
                total_eur=total,
                currency="EUR",
                status=STATUSES[(s >> 17) % len(STATUSES)],
            )
        )
    rows.sort(key=lambda r: (r.issued_on, r.number))
    return rows


@dataclass(frozen=True)
class Berth:
    code: str
    length_m: float
    available_from: str
    price_per_night_eur: float


def berths_for_month(month: str) -> list[Berth]:
    year, mon = (int(p) for p in month.split("-"))
    out: list[Berth] = []
    for code in BERTHS:
        s = _digest("berth", month, code)
        out.append(
            Berth(
                code=code,
                length_m=round(10 + (s % 220) / 10, 1),
                available_from=date(year, mon, 1 + (s >> 5) % 27).isoformat(),
                price_per_night_eur=round(45 + (s >> 9) % 210 + ((s >> 24) % 100) / 100, 2),
            )
        )
    return out
