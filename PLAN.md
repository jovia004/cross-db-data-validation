# Cross-DB Data Validation – Planning Document

## 1. Project goal

Extract data from **SQL Server (Primary)** and **PostgreSQL (Shadow)**, compare it for a configurable number of random invoices across three table pairs, and generate a **comparison report** (HTML + PDF) with section-wise results and a table of contents.

---

## 2. Databases and tables

| Role   | Database   | Table 1       | Table 2             | Table 3              |
|--------|------------|---------------|---------------------|-----------------------|
| Primary| SQL Server | Invoice       | InvoiceDetailItem   | PaymentTransaction    |
| Shadow | PostgreSQL | invoice       | invoice_item        | payment_transaction   |

- **Primary:** All three tables link by **invoiceid**.
- **Shadow:** All three tables link by **invoice_guid** (= Primary’s **UniqueCode** from Invoice).

---

## 3. Flow (high level)

1. **Load config (env + column mapping) and validate; connect to both DBs.**
   - **On error:** Validate env vars and column_mapping structure first. If invalid, abort and log a **user-understandable** message (e.g. *"Configuration error: PRIMARY_DB_HOST is missing. Please set it in .env."*). If connection fails, abort and log (e.g. *"Could not connect to Primary database. Check host, port, and credentials."*). No retry.
2. **Primary:** Randomly pick **N** rows from **Invoice**; get **invoiceid** and **uniquecode** for each.
   - **On error:** If no invoices are returned, abort and log (e.g. *"No invoices found in Primary. Check that the Invoice table has data."*). No report.
3. **Primary:** For those invoiceids, get all rows from **InvoiceDetailItem** and **PaymentTransaction**.
   - **On error:** Log any query/DB error in plain language (e.g. *"Failed to fetch detail items for Primary. &lt;reason&gt;."*). Abort run so report is not partial.
4. **Shadow:** For those uniquecodes (= invoice_guid), get matching rows from **invoice**, **invoice_item**, **payment_transaction**.
   - **On error:** Same as above; abort and log in user-understandable terms. Do not retry.
5. **For each invoice:** Match rows by **business key** (table 2 & 3); compare using **column mapping** (flat + JSON paths); classify differences (values_changed, only_in_primary, only_in_shadow, type_mismatch).
   - **On error:** If mapping is wrong or a key is missing, log a clear warning (e.g. *"Invoice &lt;guid&gt;: no matching row in Shadow for business key &lt;key&gt;. Reported as only in Primary."*). Continue with other invoices; include this in the report section with a **user-understandable note**.
