"""End-to-end tenant isolation tests against the configured Oracle or MySQL schema.

Run with:  python -m unittest tests.test_tenant_isolation -v

The suite uses the Flask test client, registers two throwaway clinics through
the real /signup route and removes only the rows it created in tearDownClass.
"""
import re
import secrets
import unittest
import uuid
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app import app, slugify
from config import config
from db import execute, query, query_one


def first_int(pattern, text):
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        cls.suffix = uuid.uuid4().hex[:10]
        cls.csrf = "tenant-test-csrf"
        cls.clients = {key: app.test_client() for key in ("A", "B", "ADMIN")}
        cls.clinic_ids = {}
        cls.patients = {"A": [], "B": []}
        cls.doctors = {}
        cls.appointments = {}
        cls.admin_id = None

        for key, label in (("A", "Alpha"), ("B", "Beta")):
            name = f"Isolation {label} Clinic {cls.suffix}"
            username = f"{label.lower()}admin{cls.suffix}"
            response = cls.post(key, "/signup", {
                "clinic_name": name,
                "full_name": f"{label} Administrator",
                "username": username,
                "email": f"{username}@example.test",
                "password": "Clinic12345",
                "confirm_password": "Clinic12345",
            })
            assert response.status_code == 302, response.get_data(as_text=True)[:800]
            row = query_one(
                "SELECT clinic_id FROM clinics WHERE slug = :s", {"s": slugify(name)}
            )
            assert row, f"clinic {name} was not created"
            cls.clinic_ids[key] = int(row["clinic_id"])
            cls.login(key, username, "Clinic12345")

        for key, count, prefix in (("A", 5, "ALPHA"), ("B", 3, "BETA")):
            for index in range(count):
                patient_id, name, _ = cls.add_patient(key, prefix, index)
                cls.patients[key].append((patient_id, name))
            doctor_id, doctor_name = cls.add_doctor(key, prefix)
            cls.doctors[key] = (doctor_id, doctor_name)

        admin_username = f"platform{cls.suffix}"
        execute(
            """INSERT INTO users (full_name, username, email, password_hash, role)
               VALUES (:n, :u, :e, :p, 'PLATFORM_ADMIN')""",
            {"n": "Test Platform Admin", "u": admin_username,
             "e": f"{admin_username}@example.test",
             "p": generate_password_hash("Platform12345")},
        )
        row = query_one("SELECT user_id FROM users WHERE username = :u", {"u": admin_username})
        cls.admin_id = int(row["user_id"])
        cls.login("ADMIN", admin_username, "Platform12345")

    @classmethod
    def tearDownClass(cls):
        for clinic_id in cls.clinic_ids.values():
            for table in ("appointments", "patients", "doctors", "users", "clinics"):
                execute(f"DELETE FROM {table} WHERE clinic_id = :c", {"c": clinic_id})
        if cls.admin_id:
            execute("DELETE FROM users WHERE user_id = :i", {"i": cls.admin_id})

    @classmethod
    def post(cls, key, path, data):
        with cls.clients[key].session_transaction() as sess:
            sess["_csrf"] = cls.csrf
        return cls.clients[key].post(path, data={**data, "_csrf": cls.csrf})

    @classmethod
    def login(cls, key, username, password):
        response = cls.post(key, "/login", {"username": username, "password": password})
        assert response.status_code == 302, response.get_data(as_text=True)[:800]
        return response

    @staticmethod
    def unique_phone():
        while True:
            phone = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
            if not query_one("SELECT 1 FROM patients WHERE phone = :p", {"p": phone}):
                return phone

    @classmethod
    def add_patient(cls, key, prefix, index):
        name = f"{prefix} Patient {index} {cls.suffix}"
        phone = cls.unique_phone()
        response = cls.post(key, "/patients", {
            "full_name": name, "phone": phone, "email": "", "gender": "", "date_of_birth": "",
        })
        assert response.status_code == 302, response.get_data(as_text=True)[:800]
        row = query_one(
            "SELECT patient_id FROM patients WHERE clinic_id = :c AND phone = :p",
            {"c": cls.clinic_ids[key], "p": phone},
        )
        assert row, f"patient {name} was not created"
        return int(row["patient_id"]), name, phone

    @classmethod
    def add_doctor(cls, key, prefix):
        name = f"Dr. {prefix} {cls.suffix}"
        response = cls.post(key, "/doctors", {
            "full_name": name, "specialization": "General Physician", "phone": "",
        })
        assert response.status_code == 302, response.get_data(as_text=True)[:800]
        row = query_one(
            "SELECT doctor_id FROM doctors WHERE clinic_id = :c AND full_name = :n",
            {"c": cls.clinic_ids[key], "n": name},
        )
        assert row, f"doctor {name} was not created"
        return int(row["doctor_id"]), name

    @classmethod
    def book(cls, key, patient_id, doctor_id, when):
        response = cls.post(key, "/appointments", {
            "patient_id": str(patient_id), "doctor_id": str(doctor_id),
            "appointment_datetime": when, "token_no": "", "reason": "Tenant isolation test",
        })
        assert response.status_code == 302, response.get_data(as_text=True)[:800]
        row = query_one(
            """SELECT appointment_id FROM appointments
                WHERE clinic_id = :c AND patient_id = :p AND doctor_id = :d
                  AND TO_CHAR(appointment_datetime, 'YYYY-MM-DD\"T\"HH24:MI') = :w""",
            {"c": cls.clinic_ids[key], "p": patient_id, "d": doctor_id, "w": when},
        )
        assert row, "appointment was not created"
        return int(row["appointment_id"])

    def test_00_every_tenant_table_has_a_clinic_foreign_key(self):
        """A converted legacy schema must have the same ownership FKs as a fresh one."""
        if config.is_mysql:
            rows = query(
                """SELECT kcu.TABLE_NAME AS table_name, kcu.COLUMN_NAME AS column_name
                     FROM information_schema.KEY_COLUMN_USAGE kcu
                     JOIN information_schema.TABLE_CONSTRAINTS tc
                       ON tc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                      AND tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                      AND tc.TABLE_NAME = kcu.TABLE_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
                      AND kcu.REFERENCED_TABLE_NAME = 'clinics'
                      AND kcu.TABLE_NAME IN
                          ('patients','doctors','appointments','users')"""
            )
            owned = {
                (row["table_name"].upper(), row["column_name"].upper())
                for row in rows
            }
        else:
            rows = query(
                """SELECT c.table_name, cc.column_name
                     FROM user_constraints c
                     JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
                     JOIN user_constraints parent
                       ON parent.constraint_name = c.r_constraint_name
                    WHERE c.constraint_type = 'R'
                      AND parent.table_name = 'CLINICS'
                      AND c.table_name IN
                          ('PATIENTS', 'DOCTORS', 'APPOINTMENTS', 'USERS')"""
            )
            owned = {(row["table_name"], row["column_name"]) for row in rows}
        for table in ("PATIENTS", "DOCTORS", "APPOINTMENTS", "USERS"):
            self.assertIn((table, "CLINIC_ID"), owned)

    def test_01_registration_created_separate_clinics(self):
        self.assertNotEqual(self.clinic_ids["A"], self.clinic_ids["B"])
        self.assertEqual(len(self.patients["A"]), 5)
        self.assertEqual(len(self.patients["B"]), 3)

    def test_02_clinic_a_sees_only_its_five_patients(self):
        body = self.clients["A"].get("/patients").get_data(as_text=True)
        for _, name in self.patients["A"]:
            self.assertIn(name, body)
        for _, name in self.patients["B"]:
            self.assertNotIn(name, body)
        self.assertEqual(
            5, first_int(r"Total patients</p>\s*<p class=\"kpi-value\">(\d+)", body)
        )

    def test_03_clinic_b_sees_only_its_three_patients(self):
        body = self.clients["B"].get("/patients").get_data(as_text=True)
        for _, name in self.patients["B"]:
            self.assertIn(name, body)
        for _, name in self.patients["A"]:
            self.assertNotIn(name, body)
        self.assertEqual(
            3, first_int(r"Total patients</p>\s*<p class=\"kpi-value\">(\d+)", body)
        )

    def test_04_cross_tenant_reads_are_denied(self):
        patient_id, _ = self.patients["A"][0]
        doctor_id, _ = self.doctors["A"]
        self.assertEqual(404, self.clients["B"].get(f"/patients/{patient_id}/edit").status_code)
        self.assertEqual(404, self.clients["B"].get(f"/queue/{doctor_id}").status_code)
        self.assertEqual(
            404,
            self.post("B", f"/doctors/{doctor_id}/availability",
                      {"availability": "OFFLINE"}).status_code,
        )

    def test_05_cross_tenant_appointment_update_is_denied(self):
        when = (datetime.now() + timedelta(days=2)).replace(
            second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M")
        appointment_id = self.book(
            "A", self.patients["A"][0][0], self.doctors["A"][0], when
        )
        response = self.post(
            "B", f"/appointments/{appointment_id}/status", {"status": "CANCELLED"}
        )
        self.assertEqual(404, response.status_code)
        row = query_one(
            "SELECT status FROM appointments WHERE appointment_id = :i", {"i": appointment_id}
        )
        self.assertEqual("BOOKED", row["status"])
        # The owning clinic can still move its own appointment.
        response = self.post(
            "A", f"/appointments/{appointment_id}/status", {"status": "WAITING"}
        )
        self.assertEqual(302, response.status_code)
        row = query_one(
            "SELECT status FROM appointments WHERE appointment_id = :i", {"i": appointment_id}
        )
        self.assertEqual("WAITING", row["status"])

    def test_06_platform_admin_sees_all_clinics(self):
        body = self.clients["ADMIN"].get("/admin/clinics").get_data(as_text=True)
        self.assertIn(f"Isolation Alpha Clinic {self.suffix}", body)
        self.assertIn(f"Isolation Beta Clinic {self.suffix}", body)
        patients_body = self.clients["ADMIN"].get("/patients").get_data(as_text=True)
        for _, name in self.patients["A"] + self.patients["B"]:
            self.assertIn(name, patients_body)
        stats = self.clients["ADMIN"].get("/api/doctor-stats").get_json()
        doctor_ids = {int(row["doctor_id"]) for row in stats["doctors"]}
        self.assertIn(self.doctors["A"][0], doctor_ids)
        self.assertIn(self.doctors["B"][0], doctor_ids)

    def test_07_stats_search_filters_reports_and_exports_are_scoped(self):
        # Keep this on today's date; +2 hours crosses midnight in the evening.
        today = datetime.now().replace(second=0, microsecond=0).strftime(
            "%Y-%m-%dT%H:%M"
        )
        appointment_id = self.book(
            "A", self.patients["A"][1][0], self.doctors["A"][0], today
        )
        a_name = self.patients["A"][1][1]
        b_name = self.patients["B"][0][1]
        # Give clinic B an appointment too, so the ledger/search assertions
        # prove that B can find its own record while A cannot.
        self.book(
            "B", self.patients["B"][0][0], self.doctors["B"][0],
            (datetime.now() + timedelta(days=3)).replace(
                second=0, microsecond=0
            ).strftime("%Y-%m-%dT%H:%M"),
        )

        a_dashboard = self.clients["A"].get("/").get_data(as_text=True)
        b_dashboard = self.clients["B"].get("/").get_data(as_text=True)
        self.assertIn(a_name, a_dashboard)
        self.assertNotIn(a_name, b_dashboard)

        a_search = self.clients["A"].get("/search", query_string={"q": b_name})
        b_search = self.clients["B"].get("/search", query_string={"q": b_name})
        self.assertEqual(0, first_int(r"(\d+) results? for", a_search.get_data(as_text=True)))
        self.assertGreaterEqual(
            first_int(r"(\d+) results? for", b_search.get_data(as_text=True)), 1
        )

        a_ledger = self.clients["A"].get(
            "/appointment-ledger", query_string={"q": b_name}
        ).get_data(as_text=True)
        b_ledger = self.clients["B"].get(
            "/appointment-ledger", query_string={"q": b_name}
        ).get_data(as_text=True)
        self.assertEqual(0, first_int(r"(\d+) records? match the current filters", a_ledger))
        self.assertGreaterEqual(
            first_int(r"(\d+) records? match the current filters", b_ledger), 1
        )

        a_report = self.clients["A"].get("/reports", query_string={"range": "all"})
        self.assertNotIn(b_name, a_report.get_data(as_text=True))
        self.assertIn(a_name, a_report.get_data(as_text=True))

        a_stats = self.clients["A"].get("/api/doctor-stats").get_json()
        b_stats = self.clients["B"].get("/api/doctor-stats").get_json()
        self.assertEqual(
            {self.doctors["A"][0]}, {int(r["doctor_id"]) for r in a_stats["doctors"]}
        )
        self.assertEqual(
            {self.doctors["B"][0]}, {int(r["doctor_id"]) for r in b_stats["doctors"]}
        )

        for key, expected in (("A", 5), ("B", 3)):
            body = self.clients[key].get("/settings").get_data(as_text=True)
            self.assertEqual(
                expected,
                first_int(r"Patients on record</span>\s*<strong>(\d+)</strong>", body),
            )
        # Explicitly confirm the created appointment belongs to clinic A only.
        row = query_one(
            "SELECT clinic_id FROM appointments WHERE appointment_id = :i",
            {"i": appointment_id},
        )
        self.assertEqual(self.clinic_ids["A"], int(row["clinic_id"]))

    def test_08_platform_admin_workspace_label_survives_revalidation(self):
        clinic_name = f"Isolation Alpha Clinic {self.suffix}"
        response = self.post(
            "ADMIN", f"/admin/clinics/{self.clinic_ids['A']}/work", {}
        )
        self.assertEqual(302, response.status_code)
        # The follow-up GET revalidates the session; the acting clinic's name
        # must survive that re-read instead of falling back to CLINIC_NAME.
        body = self.clients["ADMIN"].get("/").get_data(as_text=True)
        self.assertIn(clinic_name, body)
        self.assertNotIn("All clinics · platform admin", body)

        response = self.post("ADMIN", "/admin/clinics/global", {})
        self.assertEqual(302, response.status_code)
        body = self.clients["ADMIN"].get("/admin/clinics").get_data(as_text=True)
        self.assertIn("All clinics · platform admin", body)

    def test_09_platform_admin_global_view_can_add_records(self):
        """The global view shows add buttons, so its forms must actually work."""
        body = self.clients["ADMIN"].get("/patients").get_data(as_text=True)
        self.assertIn('name="clinic_id"', body)

        patient_name = f"Platform Added Patient {self.suffix}"
        phone = self.unique_phone()
        response = self.post("ADMIN", "/patients", {
            "full_name": patient_name, "phone": phone,
            "clinic_id": str(self.clinic_ids["B"]),
        })
        self.assertEqual(302, response.status_code, response.get_data(as_text=True)[:800])
        row = query_one("SELECT clinic_id FROM patients WHERE phone = :p", {"p": phone})
        self.assertEqual(self.clinic_ids["B"], int(row["clinic_id"]))

        doctor_name = f"Platform Added Doctor {self.suffix}"
        response = self.post("ADMIN", "/doctors", {
            "full_name": doctor_name, "specialization": "General Physician",
            "clinic_id": str(self.clinic_ids["B"]),
        })
        self.assertEqual(302, response.status_code, response.get_data(as_text=True)[:800])
        row = query_one(
            "SELECT clinic_id FROM doctors WHERE full_name = :n", {"n": doctor_name}
        )
        self.assertEqual(self.clinic_ids["B"], int(row["clinic_id"]))

        # Phone is optional: two doctors without one in the same clinic must
        # both be accepted (the old unique index rejected the second one).
        second_name = f"Second Platform Doctor {self.suffix}"
        response = self.post("ADMIN", "/doctors", {
            "full_name": second_name, "specialization": "General Physician",
            "clinic_id": str(self.clinic_ids["B"]),
        })
        self.assertEqual(302, response.status_code, response.get_data(as_text=True)[:800])
        row = query_one("SELECT phone FROM doctors WHERE full_name = :n", {"n": second_name})
        self.assertIsNone(row["phone"])

        # An unselected or forged clinic must never create an orphan record.
        response = self.post("ADMIN", "/patients", {
            "full_name": f"No Clinic Patient {self.suffix}",
            "phone": self.unique_phone(), "clinic_id": "999999999",
        })
        self.assertEqual(200, response.status_code)
        self.assertIn("Choose the clinic", response.get_data(as_text=True))

    def test_10_patient_cannot_have_two_active_appointments(self):
        """A patient must finish or cancel a visit before booking another one."""
        patient_id, _ = self.patients["A"][2]
        doctor_id, _ = self.doctors["A"]
        first = (datetime.now() + timedelta(days=5)).replace(
            second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M")
        second = (datetime.now() + timedelta(days=6)).replace(
            second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M")

        appointment_id = self.book("A", patient_id, doctor_id, first)
        response = self.post("A", "/appointments", {
            "patient_id": str(patient_id), "doctor_id": str(doctor_id),
            "appointment_datetime": second, "token_no": "",
            "reason": "Should be rejected",
        })
        self.assertEqual(200, response.status_code)
        self.assertIn("already has an active appointment", response.get_data(as_text=True))
        count = query_one(
            """SELECT COUNT(*) AS c FROM appointments
                WHERE clinic_id = :c AND patient_id = :p""",
            {"c": self.clinic_ids["A"], "p": patient_id},
        )
        self.assertEqual(1, int(count["c"]))

        # Cancelling the open visit releases the patient for a new booking.
        response = self.post(
            "A", f"/appointments/{appointment_id}/status", {"status": "CANCELLED"}
        )
        self.assertEqual(302, response.status_code)
        self.book("A", patient_id, doctor_id, second)


if __name__ == "__main__":
    unittest.main()
