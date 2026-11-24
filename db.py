"""Oracle connection pool and query helpers. Credentials come from environment variables."""
import os
from contextlib import contextmanager

import oracledb

_pool = None


def get_pool():
    """Create the pool lazily so importing the app never requires a live database."""
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            dsn=os.getenv("DB_DSN", "localhost:1521/FREEPDB1"),
            min=1,
            max=4,
            increment=1,
            timeout=30,
        )
    return _pool


def get_connection():
    return get_pool().acquire()


def _rows(cursor):
    cols = [c[0].lower() for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def query(sql, params=None):
    """Run a SELECT with bind variables and return a list of dicts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return _rows(cur)


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run an INSERT/UPDATE/DELETE with bind variables and commit."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            count = cur.rowcount
        conn.commit()
        return count


@contextmanager
def transaction():
    """One Oracle transaction: yields a cursor, then commits or rolls back.

    Use this whenever several statements must succeed together (for example,
    creating a clinic and its administrator account during sign-up).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
