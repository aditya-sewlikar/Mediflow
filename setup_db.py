"""Configure MediFlow for MySQL/MariaDB and optionally initialise a fresh schema.

Examples:
  python setup_db.py                         # interactive, writes .env.local
  python setup_db.py --url "mariadb+pymysql://user:pass@127.0.0.1:3306/mediflow"
  python setup_db.py --init                  # create DB/user and run schema
  python setup_db.py --init --reset --confirm  # drop project tables first

This script never connects to Oracle and never touches data outside the
configured MySQL database.
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
from pathlib import Path

import pymysql

from config import BASE_DIR, LOCAL_DOTENV_PATH, _parse_url

PROJECT_TABLES = ("appointments", "users", "doctors", "patients", "clinics")
SCHEMA_PATH = BASE_DIR / "sql" / "mysql" / "01_schema.sql"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: Path, updates: dict[str, str], removals=()) -> None:
    values = read_env(path)
    for key in removals:
        values.pop(key, None)
    values.update(updates)
    values.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
    lines = ["# Local secrets. Git-ignored. Generated/updated by setup_db.py."]
    lines += [f"{key}={json.dumps(str(value))}" for key, value in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="MySQL/MariaDB connection string")
    parser.add_argument("--init", action="store_true",
                        help="Create the database/user and run the MySQL schema")
    parser.add_argument("--reset", action="store_true",
                        help="Drop existing MediFlow tables before --init")
    parser.add_argument("--confirm", action="store_true",
                        help="Required together with --reset")
    args = parser.parse_args()
    if args.reset and not (args.init and args.confirm):
        parser.error("--reset requires both --init and --confirm")
    return args


def collect_settings(args) -> dict[str, str]:
    if args.url:
        parsed = _parse_url(args.url)
        if parsed["backend"] != "mysql":
            raise SystemExit("setup_db.py only accepts a mysql:// or mariadb:// URL.")
        return {
            "DATABASE_URL": args.url,
            "DB_BACKEND": "mariadb",
        }, parsed

    print("Configure MariaDB (manual input):")
    host = input("  Host [127.0.0.1]: ").strip() or "127.0.0.1"
    port = input("  Port [3306]: ").strip() or "3306"
    database = input("  Database [mediflow]: ").strip() or "mediflow"
    user = input("  Application user [mediflow]: ").strip() or "mediflow"
    password = getpass.getpass("  Application password: ")
    updates = {
        "DB_BACKEND": "mariadb",
        "DB_HOST": host,
        "DB_PORT": port,
        "DB_NAME": database,
        "DB_USER": user,
        "DB_PASSWORD": password,
        "DB_CHARSET": "utf8mb4",
    }
    parsed = {
        "backend": "mysql",
        "host": host,
        "port": int(port),
        "database": database,
        "user": user,
        "password": password,
    }
    return updates, parsed


def sql_statements(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    text = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )
    return [part.strip() for part in text.split(";") if part.strip()]


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_literal(value: str) -> str:
    return "'" + pymysql.escape_string(value) + "'"


def initialise(settings: dict, args: argparse.Namespace) -> None:
    host = settings["host"]
    port = settings["port"]
    database = settings["database"]
    user = settings["user"]
    password = settings["password"]

    if not database or not user:
        raise SystemExit("A database name and application user are required.")

    admin_user = input(f"MySQL admin user [{user}]: ").strip() or user
    admin_password = getpass.getpass("MySQL admin password: ")
    admin = pymysql.connect(
        host=host, port=port, user=admin_user, password=admin_password,
        charset="utf8mb4", autocommit=True,
    )
    db_ident = quote_identifier(database)
    with admin.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {db_ident} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        if user != admin_user:
            cur.execute(
                f"CREATE USER IF NOT EXISTS {quote_literal(user)}@'%' "
                f"IDENTIFIED BY {quote_literal(password)}"
            )
            cur.execute(
                f"GRANT ALL PRIVILEGES ON {db_ident}.* "
                f"TO {quote_literal(user)}@'%'"
            )
    admin.close()
    print(f"Database {database!r} is ready.")

    app = pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4", autocommit=False,
    )
    try:
        with app.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN (%s,%s,%s,%s,%s)",
                PROJECT_TABLES,
            )
            existing = int(cur.fetchone()[0])
        if existing:
            if not (args.reset and args.confirm):
                raise SystemExit(
                    f"Refusing to overwrite {existing} existing MediFlow table(s). "
                    "Re-run with --init --reset --confirm to drop them first."
                )
            with app.cursor() as cur:
                for table in PROJECT_TABLES:
                    cur.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
            app.commit()
            print("Dropped existing MediFlow tables.")

        for statement in sql_statements(SCHEMA_PATH):
            with app.cursor() as cur:
                cur.execute(statement)
        app.commit()
        print("MySQL schema created successfully.")
    finally:
        app.close()


def main() -> None:
    args = parse_args()
    updates, settings = collect_settings(args)
    removals = (
        ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER",
         "DB_PASSWORD", "DB_CHARSET", "DB_BACKEND")
        if args.url else
        ("DATABASE_URL",)
    )
    write_env(LOCAL_DOTENV_PATH, updates, removals)
    if args.init:
        initialise(settings, args)
    else:
        print("Config written. Run with --init to create the database and schema.")


if __name__ == "__main__":
    main()
