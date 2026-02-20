"""Compare Primary and Shadow row data using DeepDiff and column mapping.

Scope: compares 3 tables (invoice, invoice_detail_item, payment_transaction).
No DB connections: expects data already extracted from primary and shadow; callers
pass in the fetched lists.

- Column handling and normalization: as per doc — date/datetime to ISO strings for safe diffing; column mapping aligns primary/shadow names.
- Comparison: DeepDiff on mapped row dicts.
- Output: structured list of (field, primary_val, shadow_val, diff_type) with
  types: values_changed, type_mismatch, only_in_primary, only_in_shadow.
- Expected differences (not reported): Primary null vs Shadow 0 / 0.0 / False
  are treated as equivalent; null vs empty string (either direction, e.g. InternalId,
  OriginalOrderId) are treated as equivalent; numeric pairs that are equal (e.g. 18.9 vs 18.9000)
  are treated as equivalent; UUIDs that differ only by casing (e.g. B76CC2C5-... vs b76cc2c5-...)
  are treated as equivalent; only other differences are reported.
- Company-timezone datetimes: Invoice.UpdatedOn and Payment.CreatedOn, Payment.UpdatedOn
  are stored in company timezone in SQL and in UTC in PG. When company_timezones is provided,
  Primary values are converted to UTC before comparison.
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
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

from deepdiff import DeepDiff

# SQL Server / Windows return names like "AUS Eastern Standard Time"; ZoneInfo needs IANA (e.g. Australia/Sydney)
WINDOWS_TO_IANA = {
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Australia/Sydney": "Australia/Sydney",  # already IANA
    "Singapore Standard Time": "Asia/Singapore",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Central Standard Time": "Australia/Darwin",
    "W. Australia Standard Time": "Australia/Perth",
    "Tasmania Standard Time": "Australia/Hobart",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Central European Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Bucharest",
    "W. Europe Standard Time": "Europe/Berlin",
    "US Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
}

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


def _normalize_datetime_to_utc_string(dt: datetime) -> str:
    """Normalize datetime to canonical UTC ISO string so same instant compares equal."""
    if dt.tzinfo is None:
        # Naive: assume UTC (e.g. SQL Server CreatedOnUtc)
        return dt.isoformat() + "+00:00"
    # Aware: convert to UTC
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_value(val: Any) -> Any:
    """Convert date/datetime to ISO strings for safe diffing (as per doc).
    Datetimes are normalized to UTC so same instant (e.g. 07:39:45 vs 07:39:45+00:00) compares equal.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return _normalize_datetime_to_utc_string(val)
    if isinstance(val, date):
        return val.isoformat()
    # String that looks like ISO datetime: parse and canonicalize so UTC instant matches
    if isinstance(val, str) and "T" in val and ("-" in val or "+" in val or "Z" in val):
        try:
            parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return _normalize_datetime_to_utc_string(parsed)
        except (ValueError, TypeError):
            pass
    return val


# UUID pattern: 8-4-4-4-12 hex (optional hyphens); optional surrounding braces/whitespace
_UUID_PATTERN = re.compile(
    r"^\s*\{?\s*([0-9a-fA-F]{8})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{12})\s*\}?\s*$"
)


def _normalize_uuid_string(val: Any) -> Optional[str]:
    """If val looks like a UUID, return lowercase canonical form (with hyphens); else None."""
    if val is None:
        return None
    s = str(val).strip()
    m = _UUID_PATTERN.match(s)
    if not m:
        return None
    return f"{m.group(1).lower()}-{m.group(2).lower()}-{m.group(3).lower()}-{m.group(4).lower()}-{m.group(5).lower()}"


def _uuid_values_equal_case_insensitive(a: Any, b: Any) -> bool:
    """True if both values are UUID-like and equal when compared case-insensitively (omit as difference)."""
    norm_a = _normalize_uuid_string(a)
    norm_b = _normalize_uuid_string(b)
    if norm_a is None or norm_b is None:
        return False
    return norm_a == norm_b


# Column names (primary/shadow logical key) compared case-insensitively so case-only GUIDs are not reported as differences
CASE_INSENSITIVE_COMPARE_KEYS = frozenset({"UniqueCode", "UniqueId"})

