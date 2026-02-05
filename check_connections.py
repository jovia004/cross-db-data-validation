#!/usr/bin/env python3
"""
Check connectivity to Primary (SQL Server) and Shadow (PostgreSQL).
Loads .env from project root; does not require column_mapping.json.
Usage: python3 check_connections.py
"""

import sys
from pathlib import Path

# Project root
_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.config_loader import get_db_config, load_env, validate_env


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


def main() -> None:
    load_env()
    missing = validate_env()
    if missing:
        print("ERROR: Missing env variables:", ", ".join(missing))
        print("Set them in .env (see .env.example).")
        sys.exit(1)

    config = get_db_config()
    primary_conf = config["primary"]
    shadow_conf = config["shadow"]

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
