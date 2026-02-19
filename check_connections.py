#!/usr/bin/env python3
"""
Check connectivity to Primary (SQL Server) and Shadow (PostgreSQL).
Uses environment (test/prod) from config/column_mapping.json, or --env.
Usage: python3 check_connections.py [--env test|prod]
"""

import argparse
import sys
from pathlib import Path

# Project root
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.config_loader import (
    get_db_config,
    load_env,
    load_column_mapping,
    validate_env,
    ENVIRONMENTS,
)


def check_primary(conf: dict) -> tuple[bool, str]:
    """Try connecting to Primary (SQL Server). Returns (success, message)."""
    try:
        from src.db.primary import _get_connection
        conn = _get_connection(conf)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
            cur.close()
            return (True, "Connected; SELECT 1 OK" if row else "Connected; query returned no row")
        finally:
            conn.close()
    except Exception as e:
        return (False, str(e))


def check_shadow(conf: dict) -> tuple[bool, str]:
    """Try connecting to Shadow (PostgreSQL). Returns (success, message)."""
    try:
        from src.db.shadow import _get_connection
        conn = _get_connection(conf)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
            cur.close()
            return (True, "Connected; SELECT 1 OK" if row else "Connected; query returned no row")
        finally:
            conn.close()
    except Exception as e:
        return (False, str(e))


def _get_environment() -> str:
    """Resolve environment: CLI --env, or config/column_mapping.json, or 'test'."""
    parser = argparse.ArgumentParser(description="Check DB connectivity for test or prod.")
    parser.add_argument(
        "--env",
        choices=ENVIRONMENTS,
        default=None,
        help="Override environment (default: from config/column_mapping.json, else test)",
    )
    args = parser.parse_args()
    if args.env:
        return args.env
    try:
        mapping = load_column_mapping()
        env = (mapping.get("environment") or "test").strip().lower()
        return env if env in ENVIRONMENTS else "test"
    except FileNotFoundError:
        return "test"


def main() -> None:
    load_env()
    environment = _get_environment()
    missing = validate_env(environment)
    if missing:
        print("ERROR: Missing env variables for environment", repr(environment) + ":", ", ".join(missing))
        print("Set them in .env (see .env.example). Use *_TEST / *_PROD suffix.")
        sys.exit(1)

    config = get_db_config(environment)
    primary_conf = config["primary"]
    shadow_conf = config["shadow"]

    print(f"Environment: {environment}")
    print("Checking database connections (from .env)...\n")

    # Primary
    print("Primary (SQL Server):")
    print(f"  Host: {primary_conf['host']}:{primary_conf.get('port', '1433')}  Database: {primary_conf['database']}")
    p_ok, p_msg = check_primary(primary_conf)
    if p_ok:
        print(f"  OK — {p_msg}")
    else:
        print(f"  FAIL — {p_msg}")
    print()

    # Shadow
    print("Shadow (PostgreSQL):")
    print(f"  Host: {shadow_conf['host']}:{shadow_conf.get('port', '5432')}  Database: {shadow_conf['database']}")
    s_ok, s_msg = check_shadow(shadow_conf)
    if s_ok:
        print(f"  OK — {s_msg}")
    else:
        print(f"  FAIL — {s_msg}")
    print()

    sys.exit(0 if (p_ok and s_ok) else 1)


if __name__ == "__main__":
    main()
