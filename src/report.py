"""Build comparison report (HTML + PDF) with TOC and section per invoice."""

import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Union

from jinja2 import Environment, PackageLoader, select_autoescape

logger = logging.getLogger(__name__)

# Primary columns that are converted from company timezone to UTC before comparison; show * in report.
COMPANY_TZ_CONVERTED_FIELDS = {
    ("Invoice", "UpdatedOn"),
    ("Invoice", "PickupTime"),
    ("Payment transaction", "CreatedOnUtc"),
    ("Payment transaction", "CreatedOn"),
    ("Payment transaction", "UpdatedOn"),
}

# WeasyPrint optional for PDF (can fail if system deps missing, e.g. libgobject)
try:
    from weasyprint import HTML as WeasyHTML
    HAS_WEASYPRINT = True
except (ImportError, OSError):
    HAS_WEASYPRINT = False


def _format_cell(val: Any) -> str:
    """Format a value for display in the report table. None is shown as null; (present)/(missing) for key absence."""
    if val is None:
        return "null"
    if isinstance(val, str) and val in ("(present)", "(missing)"):
        return val
    s = str(val)
    if len(s) > 80:
        return s[:77] + "..."
    return s


def _build_section_data(section: dict[str, Any]) -> dict[str, Any]:
    """Prepare one invoice section for the template: TOC label, anchor id, diffs table, notes."""
    guid = section.get("invoice_guid", "")
    primary_inv = section.get("primary_invoice") or {}
    invoice_id = primary_inv.get("InvoiceId", "")
    missing = section.get("missing_in_shadow", False)
    all_diffs: list[tuple[str, str, str, Any, Any, str]] = []
    all_diffs.extend(section.get("invoice_diffs", []))
    all_diffs.extend(section.get("detail_diffs", []))
    all_diffs.extend(section.get("payment_diffs", []))
    company_tz_applied = section.get("company_tz_conversion_applied", False)
    has_converted_tz = False
    rows = []
    for table_name, prim_f, shadow_f, pv, sv, dt in all_diffs:
        if company_tz_applied and (table_name, prim_f) in COMPANY_TZ_CONVERTED_FIELDS:
            has_converted_tz = True
            sql_cell = f"{{{prim_f}: {_format_cell(pv)}}}*"
        else:
            sql_cell = f"{{{prim_f}: {_format_cell(pv)}}}"
        rows.append({
            "table_name": table_name,
            "sql_server_cell": sql_cell,
            "postgresql_cell": f"{{{shadow_f}: {_format_cell(sv)}}}",
            "difference_type": dt,
        })
    notes: list[str] = []
    if missing:
        notes.append("This invoice was not found in Shadow. Data below is from Primary only.")
    detail_missing = section.get("detail_rows_missing_in_shadow", 0)
    detail_extra = section.get("detail_rows_extra_in_shadow", 0)
    if detail_missing or detail_extra:
        parts = []
        if detail_missing:
            parts.append(f"{detail_missing} detail row(s) missing in Shadow (only in Primary)")
        if detail_extra:
            parts.append(f"{detail_extra} detail row(s) extra in Shadow (only in Shadow)")
        notes.append("Detail rows: " + ". ".join(parts))
    pay_missing = section.get("payment_rows_missing_in_shadow", 0)
    pay_extra = section.get("payment_rows_extra_in_shadow", 0)
    if pay_missing or pay_extra:
        parts = []
        if pay_missing:
            parts.append(f"{pay_missing} payment row(s) missing in Shadow (only in Primary)")
        if pay_extra:
            parts.append(f"{pay_extra} payment row(s) extra in Shadow (only in Shadow)")
        notes.append("Payment rows: " + ". ".join(parts))
    if not all_diffs and not missing and not (detail_missing or detail_extra or pay_missing or pay_extra):
        notes.append("No differences detected for this invoice.")
    if has_converted_tz:
        notes.append("(*) The Primary value marked with * is the datetime converted from company timezone to UTC for comparison.")
    type_mismatches = [r for r in rows if r["difference_type"] == "type_mismatch"]
    if type_mismatches:
        notes.append(
            "Some fields have type differences (e.g. number vs string). "
            "Check if values are semantically the same."
        )
    # Status for badges: missing_in_shadow | differences | match
    if missing:
        status = "missing_in_shadow"
    elif all_diffs or detail_missing or detail_extra or pay_missing or pay_extra:
        status = "differences"
    else:
        status = "match"
    return {
        "invoice_id": invoice_id,
        "invoice_guid": guid,
        "missing_in_shadow": missing,
        "status": status,
        "differences": rows,
        "notes": notes,
        "recommendations": [
            "Use invoice_guid as the unique key for sync checks.",
            "If values differ, verify source data or replication.",
            "Detail items with extras (Primary Parent set) are compared against the extras array in Shadow.",
        ],
    }


