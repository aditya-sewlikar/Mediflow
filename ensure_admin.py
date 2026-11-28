"""Ensure a platform administrator exists and print its login credentials.

Usage:  python ensure_admin.py [username]

Passwords are one-way hashed, so an existing password cannot be recovered.
If the account already exists, this resets its password to a fresh value.
"""
import secrets
import sys

from werkzeug.security import generate_password_hash

from db import execute, query_one

PLATFORM_ROLES = ("ADMIN", "PLATFORM_ADMIN")

username = (sys.argv[1] if len(sys.argv) > 1 else "admin").strip()
password = secrets.token_urlsafe(18)

existing = query_one(
    """SELECT user_id, role FROM users WHERE UPPER(username) = UPPER(:u)""",
    {"u": username},
)

if existing:
    if existing["role"] not in PLATFORM_ROLES:
        raise SystemExit(
            f"Error: '{username}' exists with role {existing['role']}, "
            "which is not a platform administrator."
        )
    execute(
        "UPDATE users SET password_hash = :p WHERE user_id = :i",
        {"p": generate_password_hash(password), "i": existing["user_id"]},
    )
    action = "Existing platform administrator password reset"
else:
    execute(
        """INSERT INTO users
              (clinic_id, full_name, username, email, password_hash, role)
           VALUES (NULL, :n, :u, :e, :p, 'PLATFORM_ADMIN')""",
        {
            "n": "Platform Administrator",
            "u": username,
            "e": f"{username}@mediflow.local",
            "p": generate_password_hash(password),
        },
    )
    action = "Created platform administrator"

print(f"{action}\n  username: {username}\n  password: {password}")
