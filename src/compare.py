"""Compare Primary and Shadow row data using DeepDiff and column mapping.

Scope: compares 3 tables (invoice, invoice_detail_item, payment_transaction).
No DB connections: expects data already extracted from primary and shadow; callers
pass in the fetched lists.

- Column handling and normalization: as per doc — date/datetime to ISO strings for safe diffing; column mapping aligns primary/shadow names.
- Comparison: DeepDiff on mapped row dicts.
- Output: structured list of (field, primary_val, shadow_val, diff_type) with
  types: values_changed, type_mismatch, only_in_primary, only_in_shadow.
- Matching: by invoice GUID; detail items and payments grouped by invoice GUID,
  then rows matched by business key when configured. Extra rows (in shadow only)
  and missing rows (in primary only) are noted as differences and included in
  the section report (counts and keys when available).
- compare_invoice_sections() returns per-invoice sections: invoice_diffs,
  detail_diffs, payment_diffs, missing_in_shadow, plus row-level summary counts.
"""

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional, Union

from deepdiff import DeepDiff

logger = logging.getLogger(__name__)

DIFFERENCE_TYPES = ("values_changed", "only_in_primary", "only_in_shadow", "type_mismatch")


def _get_value_at_path(obj: Any, path: str) -> Any:
    """
    Get value at a dot-separated path; supports key[index] for arrays.
    obj can be a dict or list; path e.g. 'raw_json.Order.Items[0].DiscountForGst'.
    Returns None if path does not exist.
    """
    if not path or not path.strip():
        return obj
    parts = re.split(r"\.|(?=\[)|(?=\])", path)
    parts = [p for p in parts if p and p != "]"]
    current: Any = obj
    for part in parts:
        if current is None:
            return None
        match = re.match(r"^(\w+)\[(\d+)\]$", part)
        if match:
            key, idx = match.group(1), int(match.group(2))
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
            if isinstance(current, list) and 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        else:
            key = part.strip("[]")
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
    return current


def _normalize_value(val: Any) -> Any:
    """Convert date/datetime to ISO strings for safe diffing (as per doc)."""
    if val is None:
        return None
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    return val


def row_to_mapped_dict(
    row: dict[str, Any],
    columns: list[dict[str, str]],
    side: str,
) -> dict[str, Any]:
    """
    Build a dict with logical keys from column mapping; values from row (flat or JSON path).
    side is 'primary' or 'shadow'; column entries have 'primary' and 'shadow' keys.
    """
    result: dict[str, Any] = {}
    for col in columns:
        prim_col = col.get("primary", "")
        shadow_col = col.get("shadow", "")
        key = prim_col or shadow_col or "unknown"
        if side == "primary":
            raw = row.get(prim_col)
            if raw is None and "." in prim_col:
                base = prim_col.split(".")[0]
                if base in row and row[base]:
                    try:
                        obj = json.loads(row[base]) if isinstance(row[base], str) else row[base]
                    except (TypeError, json.JSONDecodeError):
                        obj = row[base]
                    raw = _get_value_at_path(obj, prim_col[len(base) + 1 :].strip("."))
            result[key] = _normalize_value(raw)
        else:
            raw = row.get(shadow_col)
            if raw is None and "." in shadow_col:
                base = shadow_col.split(".")[0]
                if base in row and row[base]:
                    try:
                        obj = json.loads(row[base]) if isinstance(row[base], str) else row[base]
                    except (TypeError, json.JSONDecodeError):
                        obj = row[base]
                    raw = _get_value_at_path(obj, shadow_col[len(base) + 1 :].strip("."))
            result[key] = _normalize_value(raw)
    return result


def _classify_diff(diff: DeepDiff) -> list[tuple[str, Any, Any, str]]:
    """
    Convert DeepDiff result to list of (field_name, primary_value, shadow_value, difference_type).
    """
    out: list[tuple[str, Any, Any, str]] = []
    if not diff:
        return out

    # values_changed: path -> { old_value, new_value }
    for path, change in diff.get("values_changed", {}).items():
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        old_v = change.get("old_value")
        new_v = change.get("new_value")
        out.append((field, old_v, new_v, "values_changed"))

    # type_changes
    for path, change in diff.get("type_changes", {}).items():
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        old_v = change.get("old_value")
        new_v = change.get("new_value")
        out.append((field, old_v, new_v, "type_mismatch"))

    # dictionary_item_removed -> only_in_primary (key was in primary, not in shadow)
    for path in diff.get("dictionary_item_removed", []):
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        out.append((field, "(present)", "(missing)", "only_in_primary"))

    # dictionary_item_added -> only_in_shadow
    for path in diff.get("dictionary_item_added", []):
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        out.append((field, "(missing)", "(present)", "only_in_shadow"))

    return out