# Primary columns stored in company timezone (SQL); Shadow stores UTC. We convert Primary to UTC before compare.
COMPANY_TZ_DATETIME_COLUMNS = {
    "invoice": ["UpdatedOn"],
    "payment_transaction": ["CreatedOn", "UpdatedOn"],
}


def _is_semantic_zero(val: Any) -> bool:
    """True if value is a logical zero/false: 0, 0.0, False, Decimal(0), etc."""
    if val is None:
        return False
    if val is False:
        return True
    if isinstance(val, bool):
        return not val
    if isinstance(val, (int, float)) and val == 0:
        return True
    if isinstance(val, Decimal) and val == 0:
        return True
    return False


def _is_null_vs_default(primary_val: Any, shadow_val: Any) -> bool:
    """True when Primary has null and Shadow has 0/False/0.0 — expected, do not report as difference."""
    return primary_val is None and _is_semantic_zero(shadow_val)


def _is_null_vs_empty_string(a: Any, b: Any) -> bool:
    """True when one value is null and the other is empty string — expected, do not report as difference."""
    if a is None and isinstance(b, str) and b == "":
        return True
    if b is None and isinstance(a, str) and a == "":
        return True
    return False


def _numeric_values_equal(a: Any, b: Any) -> bool:
    """True if both values are numeric and equal (e.g. 18.9 vs 18.9000)."""
    if a is None or b is None:
        return False
    if not isinstance(a, (int, float, Decimal)) or not isinstance(b, (int, float, Decimal)):
        return False
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _parse_datetime(val: Any) -> Optional[datetime]:
    """Return datetime from value (datetime, date, or ISO string), or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date) and not isinstance(val, datetime):
        return datetime.combine(val, datetime.min.time())
    if isinstance(val, str) and "T" in val:
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


def _datetimes_near_equal(a: Any, b: Any, tolerance_seconds: float = 5.0) -> bool:
    """True if both values are datetime-like and within tolerance_seconds of each other (ignore sub-second noise)."""
    dt_a = _parse_datetime(a)
    dt_b = _parse_datetime(b)
    if dt_a is None or dt_b is None:
        return False
    # Normalize to UTC for comparison if aware
    if dt_a.tzinfo:
        dt_a = dt_a.astimezone(timezone.utc)
    else:
        dt_a = dt_a.replace(tzinfo=timezone.utc)
    if dt_b.tzinfo:
        dt_b = dt_b.astimezone(timezone.utc)
    else:
        dt_b = dt_b.replace(tzinfo=timezone.utc)
    return abs((dt_a - dt_b).total_seconds()) <= tolerance_seconds


def _is_iana_name(name: str) -> bool:
    """True if name is a valid IANA timezone (e.g. Australia/Sydney) so ZoneInfo can use it."""
    if not name or "/" not in name:
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _get_company_tz(company_id: Any, company_timezones: dict[Any, str]) -> Optional[str]:
    """Resolve timezone for company_id; try exact key and str/int so DB type mismatch doesn't skip conversion."""
    if company_id is None:
        return None
    tz = company_timezones.get(company_id)
    if tz:
        return tz
    tz = company_timezones.get(str(company_id))
    if tz:
        return tz
    try:
        tz = company_timezones.get(int(company_id))
    except (TypeError, ValueError):
        pass
    return tz or None


def _row_get(row: dict[str, Any], col: str) -> Any:
    """Get value from row by column name, case-insensitive (SQL Server may return different casing)."""
    v = row.get(col)
    if v is not None:
        return v
    key = next((k for k in row if str(k).lower() == col.lower()), None)
    return row.get(key) if key else None


