"""JSON Schema for a compiled flow, plus header-contract comparison."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

from .errors import Finding, PortalDriftError
from .extract import Column, ExtractedTable


def canonical_json(payload: Any) -> str:
    """Byte-stable JSON for hashing: sorted keys, no incidental whitespace.

    Two files that mean the same schema hash the same, so re-indenting a
    committed artifact is not reported as drift — while any change to a field
    name, type or header is.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def content_fingerprint(payload: Any) -> str:
    """`sha256:<hex>` over the canonical form of a flow, schema or lockfile."""
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def schema_from_table(table: ExtractedTable, title: str) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    headers: dict[str, str] = {}
    for col in table.columns:
        json_type = {"number": "number", "date": "string", "string": "string"}[col.type]
        prop: dict[str, Any] = {"type": [json_type, "null"] if not col.required else json_type}
        if col.type == "date":
            prop["format"] = "date"
        properties[col.name] = prop
        headers[col.name] = col.header
        if col.required:
            required.append(col.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
        "x-portalc": {
            "headers": headers,
            "marker": table.marker,
            "selector": table.selector,
        },
    }


def field_rows(schema: dict[str, Any], sample_row: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items = schema.get("items", {})
    required = set(items.get("required") or [])
    headers = (schema.get("x-portalc") or {}).get("headers") or {}
    out = []
    for name, prop in (items.get("properties") or {}).items():
        typ = prop.get("type", "string")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), "string")
        if prop.get("format") == "date":
            typ = "date"
        sample = ""
        if sample_row and name in sample_row:
            sample = str(sample_row[name])
        out.append(
            {
                "name": name,
                "type": typ,
                "required": name in required,
                "sample": sample,
                "header": headers.get(name, ""),
            }
        )
    return out


def header_findings(schema: dict[str, Any], table: ExtractedTable) -> list[Finding]:
    expected: dict[str, str] = (schema.get("x-portalc") or {}).get("headers") or {}
    live = {c.name: c.header for c in table.columns}
    findings: list[Finding] = []

    missing = [name for name in expected if name not in live]
    extra = [name for name in live if name not in expected]
    renamed = []
    for name, header in expected.items():
        if name in live and live[name] != header:
            renamed.append((name, header, live[name]))

    if not missing and not extra and not renamed:
        findings.append(
            Finding(
                check="headers",
                ok=True,
                message=f"{len(expected)} column headers match the contract",
            )
        )
        return findings

    if renamed:
        bits = [f"{n}: {old!r} → {new!r}" for n, old, new in renamed]
        findings.append(
            Finding(
                check="headers.renamed",
                ok=False,
                message="; ".join(bits),
                data={"renamed": [{"field": n, "from": o, "to": n2} for n, o, n2 in renamed]},
            )
        )
    if missing:
        findings.append(
            Finding(
                check="headers.missing",
                ok=False,
                message="missing fields: " + ", ".join(missing),
                data={"missing": missing},
            )
        )
    if extra:
        findings.append(
            Finding(
                check="headers.extra",
                ok=False,
                message="new fields: " + ", ".join(extra),
                severity="warn",
                data={"extra": extra},
            )
        )
    return findings


def validate_rows(schema: dict[str, Any], rows: list[dict[str, Any]]) -> list[Finding]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(rows), key=lambda e: list(e.path))
    if not errors:
        return [Finding(check="schema", ok=True, message=f"{len(rows)} rows match {schema.get('title', 'schema')}")]
    # Collapse to field-level counts so the first line of output is the field.
    by_field: dict[str, int] = {}
    messages: list[str] = []
    for err in errors[:12]:
        field = next((str(p) for p in err.path if isinstance(p, str)), "(root)")
        by_field[field] = by_field.get(field, 0) + 1
        messages.append(err.message)
    top = max(by_field, key=by_field.get) if by_field else "(root)"
    return [
        Finding(
            check="schema",
            ok=False,
            message=f"{top} failed in {by_field.get(top, 0)} row(s)"
            + (f" — {messages[0]}" if messages else ""),
            data={"byField": by_field, "sample": messages[:5]},
        )
    ]


def raise_drift(findings: list[Finding]) -> None:
    failed = [f for f in findings if not f.ok and f.severity == "error"]
    if not failed:
        return
    lead = failed[0]
    raise PortalDriftError(
        lead.message,
        [f.message for f in failed],
        hint="portalc repair will re-read the page and open a patch if the change is a rename",
        data={"findings": [f.as_dict() for f in findings]},
    )


def columns_as_dicts(columns: list[Column]) -> list[dict[str, Any]]:
    return [
        {
            "name": c.name,
            "type": c.type,
            "required": c.required,
            "sample": c.sample,
            "header": c.header,
        }
        for c in columns
    ]
