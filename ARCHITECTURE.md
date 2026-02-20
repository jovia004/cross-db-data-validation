# Technical Architecture – Cross-DB Data Validation

## High-level flow

```mermaid
flowchart LR
    subgraph Input
        ENV[.env]
        CONFIG[config/column_mapping.json]
    end

    subgraph CLI
        RUN[CLI Entry]
    end

    subgraph Load
        LOAD[Config Loader]
    end

    subgraph Data
        PRIMARY[(SQL Server\nPrimary)]
        SHADOW[(PostgreSQL\nShadow)]
    end

    subgraph Extract
        EX_P[Primary Extract]
        EX_S[Shadow Extract]
    end

    subgraph Core
        MATCH[Match by\nbusiness key]
        COMPARE[DeepDiff\nCompare]
    end

    subgraph Output
        REPORT[Report Builder\nHTML + PDF]
        FILES[reports/\nreport_YYYY-MM-DD_HH-MM-SS]
    end

    ENV --> LOAD
    CONFIG --> LOAD
    RUN --> LOAD
    LOAD --> EX_P
    LOAD --> EX_S
    EX_P --> PRIMARY
    EX_S --> SHADOW
    PRIMARY --> EX_P
    SHADOW --> EX_S
    EX_P --> MATCH
    EX_S --> MATCH
    MATCH --> COMPARE
    COMPARE --> REPORT
    REPORT --> FILES
```

---

## Component view

```mermaid
flowchart TB
    subgraph External
        SQL[(SQL Server\nInvoice\nInvoiceDetailItem\nPaymentTransaction)]
        PG[(PostgreSQL\ninvoice\ninvoice_item\npayment_transaction)]
    end

    subgraph Application["cross_db_data_validation"]
        CLI[cli.py\nArgparse/Click]
        CFG[config_loader.py\nEnv + column_mapping\nValidation]
        DB_P[db/primary.py\nConnect · Random N · Extract by invoiceid]
        DB_S[db/shadow.py\nConnect · Extract by invoice_guid]
        CMP[compare.py\nNormalize keys · DeepDiff\nClassify diff types]
        RPT[report.py\nSections · TOC links · HTML + PDF]
    end

    subgraph Config
        ENV[.env]
        MAP[column_mapping.json]
    end

    subgraph Output
        HTML[report_*.html]
        PDF[report_*.pdf]
    end

    CLI --> CFG
    CFG --> ENV
    CFG --> MAP
    CFG --> DB_P
    CFG --> DB_S
    DB_P --> SQL
    DB_S --> PG
    DB_P --> CMP
    DB_S --> CMP
    CMP --> RPT
    RPT --> HTML
    RPT --> PDF
```

---

## Data flow (per run)

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Config
    participant Primary
    participant Shadow
    participant Compare
    participant Report

    User->>CLI: run
    CLI->>Config: load env + column_mapping
    Config-->>CLI: config (or abort)
    CLI->>Primary: connect, random N from Invoice
    Primary-->>CLI: invoiceids + uniquecodes
    CLI->>Primary: fetch InvoiceDetailItem, PaymentTransaction by invoiceid
    Primary-->>CLI: rows
    CLI->>Shadow: connect, fetch by invoice_guid
    Shadow-->>CLI: rows
    CLI->>Compare: normalize + DeepDiff per invoice/table
    Compare-->>CLI: differences (values_changed, only_in_*, type_mismatch)
    CLI->>Report: build sections + TOC
    Report-->>CLI: HTML + PDF
    CLI->>User: write to reports/
```

---

## Legend

| Symbol / term | Meaning |
|----------------|--------|
| **Primary** | SQL Server: Invoice, InvoiceDetailItem, PaymentTransaction (link: invoiceid). |
| **Shadow** | PostgreSQL: invoice, invoice_item, payment_transaction (link: invoice_guid). |
| **Config Loader** | Reads .env and column_mapping.json; validates; exposes to extract/compare. |
| **Match** | For table 2 & 3, pair rows by business key from column mapping. |
| **DeepDiff** | Compare normalized row dicts; map result to values_changed, only_in_primary, only_in_shadow, type_mismatch. |
| **Report** | One HTML + one PDF per run; section per invoice; TOC links; filename includes execution datetime. Header shows compared sources (Primary vs Shadow), environment (from config), and run datetime with timezone. |

---

*See PLAN.md for full planning decisions and quality standards.*