def _convert_company_tz_columns_to_utc(
    row: dict[str, Any],
    column_names: list[str],
    company_id: Any,
    company_timezones: dict[Any, str],
) -> dict[str, Any]:
    """
    Return a copy of row with the given datetime columns converted from company local time to UTC.
    SQL Server stores these in company timezone; PostgreSQL stores UTC. If company_id not in
    company_timezones, returns a shallow copy unchanged.
    We always treat the stored value as company-local wall time (even if DB driver attached +00:00).
    """
    out = deepcopy(row)
    tz_str = _get_company_tz(company_id, company_timezones)
    if not tz_str:
        return out
    tz_str = (tz_str or "").strip()
    # SQL Server returns Windows names (e.g. "AUS Eastern Standard Time"); ZoneInfo needs IANA (e.g. "Australia/Sydney")
    iana = WINDOWS_TO_IANA.get(tz_str) or (tz_str if _is_iana_name(tz_str) else None)
    if not iana:
        logger.warning(
            "Unknown company timezone %r (not IANA and not in Windows mapping). Skipping conversion. Add to WINDOWS_TO_IANA in compare.py.",
            tz_str,
        )
        return out
    try:
        tz = ZoneInfo(iana)
    except Exception as e:
        logger.warning("ZoneInfo(%r) failed: %s. Skipping company TZ conversion.", iana, e)
        return out
    for col in column_names:
        val = _row_get(out, col)
        dt = _parse_datetime(val)
        if dt is None:
            continue
        # Always treat as company-local wall time: strip tzinfo then localize (SQL may return +00:00 but value is company TZ)
        naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        utc_dt = naive.replace(tzinfo=tz).astimezone(timezone.utc)
        # Set under the key that exists in the row (for consistency) and under col so row_to_mapped_dict finds it
        row_key = next((k for k in out if str(k).lower() == col.lower()), col)
        out[row_key] = utc_dt
        if row_key != col:
            out[col] = utc_dt
    return out


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
            raw = _row_get(row, prim_col)
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
            raw = _row_get(row, shadow_col)
            if raw is None and "." in shadow_col:
                base = shadow_col.split(".")[0]
                if base in row and row[base]:
                    try:
                        obj = json.loads(row[base]) if isinstance(row[base], str) else row[base]
                    except (TypeError, json.JSONDecodeError):
                        obj = row[base]
                    raw = _get_value_at_path(obj, shadow_col[len(base) + 1 :].strip("."))
            result[key] = _normalize_value(raw)
        if key in CASE_INSENSITIVE_COMPARE_KEYS and isinstance(result[key], str):
            result[key] = result[key].lower()
    return result


def _classify_diff(diff: DeepDiff) -> list[tuple[str, Any, Any, str]]:
    """
    Convert DeepDiff result to list of (field_name, primary_value, shadow_value, difference_type).
    """
    out: list[tuple[str, Any, Any, str]] = []
    if not diff:
        return out

    # values_changed: path -> { old_value, new_value }
    # Skip when Primary is null and Shadow is 0/False/0.0 (expected; only report when actual data values differ)
    # Skip when one is null and the other is empty string (InternalId/original_order_id etc.)
    # Skip when both are numeric and equal (e.g. 18.9 vs 18.9000)
    # Skip when both are datetimes within 1 second (millisecond/sub-second noise)
    for path, change in diff.get("values_changed", {}).items():
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        old_v = change.get("old_value")
        new_v = change.get("new_value")
        if _is_null_vs_default(old_v, new_v):
            continue
        if _is_null_vs_empty_string(old_v, new_v):
            continue
        if _numeric_values_equal(old_v, new_v):
            continue
        if _datetimes_near_equal(old_v, new_v):
            continue
        if _uuid_values_equal_case_insensitive(old_v, new_v):
            continue
        out.append((field, old_v, new_v, "values_changed"))

    # type_changes (e.g. null vs 0, null vs False, null vs "", or 18.9 vs 18.9000) — ignore null vs default, null vs "", and equal numerics
    # Also ignore datetimes within 1 second
    for path, change in diff.get("type_changes", {}).items():
        field = path.replace("root[", "").replace("]", "").strip("'\"")
        old_v = change.get("old_value")
        new_v = change.get("new_value")
        if _is_null_vs_default(old_v, new_v):
            continue
        if _is_null_vs_empty_string(old_v, new_v):
            continue
        if _numeric_values_equal(old_v, new_v):
            continue
        if _datetimes_near_equal(old_v, new_v):
            continue
        if _uuid_values_equal_case_insensitive(old_v, new_v):
            continue
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


def _effective_columns(columns: list[dict[str, str]], skipped: Optional[list[str]] = None) -> list[dict[str, str]]:
    """Return columns with skipped_columns (primary names) excluded; used for comparison and reporting."""
    if not skipped:
        return columns
    skip_set = set(skipped)
    return [c for c in columns if c.get("primary") not in skip_set]