def _resolve_primary_shadow_names(
    field_key: str, columns: list[dict[str, str]]
) -> tuple[str, str]:
    """Given a key from mapped dict (primary or shadow column name), return (primary_name, shadow_name)."""
    for col in columns:
        prim = col.get("primary", "")
        shadow = col.get("shadow", "")
        if prim == field_key or shadow == field_key:
            return (prim or field_key, shadow or field_key)
    return (field_key, field_key)


def compare_rows(
    primary_row: dict[str, Any],
    shadow_row: Optional[dict[str, Any]],
    columns: list[dict[str, str]],
    table_label: str,
) -> list[tuple[str, str, str, Any, Any, str]]:
    """
    Compare one Primary row with one Shadow row using column mapping.
    If shadow_row is None, all mapped fields are reported as only_in_primary (missing in shadow).
    Returns list of (table_label, primary_field, shadow_field, primary_value, shadow_value, difference_type).
    """
    primary_dict = row_to_mapped_dict(primary_row, columns, "primary")
    if shadow_row is None:
        return [
            (table_label, *_resolve_primary_shadow_names(k, columns), primary_dict.get(k), None, "only_in_primary")
            for k in primary_dict
        ]
    shadow_dict = row_to_mapped_dict(shadow_row, columns, "shadow")
    diff = DeepDiff(primary_dict, shadow_dict, ignore_order=True)
    raw = _classify_diff(diff)
    return [
        (table_label, *_resolve_primary_shadow_names(f, columns), pv, sv, dt)
        for f, pv, sv, dt in raw
    ]


