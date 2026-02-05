"""Database connections and extraction for Primary (SQL Server) and Shadow (PostgreSQL)."""

from .primary import fetch_primary_data
from .shadow import fetch_shadow_data

__all__ = ["fetch_primary_data", "fetch_shadow_data"]
