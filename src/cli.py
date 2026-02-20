"""CLI entry point: load config, extract, compare, report."""

import argparse
import logging
import sys
from pathlib import Path

# Add project root so "src" and config paths resolve
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.config_loader import load_config
from src.db.primary import fetch_primary_data
from src.db.shadow import fetch_shadow_data
from src.compare import compare_invoice_sections
from src.report import write_report

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging; no secrets, user-understandable messages."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Avoid noisy third-party loggers
    logging.getLogger("weasyprint").setLevel(logging.WARNING)
    logging.getLogger("fontTools").setLevel(logging.WARNING)


def run(
    config_path=None,
    verbose: bool = False,
    invoice_ids: list[int] | None = None,
) -> None:
    """
    Run the full flow: load config, fetch from Primary and Shadow, compare, write report.
    If invoice_ids is non-empty, only those Primary InvoiceIds are extracted and compared.
    Otherwise sample_count from config is used (today → last 3 days → last 7 days, or random).
    Exits with 0 on success, 1 on validation/connection/data error (after logging).
    """
    setup_logging(verbose=verbose)

    try:
        cfg = load_config(config_path)
    except ValueError:
        sys.exit(1)

    environment = cfg.get("environment", "test")
    logger.info("Environment: %s", environment)

    db_cfg = cfg["db"]
    mapping = cfg["mapping"]
    primary_conf = db_cfg["primary"]
    shadow_conf = db_cfg["shadow"]

    # Fetch from Primary (filtered by invoice_ids when provided)
    try:
        primary_invoices, primary_details, primary_payments, company_timezones = fetch_primary_data(
            primary_conf, mapping, invoice_ids=invoice_ids
        )
    except Exception as e:
        logger.error(
            "Could not connect to Primary database or fetch data. "
            "Check host, port, credentials, and that the Invoice table exists. Details: %s",
            e,
        )
        sys.exit(1)

    if not primary_invoices:
        logger.error(
            "No invoices found in Primary. Check that the Invoice table has data."
        )
        sys.exit(1)

    uniquecodes = [str(inv.get("UniqueCode", "")).lower() for inv in primary_invoices]
    uniquecodes = [u for u in uniquecodes if u]
    if not uniquecodes:
        logger.error("No UniqueCode values in Primary invoices. Cannot fetch Shadow data.")
        sys.exit(1)

    # Fetch from Shadow
    try:
        shadow_invoices, shadow_items, shadow_payments = fetch_shadow_data(
            shadow_conf, mapping, uniquecodes
        )
    except Exception as e:
        logger.error(
            "Could not connect to Shadow database or fetch data. "
            "Check host, port, credentials. Details: %s",
            e,
        )
        sys.exit(1)

    # Compare (company_timezones used to convert Primary company-local datetimes to UTC for Invoice.UpdatedOn, Payment.CreatedOnUtc, Payment.UpdatedOn)
    logger.info("Comparing data for %s invoices.", len(primary_invoices))
    sections = compare_invoice_sections(
        mapping,
        primary_invoices,
        primary_details,
        primary_payments,
        shadow_invoices,
        shadow_items,
        shadow_payments,
        company_timezones=company_timezones,
    )

    for s in sections:
        if s.get("missing_in_shadow"):
            logger.warning(
                "Invoice %s not found in Shadow. Included in report as 'Primary only'.",
                s.get("invoice_guid"),
            )

    # Report
    reports_dir = _project_root / "reports"
    try:
        html_path, pdf_path = write_report(sections, reports_dir)
        logger.info("Report saved: %s", html_path)
        if pdf_path:
            logger.info("PDF saved: %s", pdf_path)
    except OSError:
        logger.error(
            "Could not write report to reports/. "
            "Check that the folder exists and you have write permission."
        )
        sys.exit(1)

    logger.info("Done.")


def main() -> None:
    """Entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Compare invoice data between SQL Server (Primary) and PostgreSQL (Shadow)."
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose (debug) logging.",
    )
    parser.add_argument(
        "-i", "--invoice-id",
        dest="invoice_ids",
        type=int,
        action="append",
        metavar="ID",
        help="Primary DB InvoiceId to include. Can be repeated. If any are given, only these invoices are extracted, compared and reported; otherwise sample_count from config is used.",
    )
    args = parser.parse_args()
    run(config_path=None, verbose=args.verbose, invoice_ids=args.invoice_ids or None)


if __name__ == "__main__":
    main()
