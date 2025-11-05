# app/sqlite_utils.py
# Back-compat shim so legacy code that imports get_db() keeps working on Postgres/SQLAlchemy.

from contextlib import contextmanager
from typing import Any, Mapping, Optional

from app.extensions import db
from sqlalchemy import text


@contextmanager
def get_db():
    """
    Legacy helper that yielded a sqlite3 connection.
    Now yields a raw DBAPI connection from SQLAlchemy's engine.
    Only used by a few old utils (e.g., rates); safe to remove after migration.
    """
    conn = None
    try:
        conn = db.engine.raw_connection()
        yield conn
    finally:
        if conn is not None:
            conn.close()


def exec_sql(sql: str, params: Optional[Mapping[str, Any]] = None):
    """
    Convenience: run a SQL statement via SQLAlchemy and return a Result object.
    Prefer using this over get_db() for new code.
    """
    return db.session.execute(text(sql), params or {})


def fetchone(sql: str, params: Optional[Mapping[str, Any]] = None):
    res = exec_sql(sql, params)
    row = res.mappings().first()
    return dict(row) if row is not None else None


def fetchall(sql: str, params: Optional[Mapping[str, Any]] = None):
    res = exec_sql(sql, params)
    return [dict(r) for r in res.mappings().all()]