def compare_invoice_sections(
    mapping: dict[str, Any],
    primary_invoices: list[dict[str, Any]],
    primary_details: list[dict[str, Any]],
    primary_payments: list[dict[str, Any]],
    shadow_invoices: list[dict[str, Any]],
    shadow_items: list[dict[str, Any]],
    shadow_payments: list[dict[str, Any]],
    company_timezones: Optional[dict[Any, str]] = None,
) -> list[dict[str, Any]]:
    """
    For each primary invoice, find matching shadow by GUID; compare all three tables
    using pre-extracted data (no DB connections). Optional company_timezones (company_id -> timezone
    string) is used to convert Primary company-local datetimes (Invoice.UpdatedOn, Payment.CreatedOn,
    Payment.UpdatedOn) to UTC before comparing with Shadow (which stores UTC). Returns list of section dicts, each with:
    - invoice_guid, primary_invoice, shadow_invoice
    - invoice_diffs, detail_diffs, payment_diffs (list of (field, primary_val, shadow_val, diff_type))
    - missing_in_shadow (bool)
    - detail_rows_missing_in_shadow, detail_rows_extra_in_shadow (counts)
    - detail_row_keys_only_in_primary, detail_row_keys_only_in_shadow (business key values)
    - payment_rows_missing_in_shadow, payment_rows_extra_in_shadow (counts)
    - payment_row_keys_only_in_primary, payment_row_keys_only_in_shadow (business key values)
    Columns listed in skipped_columns (by primary name) are excluded from comparison and reports.
    """
    logger.debug(
        "Comparing invoice sections: %s primary, %s shadow invoices.",
        len(primary_invoices),
        len(shadow_invoices),
    )
    tables = mapping.get("tables", {})
    inv_cfg = tables.get("invoice", {})
    inv_cols = _effective_columns(
        inv_cfg.get("columns", []),
        inv_cfg.get("skipped_columns"),
    )
    detail_cfg = tables.get("invoice_detail_item", {})
    detail_cols = _effective_columns(
        detail_cfg.get("columns", []),
        detail_cfg.get("skipped_columns"),
    )
    detail_bk = detail_cfg.get("business_key") or {}
    pay_cfg = tables.get("payment_transaction", {})
    pay_cols = _effective_columns(
        pay_cfg.get("columns", []),
        pay_cfg.get("skipped_columns"),
    )
    pay_bk = pay_cfg.get("business_key") or {}
    company_timezones = company_timezones or {}
    company_col = next(
        (c.get("primary") for c in inv_cfg.get("columns", []) if c.get("shadow") == "company_id"),
        "Company",
    )

    def _company_id_from_inv(inv: dict[str, Any]) -> Any:
        """Get company ID from invoice row; key may be Company or company depending on DB driver."""
        key = next((k for k in inv if str(k).lower() == company_col.lower()), None)
        return inv.get(key) if key else None

    inv_tz_cols = COMPANY_TZ_DATETIME_COLUMNS.get("invoice", [])
    pay_tz_cols = COMPANY_TZ_DATETIME_COLUMNS.get("payment_transaction", [])

    def _norm_guid(g: Any) -> str:
        return str(g).lower() if g is not None else ""

    def _norm_business_key(val: Any) -> str:
        """Normalize business key for matching (e.g. GUIDs that differ only by case)."""
        if val is None:
            return ""
        return str(val).strip().lower()

    shadow_inv_by_guid: dict[str, dict] = {}
    for r in shadow_invoices:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_inv_by_guid[_norm_guid(g)] = r

    shadow_items_by_guid: dict[str, list] = {}
    for r in shadow_items:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_items_by_guid.setdefault(_norm_guid(g), []).append(r)

    shadow_payments_by_guid: dict[str, list] = {}
    for r in shadow_payments:
        g = r.get("invoice_guid")
        if g is not None:
            shadow_payments_by_guid.setdefault(_norm_guid(g), []).append(r)

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
            invoice_id = inv.get("InvoiceId", "?")
            logger.warning(
                "Primary invoice InvoiceId=%s has no UniqueCode; including in report as 'Primary only' (cannot match to Shadow).",
                invoice_id,
            )
            guid_str = f"(no UniqueCode — InvoiceId {invoice_id})"
            shadow_inv = None
        else:
            guid_str = _norm_guid(guid)
            shadow_inv = shadow_inv_by_guid.get(guid_str)

        company_id = _company_id_from_inv(inv)
        tz_applied_for_this_invoice = _get_company_tz(company_id, company_timezones) is not None
        if tz_applied_for_this_invoice:
            logger.debug(
                "Converting company TZ to UTC for invoice %s (company_id=%s, timezone=%s).",
                inv.get("InvoiceId"),
                company_id,
                company_timezones.get(company_id),
            )
        if company_timezones and not tz_applied_for_this_invoice and company_id is not None:
            logger.debug(
                "No timezone for company ID %s (map keys: %s); skipping company TZ conversion for invoice %s.",
                company_id,
                list(company_timezones.keys()),
                inv.get("InvoiceId"),
            )
        inv_primary = (
            _convert_company_tz_columns_to_utc(inv, inv_tz_cols, company_id, company_timezones)
            if company_timezones else inv
        )

        # Invoice-level comparison (Primary datetimes in company TZ converted to UTC when company_timezones provided)
        invoice_diffs = compare_rows(inv_primary, shadow_inv, inv_cols, "Invoice") if inv_cols else []

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
            shadow_by_key = {_norm_business_key(r.get(shadow_bk_col)): r for r in shadow_detail_list}
            for pr in prim_detail_list:
                pk = pr.get(prim_bk_col)
                sr = shadow_by_key.get(_norm_business_key(pk)) if pk is not None else None
                if sr is None:
                    detail_rows_missing_in_shadow += 1
                    if pk is not None:
                        detail_row_keys_only_in_primary.append(pk)
                detail_diffs.extend(
                    compare_rows(pr, sr, detail_cols, "Invoice detail item") if detail_cols else []
                )
            for sr in shadow_detail_list:
                sk = sr.get(shadow_bk_col)
                sk_norm = _norm_business_key(sk)
                if not any(_norm_business_key(pr.get(prim_bk_col)) == sk_norm for pr in prim_detail_list):
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
            shadow_pay_by_key = {_norm_business_key(r.get(pay_shadow_bk)): r for r in shadow_pay_list}
            for pr in prim_pay_list:
                pk = pr.get(pay_prim_bk)
                sr = shadow_pay_by_key.get(_norm_business_key(pk)) if pk is not None else None
                if sr is None:
                    payment_rows_missing_in_shadow += 1
                    if pk is not None:
                        payment_row_keys_only_in_primary.append(pk)
                pr_primary = (
                    _convert_company_tz_columns_to_utc(pr, pay_tz_cols, company_id, company_timezones)
                    if company_timezones else pr
                )
                payment_diffs.extend(
                    compare_rows(pr_primary, sr, pay_cols, "Payment transaction") if pay_cols else []
                )
            for sr in shadow_pay_list:
                sk = sr.get(pay_shadow_bk)
                sk_norm = _norm_business_key(sk)
                if not any(_norm_business_key(pr.get(pay_prim_bk)) == sk_norm for pr in prim_pay_list):
                    payment_rows_extra_in_shadow += 1
                    if sk is not None:
                        payment_row_keys_only_in_shadow.append(sk)
                    # Extra-in-shadow: no primary row; no company TZ conversion needed
                    payment_diffs.extend(
                        compare_rows({}, sr, pay_cols, "Payment transaction") if pay_cols else []
                    )
        else:
            for i, pr in enumerate(prim_pay_list):
                sr = shadow_pay_list[i] if i < len(shadow_pay_list) else None
                if sr is None:
                    payment_rows_missing_in_shadow += 1
                pr_primary = (
                    _convert_company_tz_columns_to_utc(pr, pay_tz_cols, company_id, company_timezones)
                    if company_timezones else pr
                )
                payment_diffs.extend(compare_rows(pr_primary, sr, pay_cols, "Payment transaction") if pay_cols else [])
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
            "company_tz_conversion_applied": tz_applied_for_this_invoice,
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
