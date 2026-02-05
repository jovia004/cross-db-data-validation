"""Build comparison report (HTML + PDF) with TOC and section per invoice."""

import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Union

from jinja2 import Environment, PackageLoader, select_autoescape

logger = logging.getLogger(__name__)

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
    missing = section.get("missing_in_shadow", False)
    all_diffs: list[tuple[str, str, str, Any, Any, str]] = []
    all_diffs.extend(section.get("invoice_diffs", []))
    all_diffs.extend(section.get("detail_diffs", []))
    all_diffs.extend(section.get("payment_diffs", []))
    rows = [
        {
            "table_name": table_name,
            "primary_field": prim_f,
            "shadow_field": shadow_f,
            "sql_server_value": _format_cell(pv),
            "postgresql_value": _format_cell(sv),
            "difference_type": dt,
        }
        for table_name, prim_f, shadow_f, pv, sv, dt in all_diffs
    ]
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
    type_mismatches = [r for r in rows if r["difference_type"] == "type_mismatch"]
    if type_mismatches:
        notes.append(
            "Some fields have type differences (e.g. number vs string). "
            "Check if values are semantically the same."
        )
    return {
        "invoice_guid": guid,
        "missing_in_shadow": missing,
        "differences": rows,
        "notes": notes,
        "recommendations": [
            "Use invoice_guid as the unique key for sync checks.",
            "If values differ, verify source data or replication.",
        ],
    }


def build_html(sections: list[dict[str, Any]], run_datetime: datetime) -> str:
    """Render full HTML report with TOC and sections. Uses Jinja2 template."""
    env = Environment(
        loader=PackageLoader("src", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")
    toc_entries = [
        {"index": i + 1, "guid": s.get("invoice_guid", ""), "anchor_id": f"invoice-{i + 1}"}
        for i, s in enumerate(sections)
    ]
    section_data = [_build_section_data(s) for s in sections]
    html = template.render(
        run_datetime=run_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        toc_entries=toc_entries,
        sections=section_data,
        compared_sources="SQL Server (Primary) vs PostgreSQL (Shadow)",
    )
    return html


def write_report(
    sections: list[dict[str, Any]],
    output_dir: Union[str, Path],
) -> tuple[str, str]:
    """
    Generate HTML and PDF reports; save to output_dir with execution datetime in filename.
    Returns (path_to_html, path_to_pdf). PDF path may be same base with .pdf if WeasyPrint available.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dt = datetime.now()
    stamp = run_dt.strftime("%Y-%m-%d_%H-%M-%S")
    base_name = f"report_{stamp}"

    html_content = build_html(sections, run_dt)
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

    pdf_path = output_dir / f"{base_name}.pdf"
    if HAS_WEASYPRINT:
        try:
            WeasyHTML(string=html_content, base_url=str(output_dir)).write_pdf(pdf_path)
            logger.info("Wrote PDF report: %s", pdf_path)
        except Exception as e:
            logger.warning("Could not generate PDF: %s. HTML report was saved.", e)
    else:
        logger.warning("WeasyPrint not available; only HTML report was generated.")

    return str(html_path), str(pdf_path)
