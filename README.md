# MediFlow — multi-tenant Medical Appointment & Queue Management

MediFlow is a Flask + Oracle Database application where **every clinic is an
isolated tenant**. A self-service registration creates a new clinic, generates
its `clinic_id` in Oracle, and creates that clinic's administrator in the same
transaction. Platform administration is separate from clinic administration.

## 1. Database (do this first)

### Fresh install
```
sqlplus clinic/ChangeMe_123@localhost:1521/FREEPDB1
SQL> @sql/01_schema.sql        -- creates CLINICS + tenant keys/constraints
SQL> @sql/02_sample_data.sql   -- optional sample rows for the default clinic
SQL> @sql/03_queries.sql       -- tenant-safe demonstration queries
SQL> @sql/04_tests.sql         -- most statements here SHOULD fail (constraint tests)
SQL> @sql/07_multi_tenant_tests.sql  -- cross-tenant constraint proof
```

### Existing single-clinic database (preserves every record)
Do **not** re-run `sql/01_schema.sql`. Run the idempotent migrations:
```
sqlplus clinic/ChangeMe_123@localhost:1521/FREEPDB1
SQL> @sql/05_auth_and_availability_migration.sql   -- older account fields (once)
SQL> @sql/06_multi_tenant_migration.sql            -- tenant conversion
SQL> @sql/07_multi_tenant_tests.sql                -- optional verification
```
`06_multi_tenant_migration.sql` adds `CLINICS`, `clinic_id` on all four data
tables, direct `clinic_id → clinics` ownership foreign keys, tenant-qualified
composite foreign keys and per-clinic phone uniqueness. It assigns all existing
patients, doctors and appointments to one **default clinic**
(`slug = 'default-clinic'`), keeps the existing `admin` account as a platform
administrator (`clinic_id = NULL`) and keeps the existing `clinic` account in
the default clinic. No table or row is dropped. It is safe to re-run.

`sql/00_create_user.sql` optionally creates the schema; `sql/00_drop_all.sql`
is a destructive reset helper — never run it on a live database.

## 2. Run the web app
```
python -m venv venv
venv\Scripts\activate          (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt

set DB_USER=clinic             (Linux/macOS: export ...)
set DB_PASSWORD=ChangeMe_123
set DB_DSN=localhost:1521/FREEPDB1
set SECRET_KEY=a-stable-random-value
python app.py
```
Open http://127.0.0.1:5000

`DISABLE_LOGIN=1` runs a local demo scoped to the default clinic (resolved
from the database, never hardcoded). Use `FLASK_DEBUG=1` only locally.

## 3. Roles and tenant context

| Role | Clinic context | What it can see |
|---|---|---|
| `PLATFORM_ADMIN` / legacy `ADMIN` | `clinic_id IS NULL` | Global dashboard, reports, all clinics and accounts in **Platform Clinics**; can explicitly choose a clinic to work in |
| `CLINIC_ADMIN` (self-service signup) | its own `clinic_id` | Only its clinic's patients, doctors and appointments |
| `RECEPTIONIST`, `DOCTOR` | its own `clinic_id` | Only its clinic's data (DOCTOR is read-only except availability) |

The clinic id used by every query comes from the signed Flask session and is
**re-read from the database on each request**. No route accepts a clinic id
from a form, query string or JSON body for data access. A platform admin may
call `/admin/clinics/<id>/work` to switch into a tenant; that id is validated
against the `CLINICS` table first.

## 4. Routes

| Route | Purpose |
|---|---|
| `GET, POST /login`, `/signup`, `POST /logout` | Authentication; signup creates a clinic + its administrator |
| `GET /` | Tenant dashboard (global when a platform admin is in the global view) |
| `GET, POST /patients`, `/patients/<id>/edit` | Tenant patient directory and record editing |
| `GET, POST /doctors`, `/doctors/<id>/availability` | Tenant roster and doctor availability |
| `GET, POST /appointments`, `/appointment-ledger` | Booking and tenant-filtered ledger |
| `POST /appointments/<id>/status` | Tenant-owned status transition |
| `GET /queue`, `/queue/<doctor_id>` | Tenant queue boards |
| `GET /reports`, `/insights`, `/api/doctor-stats` | Tenant statistics from the same canonical query |
| `GET /search`, `/settings`, `/help` | Tenant search, counts and help |
| `GET /admin/clinics` | **Platform admin only**: all clinics, accounts and totals |
| `POST /admin/clinics/<id>/work`, `/admin/clinics/global` | Platform admin: enter/leave a tenant workspace |

## 5. Isolation design

* `CLINICS` owns every tenant; `users`, `patients`, `doctors` and
  `appointments` carry `clinic_id`.
* `appointments(patient_id, clinic_id)` and `appointments(doctor_id, clinic_id)`
  have **composite foreign keys** to the parent tables. Oracle therefore
  rejects a booking that mixes records from two clinics even if application
  code is bypassed.
* Phone numbers are unique **per clinic** (`clinic_id, phone`), not globally.
* `chk_users_clinic_scope` forbids a non-platform account without a clinic and
  a platform admin with one.
* Every SELECT/INSERT/UPDATE/DELETE for clinic data goes through `where_clause`
  / `scope_condition` in `app.py`, which adds the tenant predicate from the
  session. `book()` verifies both selected records inside one transaction.
* `DISABLE_LOGIN` demo mode and the notification cache are tenant-keyed too, so
  a cached alert from one clinic cannot appear in another.

## 6. Testing

Database level: `@sql/07_multi_tenant_tests.sql` proves that duplicate phones
are allowed in two clinics and cross-tenant patient/doctor bookings are
rejected by Oracle.

Application level (automated, uses the live configured Oracle schema):
```
python -m unittest tests.test_tenant_isolation -v
```
The suite registers two temporary clinics through `/signup`, adds 5 and 3
patients, logs in as each, tries cross-tenant reads/writes, then signs in as a
platform administrator and verifies the global view, entering/leaving a tenant
workspace. It deletes only the rows it created in `tearDownClass`.

Manual checks: register `Clinic A` and `Clinic B`, add patients to each, sign
in as each in different browsers, and confirm `/patients`, `/api/doctor-stats`,
`/search`, `/reports`, `/appointment-ledger` and dashboard totals differ and
never contain the other clinic's records. Sign in as `admin` and open
**Platform Clinics** to see all tenants.

## 7. Known limitations

* Flask's signed cookie session is the tenant boundary. Keep `SECRET_KEY`
  stable and private; for very high-assurance deployments use Oracle VPD /
  `DBMS_SESSION.SET_CONTEXT` with a connection-level tenant context as a second
  line of defence.
* Platform administrators deliberately have a global view; anyone with that
  role can read every tenant. Only trusted operators should receive it.
* `DOCTOR` accounts can publish availability but cannot edit other records.
* The UI still uses one set of templates; tenant branding beyond the clinic
  name is not implemented.
* There is no server-side CSV/XLSX export endpoint; report "export" is the
  browser print action, which prints only the already-scoped page.
* There are no delete routes for patients, doctors or appointments today. Any
  future delete must follow the same tenant-scoped `WHERE clinic_id = :c`
  pattern used by the existing edits.
* Staff accounts are created with `create_user.py` (by clinic slug), not from
  the browser; the slug lookup supplies the clinic id, never a form field.