def compare_invoice_sections(
    mapping: dict[str, Any],
    primary_invoices: list[dict[str, Any]],
    primary_details: list[dict[str, Any]],
    primary_payments: list[dict[str, Any]],
    shadow_invoices: list[dict[str, Any]],
    shadow_items: list[dict[str, Any]],
    shadow_payments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    For each primary invoice, find matching shadow by GUID; compare all three tables
    using pre-extracted data (no DB connections). Returns list of section dicts, each with:
    - invoice_guid, primary_invoice, shadow_invoice
    - invoice_diffs, detail_diffs, payment_diffs (list of (field, primary_val, shadow_val, diff_type))
    - missing_in_shadow (bool)
    - detail_rows_missing_in_shadow, detail_rows_extra_in_shadow (counts)
    - detail_row_keys_only_in_primary, detail_row_keys_only_in_shadow (business key values)
    - payment_rows_missing_in_shadow, payment_rows_extra_in_shadow (counts)
    - payment_row_keys_only_in_primary, payment_row_keys_only_in_shadow (business key values)
    """
    tables = mapping.get("tables", {})
    inv_cols = tables.get("invoice", {}).get("columns", [])
    detail_cfg = tables.get("invoice_detail_item", {})
    detail_cols = detail_cfg.get("columns", [])
    detail_bk = detail_cfg.get("business_key") or {}
    pay_cfg = tables.get("payment_transaction", {})
    pay_cols = pay_cfg.get("columns", [])
    pay_bk = pay_cfg.get("business_key") or {}

    shadow_inv_by_guid: dict[str, dict] = {}
    for r in shadow_invoices:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_inv_by_guid[str(g)] = r

    shadow_items_by_guid: dict[str, list] = {}
    for r in shadow_items:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_items_by_guid.setdefault(str(g), []).append(r)

    shadow_payments_by_guid: dict[str, list] = {}
    for r in shadow_payments:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_payments_by_guid.setdefault(str(g), []).append(r)

    primary_details_by_invoice: dict[Union[int, str], list] = {}
    detail_fk_col = detail_cfg.get("primary_invoice_fk_column", "InvoiceId")
    for r in primary_details:
        iid = r.get(detail_fk_col)
        if iid is not None:
            primary_details_by_invoice.setdefault(iid, []).append(r)

    primary_payments_by_invoice: dict[Union[int, str], list] = {}
    pay_fk_col = pay_cfg.get("primary_invoice_fk_column", "InvoiceId")
    for r in primary_payments:
        iid = r.get(pay_fk_col)
        if iid is not None:
            primary_payments_by_invoice.setdefault(iid, []).append(r)

    sections: list[dict[str, Any]] = []
    for inv in primary_invoices:
        guid = inv.get("UniqueCode")
        if guid is None:
            continue
        guid_str = str(guid)
        shadow_inv = shadow_inv_by_guid.get(guid_str)

        # Invoice-level comparison
        invoice_diffs = compare_rows(inv, shadow_inv, inv_cols, "Invoice") if inv_cols else []

        # Detail items: match by invoice GUID (already grouped), then by business key
        prim_detail_list = primary_details_by_invoice.get(inv.get("InvoiceId"), [])
        shadow_detail_list = shadow_items_by_guid.get(guid_str, [])
        prim_bk_col = detail_bk.get("primary")
        shadow_bk_col = detail_bk.get("shadow")
        detail_diffs: list[tuple[str, Any, Any, str]] = []
        detail_rows_missing_in_shadow = 0
        detail_rows_extra_in_shadow = 0
        detail_row_keys_only_in_primary: list[Any] = []
        detail_row_keys_only_in_shadow: list[Any] = []
        if prim_bk_col and shadow_bk_col:
            shadow_by_key = {str(r.get(shadow_bk_col)): r for r in shadow_detail_list}
            for pr in prim_detail_list:
                pk = pr.get(prim_bk_col)
                sr = shadow_by_key.get(str(pk)) if pk is not None else None
                if sr is None:
                    detail_rows_missing_in_shadow += 1
                    if pk is not None:
                        detail_row_keys_only_in_primary.append(pk)
                detail_diffs.extend(
                    compare_rows(pr, sr, detail_cols, "Invoice detail item") if detail_cols else []
                )
            for sr in shadow_detail_list:
                sk = sr.get(shadow_bk_col)
                if not any(pr.get(prim_bk_col) == sk for pr in prim_detail_list):
                    detail_rows_extra_in_shadow += 1
                    if sk is not None:
                        detail_row_keys_only_in_shadow.append(sk)
                    detail_diffs.extend(
                        compare_rows({}, sr, detail_cols, "Invoice detail item") if detail_cols else []
                    )
        else:
            for i, pr in enumerate(prim_detail_list):
                sr = shadow_detail_list[i] if i < len(shadow_detail_list) else None
                if sr is None:
                    detail_rows_missing_in_shadow += 1
                detail_diffs.extend(compare_rows(pr, sr, detail_cols, "Invoice detail item") if detail_cols else [])
            if len(shadow_detail_list) > len(prim_detail_list):
                detail_rows_extra_in_shadow = len(shadow_detail_list) - len(prim_detail_list)

        # Payments: match by invoice GUID (already grouped), then by business key
        prim_pay_list = primary_payments_by_invoice.get(inv.get("InvoiceId"), [])
        shadow_pay_list = shadow_payments_by_guid.get(guid_str, [])
        pay_prim_bk = pay_bk.get("primary")
        pay_shadow_bk = pay_bk.get("shadow")
        payment_diffs: list[tuple[str, Any, Any, str]] = []
        payment_rows_missing_in_shadow = 0
        payment_rows_extra_in_shadow = 0
        payment_row_keys_only_in_primary: list[Any] = []
        payment_row_keys_only_in_shadow: list[Any] = []
        if pay_prim_bk and pay_shadow_bk:
            shadow_pay_by_key = {str(r.get(pay_shadow_bk)): r for r in shadow_pay_list}
            for pr in prim_pay_list:
                pk = pr.get(pay_prim_bk)
                sr = shadow_pay_by_key.get(str(pk)) if pk is not None else None
                if sr is None:
                    payment_rows_missing_in_shadow += 1
                    if pk is not None:
                        payment_row_keys_only_in_primary.append(pk)
                payment_diffs.extend(
                    compare_rows(pr, sr, pay_cols, "Payment transaction") if pay_cols else []
                )
            for sr in shadow_pay_list:
                sk = sr.get(pay_shadow_bk)
                if not any(pr.get(pay_prim_bk) == sk for pr in prim_pay_list):
                    payment_rows_extra_in_shadow += 1
                    if sk is not None:
                        payment_row_keys_only_in_shadow.append(sk)
                    payment_diffs.extend(
                        compare_rows({}, sr, pay_cols, "Payment transaction") if pay_cols else []
                    )
        else:
            for i, pr in enumerate(prim_pay_list):
                sr = shadow_pay_list[i] if i < len(shadow_pay_list) else None
                if sr is None:
                    payment_rows_missing_in_shadow += 1
                payment_diffs.extend(compare_rows(pr, sr, pay_cols, "Payment transaction") if pay_cols else [])
            if len(shadow_pay_list) > len(prim_pay_list):
                payment_rows_extra_in_shadow = len(shadow_pay_list) - len(prim_pay_list)

        sections.append({
            "invoice_guid": guid_str,
            "primary_invoice": inv,
            "shadow_invoice": shadow_inv,
            "invoice_diffs": invoice_diffs,
            "detail_diffs": detail_diffs,
            "payment_diffs": payment_diffs,
            "missing_in_shadow": shadow_inv is None,
            "detail_rows_missing_in_shadow": detail_rows_missing_in_shadow,
            "detail_rows_extra_in_shadow": detail_rows_extra_in_shadow,
            "detail_row_keys_only_in_primary": detail_row_keys_only_in_primary,
            "detail_row_keys_only_in_shadow": detail_row_keys_only_in_shadow,
            "payment_rows_missing_in_shadow": payment_rows_missing_in_shadow,
            "payment_rows_extra_in_shadow": payment_rows_extra_in_shadow,
            "payment_row_keys_only_in_primary": payment_row_keys_only_in_primary,
            "payment_row_keys_only_in_shadow": payment_row_keys_only_in_shadow,
        })
    return sections