def build_html(
    sections: list[dict[str, Any]],
    run_datetime: datetime,
    environment: str = "test",
) -> str:
    """Render full HTML report with TOC and sections. Uses Jinja2 template.

    environment is shown in the report header (e.g. "Test" / "Prod"). run_datetime
    is formatted with local timezone (e.g. "2026-02-20 16:31:17 EST").
    """
    env = Environment(
        loader=PackageLoader("src", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")
    section_data = [_build_section_data(s) for s in sections]
    # Executive summary counts
    summary = {
        "total": len(section_data),
        "matched": sum(1 for s in section_data if s["status"] == "match"),
        "with_differences": sum(1 for s in section_data if s["status"] == "differences"),
        "missing_in_shadow": sum(1 for s in section_data if s["status"] == "missing_in_shadow"),
    }
    toc_entries = [
        {
            "index": i + 1,
            "invoice_id": section_data[i]["invoice_id"],
            "anchor_id": f"invoice-{i + 1}",
            "status": section_data[i]["status"],
        }
        for i in range(len(section_data))
    ]
    env_display = environment.strip().capitalize()
    compared_sources = f"SQL Server (Primary) vs PostgreSQL (Shadow) in {env_display} environment"
    # Show timezone: use local timezone if datetime is naive
    dt = run_datetime.astimezone() if run_datetime.tzinfo is None else run_datetime
    run_datetime_str = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    html = template.render(
        run_datetime=run_datetime_str,
        summary=summary,
        toc_entries=toc_entries,
        sections=section_data,
        compared_sources=compared_sources,
        environment=env_display,
    )
    return html


def write_report(
    sections: list[dict[str, Any]],
    output_dir: Union[str, Path],
    environment: str = "test",
) -> tuple[str, str]:
    """
    Generate HTML and PDF reports; save to output_dir with execution datetime in filename.

    environment (from config/column_mapping.json) is included in the report header.
    Returns (path_to_html, path_to_pdf). path_to_pdf is empty string if no PDF was written.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dt = datetime.now()
    stamp = run_dt.strftime("%Y-%m-%d_%H-%M-%S")
    base_name = f"report_{stamp}"

    html_content = build_html(sections, run_dt, environment=environment)
    html_path = output_dir / f"{base_name}.html"
    try:
        html_path.write_text(html_content, encoding="utf-8")
        logger.info("Wrote HTML report: %s", html_path)
    except OSError as e:
        logger.error(
            "Could not write report to %s. Check that the folder exists and you have write permission.",
            output_dir,
        )
        raise

    pdf_path_out = ""
    if HAS_WEASYPRINT:
        pdf_path = output_dir / f"{base_name}.pdf"
        try:
            WeasyHTML(string=html_content, base_url=str(output_dir)).write_pdf(pdf_path)
            logger.info("Wrote PDF report: %s", pdf_path)
            pdf_path_out = str(pdf_path)
        except Exception as e:
            logger.warning("Could not generate PDF: %s. HTML report was saved.", e)
    else:
        logger.warning("WeasyPrint not available; only HTML report was generated.")

    return str(html_path), pdf_path_out
