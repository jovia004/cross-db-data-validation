"""PostgreSQL (Shadow) connection and data extraction."""

import logging
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


def _get_connection(conf: dict[str, Any]) -> psycopg2.extensions.connection:
    """Connect to PostgreSQL using config."""
    return psycopg2.connect(
        host=conf["host"],
        port=conf.get("port", "5432"),
        dbname=conf["database"],
        user=conf["user"],
        password=conf["password"],
        cursor_factory=RealDictCursor,
    )


def fetch_shadow_data(
    db_config: dict[str, Any],
    mapping: dict[str, Any],
    invoice_guids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch data from Shadow (PostgreSQL) for the given invoice_guid list.
    Returns (invoice_rows, invoice_item_rows, payment_transaction_rows).
    """
    if not invoice_guids:
        return ([], [], [])

    tables = mapping.get("tables", {})
    inv_cfg = tables.get("invoice", {})
    item_cfg = tables.get("invoice_detail_item", {})
    pay_cfg = tables.get("payment_transaction", {})

    shadow_invoice = inv_cfg.get("shadow_table", "invoice")
    shadow_item = item_cfg.get("shadow_table", "invoice_item")
    shadow_payment = pay_cfg.get("shadow_table", "payment_transaction")

    logger.info("Connecting to Shadow database (PostgreSQL).")
    conn = _get_connection(db_config)
    try:
        with conn.cursor() as cur:
            # Cast uuid columns to text so ANY(%s) (text[]) works
            cur.execute(
                f'SELECT * FROM "{shadow_invoice}" WHERE invoice_guid::text = ANY(%s)',
                (invoice_guids,),
            )
            invoice_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f'SELECT * FROM "{shadow_item}" WHERE invoice_guid::text = ANY(%s)',
                (invoice_guids,),
            )
            item_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f'SELECT * FROM "{shadow_payment}" WHERE invoice_guid::text = ANY(%s)',
                (invoice_guids,),
            )
            payment_rows = [dict(row) for row in cur.fetchall()]

        logger.info(
            "Fetched from Shadow: %s invoices, %s invoice items, %s payment transactions.",
            len(invoice_rows),
            len(item_rows),
            len(payment_rows),
        )
        return (invoice_rows, item_rows, payment_rows)
    finally:
        conn.close()
