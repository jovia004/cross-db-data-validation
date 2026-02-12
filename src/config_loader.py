"""Load and validate configuration (env + column mapping)."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Union

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REQUIRED_ENV = [
    "PRIMARY_DB_HOST",
    "PRIMARY_DB_PORT",
    "PRIMARY_DB_NAME",
    "PRIMARY_DB_USER",
    "PRIMARY_DB_PASSWORD",
    "SHADOW_DB_HOST",
    "SHADOW_DB_PORT",
    "SHADOW_DB_NAME",
    "SHADOW_DB_USER",
    "SHADOW_DB_PASSWORD",
]


def load_env() -> None:
    """Load .env from project root. Does not fail if .env is missing."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    load_dotenv(env_path)


def validate_env() -> list[str]:
    """
    Check that all required env vars are set (non-empty).
    Returns list of missing variable names; empty if valid.
    """
    load_env()
    missing = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
    return missing


def _normalize_host(host: str, default_port: str) -> tuple[str, str]:
    """
    Strip URL scheme and trailing slash from host so pasted URLs work.
    If host ends with :<digits>, use that as port and return (host_part, port).
    """
    import re
    s = (host or "").strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :].strip()
            break
    s = s.rstrip("/")
    match = re.match(r"^(.+):(\d+)$", s)
    if match:
        return match.group(1).strip(), match.group(2)
    return s, default_port


def get_db_config() -> dict[str, Any]:
    """Return DB connection config from env. Call after validate_env() passes."""
    load_env()
    primary_host, primary_port = _normalize_host(
        os.getenv("PRIMARY_DB_HOST", ""), os.getenv("PRIMARY_DB_PORT", "1433")
    )
    shadow_host, shadow_port = _normalize_host(
        os.getenv("SHADOW_DB_HOST", ""), os.getenv("SHADOW_DB_PORT", "5432")
    )
    return {
        "primary": {
            "host": primary_host,
            "port": primary_port or "1433",
            "database": os.getenv("PRIMARY_DB_NAME", ""),
            "user": os.getenv("PRIMARY_DB_USER", ""),
            "password": os.getenv("PRIMARY_DB_PASSWORD", ""),
        },
        "shadow": {
            "host": shadow_host,
            "port": shadow_port or "5432",
            "database": os.getenv("SHADOW_DB_NAME", ""),
            "user": os.getenv("SHADOW_DB_USER", ""),
            "password": os.getenv("SHADOW_DB_PASSWORD", ""),
        },
    }


def load_column_mapping(config_path: Optional[Union[str, Path]] = None) -> dict[str, Any]:
    """
    Load column mapping JSON. Path defaults to config/column_mapping.json under project root.
    Raises FileNotFoundError or json.JSONDecodeError on invalid file.
    """
    root = Path(__file__).resolve().parent.parent
    path = Path(config_path) if config_path else root / "config" / "column_mapping.json"
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data


def validate_column_mapping(data: dict[str, Any]) -> list[str]:
    """
    Validate column mapping structure. Returns list of error messages; empty if valid.
    Expects: sample_count (int), tables (dict) with invoice, invoice_detail_item, payment_transaction.
    Each table: primary_table, shadow_table, columns (list), business_key (null or {primary, shadow}).
    """
    errors: list[str] = []
    if "tables" not in data or not isinstance(data["tables"], dict):
        errors.append("Configuration must contain a 'tables' object.")
        return errors

    required_tables = ("invoice", "invoice_detail_item", "payment_transaction")
    for key in required_tables:
        if key not in data["tables"]:
            errors.append(f"Configuration must define table '{key}'.")
            continue
        t = data["tables"][key]
        if not isinstance(t, dict):
            errors.append(f"Table '{key}' must be an object.")
            continue
        for field in ("primary_table", "shadow_table", "columns"):
            if field not in t:
                errors.append(f"Table '{key}' must have '{field}'.")
        if "columns" in t and not isinstance(t.get("columns"), list):
            errors.append(f"Table '{key}' 'columns' must be a list.")
        skipped = t.get("skipped_columns")
        if skipped is not None:
            if not isinstance(skipped, list):
                errors.append(f"Table '{key}' 'skipped_columns' must be a list.")
            elif not all(isinstance(x, str) for x in skipped):
                errors.append(f"Table '{key}' 'skipped_columns' must contain only strings (primary column names).")

    if "sample_count" in data:
        sc = data["sample_count"]
        if not isinstance(sc, int) or sc < 1:
            errors.append("sample_count must be a positive integer.")

    return errors


def load_config(config_path: Optional[Union[str, Path]] = None) -> dict[str, Any]:
    """
    Load full config: env (validated) + column mapping (validated).
    Returns dict with keys: db (from get_db_config), mapping (column_mapping dict), sample_count.
    Raises SystemExit-style messages via logger and raises ValueError on validation failure.
    """
    missing_env = validate_env()
    if missing_env:
        msg = (
            f"Configuration error: missing environment variable(s): {', '.join(missing_env)}. "
            "Please set them in .env (see .env.example)."
        )
        logger.error(msg)
        raise ValueError(msg)

    mapping_data = load_column_mapping(config_path)
    mapping_errors = validate_column_mapping(mapping_data)
    if mapping_errors:
        msg = (
            "Configuration error in column mapping: "
            + "; ".join(mapping_errors)
            + " Please check config/column_mapping.json."
        )
        logger.error(msg)
        raise ValueError(msg)

    sample_count = int(mapping_data.get("sample_count", 10))
    if sample_count < 1:
        sample_count = 10

    return {
        "db": get_db_config(),
        "mapping": mapping_data,
        "sample_count": sample_count,
    }
