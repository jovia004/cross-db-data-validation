"""SQL Server (Primary) connection and data extraction."""

import logging
from datetime import datetime, timedelta
from typing import Any, List

logger = logging.getLogger(__name__)


def _get_connection(conf: dict[str, Any]):
    """Build SQL Server connection string and connect. Driver 17 or 18 preferred."""
    import pyodbc
    host = conf["host"]
    port = conf.get("port", "1433")
    database = conf["database"]
    user = conf["user"]
    password = conf["password"]
    drivers = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
    ]
    driver = None
    for d in drivers:
        try:
            if d in [x for x in pyodbc.drivers()]:
                driver = d
                break
        except Exception:
            continue
    if not driver:
        driver = drivers[0]
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
        f"UID={user};PWD={password};Encrypt=no;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


def _fetch_invoice_ids_date_tier(
    cursor,
    primary_invoice: str,
    date_col: str,
    sample_count: int,
    id_col: str,
    unique_code_col: str,
) -> List[dict]:
    """
    Fetch up to sample_count invoice IDs: first from today, then last 3 days, then last 7 days.
    Returns list of dicts with canonical keys "InvoiceId" and "UniqueCode".
    """
    # Today only
    sql_today = f"""
        SELECT TOP (?) [{id_col}], [{unique_code_col}]
        FROM {primary_invoice}
        WHERE CAST([{date_col}] AS DATE) = CAST(GETDATE() AS DATE)
        ORDER BY NEWID()
    """
    cursor.execute(sql_today, (sample_count,))
    rows = list(cursor.fetchall())
    cols = [c[0] for c in cursor.description]
    result = []
    for row in rows:
        d = dict(zip(cols, row))
        result.append({"InvoiceId": d.get(id_col), "UniqueCode": d.get(unique_code_col)})
    ids_so_far = [r["InvoiceId"] for r in result]

    if len(result) >= sample_count:
        return result[:sample_count]

    need = sample_count - len(result)
    # Last 3 days (excluding today)
    not_in_clause = ""
    params = [need]
    if ids_so_far:
        placeholders = ",".join("?" for _ in ids_so_far)
        not_in_clause = f" AND [{id_col}] NOT IN ({placeholders})"
        params.extend(ids_so_far)
    sql_3d = f"""
        SELECT TOP (?) [{id_col}], [{unique_code_col}]
        FROM {primary_invoice}
        WHERE CAST([{date_col}] AS DATE) >= DATEADD(day, -3, CAST(GETDATE() AS DATE))
          AND CAST([{date_col}] AS DATE) < CAST(GETDATE() AS DATE)
          {not_in_clause}
        ORDER BY NEWID()
    """
    cursor.execute(sql_3d, params)
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        result.append({"InvoiceId": d.get(id_col), "UniqueCode": d.get(unique_code_col)})
    ids_so_far = [r["InvoiceId"] for r in result]

    if len(result) >= sample_count:
        return result[:sample_count]

    need = sample_count - len(result)
    # Past 7 days (any, excluding already chosen)
    not_in_clause = ""
    params = [need]
    if ids_so_far:
        placeholders = ",".join("?" for _ in ids_so_far)
        not_in_clause = f" AND [{id_col}] NOT IN ({placeholders})"
        params.extend(ids_so_far)
    sql_7d = f"""
        SELECT TOP (?) [{id_col}], [{unique_code_col}]
        FROM {primary_invoice}
        WHERE CAST([{date_col}] AS DATE) >= DATEADD(day, -7, CAST(GETDATE() AS DATE))
          {not_in_clause}
        ORDER BY NEWID()
    """
    cursor.execute(sql_7d, params)
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        result.append({"InvoiceId": d.get(id_col), "UniqueCode": d.get(unique_code_col)})
    return result[:sample_count]


