"""Environment and database configuration for MediFlow.

Precedence (highest first):
  1. real process environment variables
  2. .env.local  (local secrets, never committed)
  3. .env        (shared non-secret defaults)
  4. built-in defaults

.env.example is documentation only and is never loaded.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / ".env"
LOCAL_DOTENV_PATH = BASE_DIR / ".env.local"


def _load_dotenv_files() -> None:
    """Load dotenv files without letting them override real env variables."""
    before = dict(os.environ)
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=False)
    if LOCAL_DOTENV_PATH.exists():
        load_dotenv(LOCAL_DOTENV_PATH, override=True)
    # Real environment variables always win over either file.
    for key, value in before.items():
        os.environ[key] = value


_load_dotenv_files()


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _bool(name: str, default: bool = False) -> bool:
    value = _get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = _get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid integer for %s: %r", name, value)
        return default


@dataclass(frozen=True)
class Config:
    backend: str
    driver: str
    user: str | None
    password: str | None
    host: str | None
    port: int
    database: str | None
    dsn: str | None
    charset: str
    pool_min: int
    pool_max: int
    secret_key: str
    flask_env: str
    clinic_name: str
    support_email: str
    login_enabled: bool
    debug: bool
    session_cookie_secure: bool
    session_lifetime_seconds: int

    @property
    def is_production(self) -> bool:
        return self.flask_env.lower() == "production"

    @property
    def is_mysql(self) -> bool:
        return self.backend == "mysql"

    @property
    def is_oracle(self) -> bool:
        return self.backend == "oracle"

    def safe_summary(self) -> str:
        """Connection description without credentials, safe for logs."""
        if self.is_mysql:
            return (
                f"mysql://{self.host}:{self.port}/{self.database} "
                f"(user={self.user or '<unset>'}, charset={self.charset})"
            )
        return f"oracle dsn={self.dsn or '<unset>'} (user={self.user or '<unset>'})"


def _backend_from_url(url: str) -> tuple[str, str]:
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("mysql", "mysql+pymysql", "mariadb", "mariadb+pymysql"):
        return "mysql", "pymysql"
    if scheme in ("oracle", "oracle+oracledb"):
        return "oracle", "oracledb"
    raise ValueError(
        "Unsupported DATABASE_URL scheme "
        f"{scheme!r}; use mysql:// or oracle:// (mariadb:// is an alias)."
    )


def _parse_url(url: str) -> dict:
    parts = urlsplit(url)
    backend, driver = _backend_from_url(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    values = {
        "backend": backend,
        "driver": driver,
        "user": unquote(parts.username) if parts.username else None,
        "password": unquote(parts.password) if parts.password else None,
        "host": parts.hostname,
        "database": unquote(parts.path.lstrip("/")) or None,
        "dsn": None,
        "charset": (query.get("charset") or [None])[0],
    }
    if backend == "oracle":
        service = (query.get("service_name") or [None])[0] or values["database"]
        netloc = parts.netloc.rsplit("@", 1)[-1]
        values["dsn"] = netloc + (f"/{service}" if service else "")
        values["database"] = service
    elif backend == "mysql":
        values["port"] = parts.port or 3306
    return values


def load_config() -> Config:
    flask_env = (_get("FLASK_ENV", "development") or "development").strip()
    production = flask_env.lower() == "production"

    secret_key = _get("SECRET_KEY")
    if not secret_key:
        if production:
            raise RuntimeError(
                "SECRET_KEY must be set in production. Generate one with "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
                "and store it in .env.local."
            )
        secret_key = secrets.token_urlsafe(48)
        logger.warning(
            "SECRET_KEY is not set; using an ephemeral development key. "
            "Sessions will be invalidated on restart."
        )
    elif len(secret_key) < 32 and production:
        raise RuntimeError("SECRET_KEY must be at least 32 characters in production.")

    disable_login = _bool("DISABLE_LOGIN")
    debug = _bool("FLASK_DEBUG")
    if production and disable_login:
        raise RuntimeError("DISABLE_LOGIN=1 is not allowed in production.")
    if production and debug:
        raise RuntimeError("FLASK_DEBUG=1 is not allowed in production.")

    url = _get("DATABASE_URL")
    parsed = _parse_url(url) if url else {}
    explicit_backend = _get("DB_BACKEND")
    if explicit_backend:
        backend = explicit_backend.strip().lower()
        if backend not in ("mysql", "mariadb", "oracle"):
            raise ValueError(f"Unsupported DB_BACKEND {backend!r}.")
        if backend == "mariadb":
            backend = "mysql"
        if parsed and parsed["backend"] != backend:
            raise ValueError("DB_BACKEND conflicts with the scheme in DATABASE_URL.")
    elif parsed:
        backend = parsed["backend"]
    elif _get("DB_DSN") or _get("DB_USER"):
        # Legacy installations relied on DB_USER/DB_DSN without DB_BACKEND.
        backend = "oracle"
    else:
        backend = "mysql"

    driver = parsed.get("driver") or ("oracledb" if backend == "oracle" else "pymysql")
    user = parsed.get("user") or _get("DB_USER")
    password = parsed.get("password") or _get("DB_PASSWORD")

    if backend == "mysql":
        host = parsed.get("host") or _get("DB_HOST", "127.0.0.1")
        port = parsed.get("port") or _int("DB_PORT", 3306)
        database = parsed.get("database") or _get("DB_NAME", "mediflow")
        dsn = None
        charset = parsed.get("charset") or _get("DB_CHARSET", "utf8mb4")
    else:
        host = parsed.get("host")
        port = parsed.get("port") or 1521
        database = parsed.get("database")
        dsn = parsed.get("dsn") or _get("DB_DSN", "localhost:1521/FREEPDB1")
        charset = parsed.get("charset") or _get("DB_CHARSET", "utf8mb4")

    cookie_secure = _get("SESSION_COOKIE_SECURE")
    secure_cookies = (
        _bool("SESSION_COOKIE_SECURE", production)
        if cookie_secure is None
        else _bool("SESSION_COOKIE_SECURE")
    )

    return Config(
        backend=backend,
        driver=driver,
        user=user,
        password=password,
        host=host,
        port=port,
        database=database,
        dsn=dsn,
        charset=charset,
        pool_min=_int("DB_POOL_MIN", 1),
        pool_max=_int("DB_POOL_MAX", 4),
        secret_key=secret_key,
        flask_env=flask_env,
        clinic_name=_get("CLINIC_NAME", "MediFlow Clinic") or "MediFlow Clinic",
        support_email=_get("SUPPORT_EMAIL", "support@mediflow.clinic")
        or "support@mediflow.clinic",
        login_enabled=not disable_login,
        debug=debug,
        session_cookie_secure=secure_cookies,
        session_lifetime_seconds=_int("SESSION_LIFETIME_SECONDS", 43200),
    )


config = load_config()
