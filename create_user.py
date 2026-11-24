"""Create a login account:  python create_user.py

A CLI helper for seeding the first platform administrator or adding staff to
an existing clinic. Self-service registration from the web app is handled by
``/signup`` and always creates a brand-new clinic plus its administrator.

Existing accounts are never modified.
"""
import getpass
import re

import oracledb
from werkzeug.security import generate_password_hash

from db import execute, query_one

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PLATFORM_ROLES = ("ADMIN", "PLATFORM_ADMIN")
ROLES = PLATFORM_ROLES + ("CLINIC_ADMIN", "RECEPTIONIST", "DOCTOR")

username = input("Username: ").strip()
full_name = input("Full name: ").strip() or username
email = input("Email (optional): ").strip().lower() or None
role = input(
    "Role (PLATFORM_ADMIN / CLINIC_ADMIN / RECEPTIONIST / DOCTOR) [RECEPTIONIST]: "
).strip().upper() or "RECEPTIONIST"
password = getpass.getpass("Password: ")

clinic_id = None
if role not in PLATFORM_ROLES:
    slug = input("Clinic slug [default-clinic]: ").strip().lower() or "default-clinic"
    clinic = query_one("SELECT clinic_id, clinic_name FROM clinics WHERE slug = :s", {"s": slug})
    if not clinic:
        raise SystemExit(f"Error: no clinic with slug '{slug}'. Register it first or run the migration.")
    clinic_id = clinic["clinic_id"]

errors = []
if not USERNAME_RE.fullmatch(username):
    errors.append("Username must be 3-50 letters, numbers, dots, dashes or underscores.")
if not full_name or len(full_name) > 100:
    errors.append("Full name must be 100 characters or fewer.")
if email and (not EMAIL_RE.fullmatch(email) or len(email) > 100):
    errors.append("Enter a valid email address (100 characters or fewer).")
if role not in ROLES:
    errors.append("Role must be PLATFORM_ADMIN, ADMIN, CLINIC_ADMIN, RECEPTIONIST or DOCTOR.")
if len(password) < 8:
    errors.append("Password must be at least 8 characters.")

if errors:
    for message in errors:
        print(f"Error: {message}")
    raise SystemExit(1)

if query_one(
    """SELECT user_id FROM users
       WHERE UPPER(username) = UPPER(:u)
          OR (email IS NOT NULL AND LOWER(email) = LOWER(:e))""",
    {"u": username, "e": email or ""},
):
    raise SystemExit("Error: that username or email is already registered.")

try:
    execute(
        """INSERT INTO users (clinic_id, full_name, username, email, password_hash, role)
           VALUES (:c, :n, :u, :e, :p, :r)""",
        {
            "c": clinic_id,
            "n": full_name,
            "u": username,
            "e": email,
            "p": generate_password_hash(password),
            "r": role,
        },
    )
except oracledb.IntegrityError:
    raise SystemExit("Error: that username or email is already registered.")

scope = "all clinics (platform administrator)" if clinic_id is None else f"clinic_id={clinic_id}"
print(f"User '{username}' created with role {role} for {scope}.")