def fetch_primary_data(
    db_config: dict[str, Any],
    mapping: dict[str, Any],
    invoice_ids: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch data from Primary (SQL Server).
    If invoice_ids is non-empty, fetch only those InvoiceIds; otherwise use sample_count
    (today → last 3 days → last 7 days, or random).
    Returns (invoice_rows, detail_rows, payment_rows).
    """
    tables = mapping.get("tables", {})
    invoice_cfg = tables.get("invoice", {})
    detail_cfg = tables.get("invoice_detail_item", {})
    payment_cfg = tables.get("payment_transaction", {})
    sample_count = int(mapping.get("sample_count", 10))
    date_col = mapping.get("invoice_date_column", "").strip()

    primary_invoice = invoice_cfg.get("primary_table", "Invoice")
    primary_detail = detail_cfg.get("primary_table", "InvoiceDetailItem")
    primary_payment = payment_cfg.get("primary_table", "PaymentTransaction")

    # Configurable column names (Primary DB). Use if you get "Invalid column name 'InvoiceId'".
    id_col = invoice_cfg.get("primary_invoice_id_column", "InvoiceId")
    unique_code_col = invoice_cfg.get("primary_invoice_unique_code_column", "UniqueCode")
    detail_fk_col = detail_cfg.get("primary_invoice_fk_column", id_col)
    payment_fk_col = payment_cfg.get("primary_invoice_fk_column", id_col)

    logger.info("Connecting to Primary database (SQL Server).")
    conn = _get_connection(db_config)
    try:
        cursor = conn.cursor()
        if invoice_ids:
            # Specific InvoiceIds requested: fetch only those
            logger.info("Fetching %s invoice(s) by InvoiceId: %s.", len(invoice_ids), invoice_ids)
            placeholders = ",".join("?" for _ in invoice_ids)
            sql_invoice = (
                f"SELECT [{id_col}], [{unique_code_col}] FROM {primary_invoice} WHERE [{id_col}] IN ({placeholders})"
            )
            cursor.execute(sql_invoice, invoice_ids)
            columns = [c[0] for c in cursor.description]
            invoice_rows = []
            for row in cursor.fetchall():
                d = dict(zip(columns, row))
                invoice_rows.append({"InvoiceId": d.get(id_col), "UniqueCode": d.get(unique_code_col)})
        elif date_col:
            logger.info(
                "Selecting up to %s invoices: today, then last 3 days, then last 7 days (date column: %s).",
                sample_count,
                date_col,
            )
            invoice_rows = _fetch_invoice_ids_date_tier(
                cursor, primary_invoice, date_col, sample_count, id_col, unique_code_col
            )
        else:
            sql_invoice = (
                f"SELECT TOP ({sample_count}) [{id_col}], [{unique_code_col}] FROM {primary_invoice} ORDER BY NEWID()"
            )
            cursor.execute(sql_invoice)
            columns = [c[0] for c in cursor.description]
            invoice_rows = []
            for row in cursor.fetchall():
                d = dict(zip(columns, row))
                invoice_rows.append({"InvoiceId": d.get(id_col), "UniqueCode": d.get(unique_code_col)})

        if not invoice_rows:
            logger.warning("No invoices returned from Primary.")
            return ([], [], [])

        invoice_ids = [r["InvoiceId"] for r in invoice_rows]
        placeholders = ",".join("?" for _ in invoice_ids)

        # Fetch full invoice rows (SELECT *) so comparison has all columns; otherwise only InvoiceId/UniqueCode would be present and report would show "(missing)" for Primary on most fields.
        sql_invoice_full = f"SELECT * FROM {primary_invoice} WHERE [{id_col}] IN ({placeholders})"
        cursor.execute(sql_invoice_full, invoice_ids)
        cols = [c[0] for c in cursor.description]
        invoice_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        logger.info("Fetched %s invoices from Primary (full rows).", len(invoice_rows))

        sql_detail = f"SELECT * FROM {primary_detail} WHERE [{detail_fk_col}] IN ({placeholders})"
        cursor.execute(sql_detail, invoice_ids)
        cols = [c[0] for c in cursor.description]
        detail_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        logger.info("Fetched %s detail item rows from Primary.", len(detail_rows))

        sql_payment = f"SELECT * FROM {primary_payment} WHERE [{payment_fk_col}] IN ({placeholders})"
        cursor.execute(sql_payment, invoice_ids)
        cols = [c[0] for c in cursor.description]
        payment_rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        logger.info("Fetched %s payment transaction rows from Primary.", len(payment_rows))

        return (invoice_rows, detail_rows, payment_rows)
    finally:
        conn.close()
