"""Database access for Oracle and MySQL/MariaDB.

Callers use named ``:bind`` parameters and portable SQL. The MySQL adapter
translates the few Oracle-specific constructs still present in the application.
"""
from __future__ import annotations

import queue
import re
import threading
from contextlib import contextmanager

from config import config

if config.is_oracle:
    import oracledb

    DatabaseError = oracledb.Error
    IntegrityError = oracledb.IntegrityError
else:
    import pymysql
    from pymysql.err import IntegrityError, MySQLError

    DatabaseError = MySQLError


_pool = None
_pool_lock = threading.Lock()


class Cursor:
    """Small wrapper that normalises binds and exposes the native cursor."""

    def __init__(self, native):
        self.native = native

    def execute(self, sql, params=None):
        self.native.execute(_prepare(sql), params or {})

    def __getattr__(self, name):
        return getattr(self.native, name)


class _MySQLPool:
    """Minimal thread-safe connection pool for PyMySQL."""

    def __init__(self):
        self._idle = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def _connect(self):
        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset=config.charset,
            autocommit=False,
        )

    def acquire(self):
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = None
            if conn is not None:
                try:
                    conn.ping()
                    return conn
                except Exception:
                    self._discard(conn)
                    continue
            with self._lock:
                if self._created < config.pool_max:
                    self._created += 1
                    try:
                        return self._connect()
                    except Exception:
                        self._created -= 1
                        raise
            conn = self._idle.get()
            try:
                conn.ping()
                return conn
            except Exception:
                self._discard(conn)

    def release(self, conn):
        self._idle.put(conn)

    def _discard(self, conn):
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._created = max(0, self._created - 1)


def get_pool():
    """Create the pool lazily so importing the app never requires a live DB."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if config.is_oracle:
                    _pool = oracledb.create_pool(
                        user=config.user,
                        password=config.password,
                        dsn=config.dsn,
                        min=config.pool_min,
                        max=config.pool_max,
                        increment=1,
                        timeout=30,
                    )
                else:
                    _pool = _MySQLPool()
    return _pool


def get_connection():
    pool = get_pool()
    if config.is_oracle:
        return pool.acquire()
    return pool.acquire()


def release_connection(conn):
    pool = get_pool()
    if config.is_oracle:
        pool.release(conn)
    else:
        pool.release(conn)


def close_pool():
    """Release all pooled connections (used by tests and shutdown hooks)."""
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is None:
        return
    if config.is_oracle:
        pool.close()
    else:
        while not pool._idle.empty():
            try:
                pool._idle.get_nowait().close()
            except queue.Empty:
                break


@contextmanager
def connection():
    """Acquire, commit/rollback and release a connection."""
    conn = get_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        release_connection(conn)


def _prepare(sql):
    """Translate portable SQL to the active backend's dialect."""
    if config.is_oracle:
        return sql
    text = sql
    # Oracle pagination -> MySQL LIMIT/OFFSET.
    text = re.sub(
        r"\bOFFSET\s+(\S+)\s+ROWS\s+FETCH\s+NEXT\s+(\S+)\s+ROWS\s+ONLY",
        r"LIMIT \2 OFFSET \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bFETCH\s+FIRST\s+(\S+)\s+ROWS\s+ONLY",
        r"LIMIT \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+FROM\s+dual\b", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bTRUNC\s*\(\s*CAST\s*\(\s*([\w.]+)\s+AS\s+DATE\s*\)\s*\)",
        r"DATE(\1)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bTRUNC\s*\(\s*([\w.]+)\s*\)", r"DATE(\1)", text, flags=re.IGNORECASE
    )
    text = re.sub(r"\bNVL\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
    text = re.sub(
        r"""\bTO_CHAR\s*\(\s*([\w.]+)\s*,\s*'YYYY-MM-DD"T"HH24:MI'\s*\)""",
        r"DATE_FORMAT(\1, '%Y-%m-%dT%H:%i')",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bTO_CHAR\s*\(\s*([\w.]+)\s*\)",
        r"CAST(\1 AS CHAR)",
        text,
        flags=re.IGNORECASE,
    )
    # Convert :name binds to PyMySQL's %(name)s, escaping literal % signs.
    text = re.sub(
        r":([A-Za-z_]\w*)", lambda m: "\x01" + m.group(1) + "\x01", text
    )
    text = text.replace("%", "%%")
    text = re.sub(r"\x01([A-Za-z_]\w*)\x01", r"%(\1)s", text)
    return text


def _rows(cursor):
    cols = [c[0].lower() for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def query(sql, params=None):
    """Run a SELECT with bind variables and return a list of dicts."""
    with connection() as conn:
        with conn.cursor() as native:
            cur = Cursor(native)
            cur.execute(sql, params)
            return _rows(cur)


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run an INSERT/UPDATE/DELETE with bind variables and commit."""
    with connection() as conn:
        with conn.cursor() as native:
            cur = Cursor(native)
            cur.execute(sql, params)
            count = cur.rowcount
        return count


@contextmanager
def transaction():
    """One transaction: yields a cursor, then commits or rolls back.

    Use this whenever several statements must succeed together (for example,
    creating a clinic and its administrator account during sign-up).
    """
    conn = get_connection()
    try:
        with conn.cursor() as native:
            yield Cursor(native)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def insert_and_get_id(cur, sql, params, id_column):
    """Insert a row and return its generated primary key on both backends."""
    values = dict(params or {})
    if config.is_oracle:
        new_id = cur.native.var(oracledb.NUMBER)
        values["new_id"] = new_id
        cur.native.execute(
            sql.rstrip().rstrip(";") + f" RETURNING {id_column} INTO :new_id",
            values,
        )
        return new_id.getvalue()[0]
    cur.execute(sql, values)
    return cur.lastrowid


def schema_column_exists(table, column):
    """Return True when a column exists, without modifying the schema."""
    if config.is_oracle:
        row = query_one(
            """SELECT COUNT(*) AS c FROM user_tab_columns
               WHERE table_name = :t AND column_name = :c""",
            {"t": table.upper(), "c": column.upper()},
        )
    else:
        row = query_one(
            """SELECT COUNT(*) AS c FROM information_schema.columns
               WHERE table_schema = DATABASE()
                 AND table_name = :t AND column_name = :c""",
            {"t": table.lower(), "c": column.lower()},
        )
    return bool(row and int(row["c"]))
