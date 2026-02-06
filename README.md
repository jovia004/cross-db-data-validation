# Cross-DB Data Validation

Compare invoice data between **SQL Server (Primary)** and **PostgreSQL (Shadow)** and generate HTML + PDF reports.

---

## 1. Where to keep `.env`

Keep the **`.env`** file in the **project root** (the same folder as `run.py`, `PLAN.md`, and `README.md`).  
Copy `.env.example` to `.env` in that folder and fill in your database credentials. Do not commit `.env` to git.

---

## 2. Running on your Mac vs others (git)

- **On your Mac:** Install Python 3.10+ (or 3.9), put `.env` in the project root. Use a **virtual environment** (recommended on macOS to avoid “externally managed” pip errors):

  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python run.py
  ```

  If you see `ModuleNotFoundError: No module named 'dotenv'`, the dependencies are not installed in the environment you’re using—activate the venv (e.g. `source .venv/bin/activate`) and run `pip install -r requirements.txt` again.

- **If you push to git and others use the repo:**  
  They should clone the repo, **not** commit `.env`. Each person (or environment) creates their own `.env` from `.env.example` and fills in their DB credentials. They run `pip install -r requirements.txt` (ideally inside a venv) and then the same commands. So the tool is **portable**: no install “on the machine” beyond Python and pip; optional system dependencies (ODBC driver for SQL Server, WeasyPrint libraries for PDF) depend on their OS—see **Dependencies** below.

---

## 3. Dependencies

- **Python:** 3.10+ recommended; 3.9 supported.
- **Pip packages:** Install with `pip install -r requirements.txt` (pyodbc, psycopg2-binary, python-dotenv, deepdiff, Jinja2, weasyprint).
- **Optional – SQL Server (Primary):** To connect to SQL Server you need an ODBC driver (e.g. [Microsoft ODBC Driver for SQL Server](https://docs.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)). On macOS with Homebrew: `brew install unixodbc` and install the Microsoft driver as per the link.
- **Optional – PDF reports:** WeasyPrint needs system libraries (e.g. Pango, GLib). If they are missing, only HTML reports are generated. See [WeasyPrint first steps](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html). You can skip this and still use HTML reports.

---

## 4. Configuration

- **Column mapping:** Edit **`config/column_mapping.json`** to define:
  - `sample_count`: number of invoices to compare per run (default 10).
  - `invoice_date_column`: (optional) Primary table date column used for “today → last 3 days → last 7 days” sampling (e.g. `"CreatedOn"`). If omitted, sampling is random from the whole table.
  - For each table pair: `primary_table`, `shadow_table`, `business_key` (for detail and payment tables), and `columns` (list of `{ "primary": "...", "shadow": "..." }` for flat or JSON paths).
  - **If you get “Invalid column name 'InvoiceId'”:** Your SQL Server detail/payment tables may use a different column name for the invoice reference. Under `tables.invoice_detail_item` and/or `tables.payment_transaction` set `primary_invoice_fk_column` to the actual column name (e.g. `"InvoiceID"`). Optionally under `tables.invoice` set `primary_invoice_id_column` and `primary_invoice_unique_code_column` if those differ from `"InvoiceId"` and `"UniqueCode"`.

---

## 5. How invoices are chosen (daily runs)

When **`invoice_date_column`** is set in `config/column_mapping.json` (e.g. `"CreatedOn"`), the script picks invoices in three steps so it works for daily runs:

1. **Today:** Randomly select up to `sample_count` invoices from the Primary `Invoice` table where the date column is **today**.
2. **If fewer than `sample_count`:** Fill the remainder by randomly selecting from the **last 3 days** (excluding today), without duplicating already chosen invoices.
3. **If still fewer than `sample_count`:** Fill the rest from the **past 7 days** (again, no duplicates).

If `invoice_date_column` is not set, the script simply picks `sample_count` random invoices from the entire Primary `Invoice` table (previous behavior).

---

## Run

From the project root:

```bash
python run.py
```

Reports are written to **`reports/`** with a filename that includes the run datetime (e.g. `report_2026-02-05_14-30-00.html` and `.pdf` if WeasyPrint is available).

---

## Plan and architecture

- **PLAN.md** – Full planning document (flow, comparison, errors, quality).
- **ARCHITECTURE.md** – Technical architecture and data flow diagrams.