6. **Build report:** One report (HTML + PDF) with **one section per invoice**, **TOC with hyperlinks** at top, and **execution datetime in filename**; save under **reports/**.
   - **On error:** If file write fails, abort and log (e.g. *"Could not write report to reports/. Check folder exists and permissions."*).

**Logging:** At each step, log progress and errors with levels (INFO/WARNING/ERROR). Every message must be a **clear, user-understandable sentence** (no raw stack traces in console unless in debug mode). **Reporting** (console messages, report notes, and error text) must use **plain, understandable language** so a non-developer can act on it.

---

## 4. Configuration

- **Connections:** Env file (e.g. `.env`). Use `.env.example` with empty parameters; **`.env` in `.gitignore`** (never commit real credentials). **Environment** (e.g. `test` or `prod`) is read from `config/column_mapping.json`; env vars use suffixes `_TEST` / `_PROD` (e.g. `PRIMARY_DB_HOST_TEST`, `PRIMARY_DB_HOST_PROD`).
- **Column mapping:** One file (e.g. `config/column_mapping.json`) with **one section per table**. Each section lists primary ↔ shadow column/path pairs and **business key** for table 2 & 3. Supports **flat columns** and **nested JSON paths** (e.g. `raw_json.Order.Items[0].DiscountForGst`). Optional **skipped_columns** (list of primary column names) excludes those fields from comparison and the report.
- **Sample size:** Configurable (default **10**).

---

## 5. Comparison

- **Tool:** **DeepDiff** (Python) to compare row data normalized to the same logical keys from the column mapping.
- **Difference types:** values_changed, only_in_primary, only_in_shadow, type_mismatch.
- **Structural & type mapping:** Use column mapping consistently; if a path exists only on one side, report only_in_primary / only_in_shadow. For types, avoid false type_mismatch when values are semantically equal (e.g. 100 vs 100.0); report type_mismatch only when values genuinely differ.
- **Replication lag:** No special handling. If Primary has data Shadow doesn’t yet have, mark as difference and report; you’ll say later if we need to change this.

**Input and scope**

- **Input:** For each matched pair (Primary row, Shadow row), build two dicts with the **same logical keys**: each key = one mapped field/path, value = value from that row (flat column or value at JSON path). Only **mapped** columns/paths are compared; unmapped columns are ignored.
- **JSON paths:** Before comparing, extract the value at each path from the relevant column (e.g. `raw_json`) on each side. If the path does not exist on one side, treat as only_in_primary or only_in_shadow and report it.
- **Order:** Compare table 1 (Invoice) first, then all matched pairs for table 2 (InvoiceDetailItem ↔ invoice_item), then table 3 (PaymentTransaction ↔ payment_transaction), per invoice.

**Values and edge cases**

- **NULL and missing:** Rely on DeepDiff: NULL on one side vs value on the other is reported (e.g. values_changed or type_changes). Missing key on one side → only_in_primary / only_in_shadow.
- **Strings:** Compare as-is (no case or whitespace normalization unless we add it later).
- **Numbers:** Avoid false type_mismatch when semantically equal (e.g. int 100 vs float 100.0); coerce or compare in a type-safe way where it’s safe.
- **Dates/timestamps:** Compare as-is for now (no timezone normalization); report both values in the report so the user can judge.

**Output**

- **Per invoice, per table:** Produce a **structured list of differences**: (field name, Primary value, Shadow value, difference_type). This feeds the report builder for the “Key Differences” table and Notes. Optionally aggregate counts (e.g. X values_changed, Y only_in_primary) for the section summary.

---

## 6. Matching rows (table 2 & 3)

- **Multiple rows per invoice** in InvoiceDetailItem / invoice_item and PaymentTransaction / payment_transaction.
- Match rows by **business key** (e.g. line item id, transaction id) from the **column mapping**.
- Business key will be defined in the same column_mapping file; after DB connection works, we’ll propose keys from the DB for your review.

---

## 7. Report

- **Format:** Both **HTML** and **PDF** per run.
- **Scope:** One report per run (all N invoices in one HTML + one PDF).
- **Layout:** **Section per invoice** (like the sample PDF): Header (GUID, sources, date) → Summary → Key Differences table (Field | SQL Server Value | PostgreSQL Value) → Notes → Recommendations.
- **Navigation:** At the **top**, **N hyperlinks** (e.g. “Invoice 1: &lt;guid&gt;” … “Invoice N: &lt;guid&gt;”); clicking scrolls/jumps to that invoice’s section. No “Back to top” link.
- **Filename:** Include **execution datetime** (e.g. `report_2026-02-05_14-30-00.html` / `.pdf`). Save under **reports/**.
- **Future:** Reports may be uploaded elsewhere or emailed later; no upload/email in scope for now.

---

## 8. Error handling and validation

Handle each error case explicitly; log every failure in **user-understandable sentences**. No raw exception dumps in console (unless debug); no jargon without a short explanation.

| Error case | What we do | What we log (example) | What appears in report (if applicable) |
|------------|------------|------------------------|----------------------------------------|
| **Config invalid** (missing env, bad column_mapping) | Abort before DB. | *"Configuration error: &lt;what’s wrong&gt;. Please check .env and config/column_mapping.json."* | N/A (no report). |
| **DB connection failure** (Primary or Shadow) | Abort; no retry. | *"Could not connect to &lt;Primary\|Shadow&gt; database. Check host, port, and credentials in .env."* | N/A. |
| **No invoices from Primary** | Abort; no report. | *"No invoices found in Primary. Check that the Invoice table has data."* | N/A. |
| **Query/DB error** during extract | Abort run. | *"Failed to &lt;fetch invoices \| detail items \| …&gt; from &lt;Primary\|Shadow&gt;. &lt;short reason&gt;."* | N/A. |
| **Invoice missing in Shadow** | Continue run; add a section for that invoice. | WARNING: *"Invoice &lt;guid&gt; not found in Shadow. Included in report as 'Primary only'."* | Section with note: *"This invoice was not found in Shadow. Data below is from Primary only."* |
| **Row missing for business key** (table 2 or 3) | Treat as only_in_primary / only_in_shadow; continue. | WARNING: *"Invoice &lt;guid&gt;: no matching &lt;detail\|payment&gt; row in Shadow for key &lt;key&gt;."* | Reflected in Key Differences (only_in_primary / only_in_shadow) and Notes in **plain language**. |
| **Report file write failure** | Abort. | *"Could not write report to reports/. Check that the folder exists and you have write permission."* | N/A. |

**Reporting in the document:** Notes and recommendations in the generated report must use **short, user-understandable sentences** (e.g. *"This field exists only in SQL Server."*, *"Values differ; check sync or source data."*), not technical codes alone.

---

## 9. Tech and run

- **Language:** Python (**3.10+**).
- **Run:** **CLI first** (e.g. `python run.py` or one command). Library/scriptable API only later if needed.
- **Encoding:** UTF-8 for DB and report I/O.

---

## 10. Quality standards

- **Code:** PEP 8; one formatter (e.g. Black); type hints on public functions and key variables; short docstrings for public functions.
- **Logging:** Console logs with levels (INFO/WARNING/ERROR); **no secrets in logs**. Every log and error message must be a **user-understandable sentence** (actionable, plain language; no raw stack traces in console unless debug).
- **Reporting:** All user-facing text—console messages, report notes, recommendations, and error text—must be **clear, understandable sentences** so a non-developer can follow and act on them.
- **Dependencies:** requirements.txt with pinned or minimum versions.

---

## 11. Folder structure (target)

```
cross_db_data_validation/
├── .env.example
├── .gitignore
├── config/
│   └── column_mapping.json
├── reports/
├── src/
│   ├── __init__.py
│   ├── cli.py
│   ├── config_loader.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── primary.py
│   │   └── shadow.py
│   ├── compare.py
│   └── report.py
├── check_connections.py   # Optional: verify DB connectivity (test/prod)
├── requirements.txt
├── run.py                  # Entry point: python run.py
└── README.md
```

---

## 12. References

- Sample report design: `🧾 Invoice Comparison Report.pdf` (Invoice GUID, sources, Key Differences table, Notes, Recommendations).
- Key mapping: Shadow uses **invoice_guid**; Primary uses **UniqueCode** for the same logical key.

---

*Document version: 1.0 — ready for implementation.*
