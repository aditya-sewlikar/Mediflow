"""Tests for the portable SQL -> MySQL translation layer."""
import unittest

from db import _prepare


class SqlCompatibilityTests(unittest.TestCase):
    def test_named_binds(self):
        sql = "SELECT * FROM clinics WHERE slug = :s AND status = :status"
        self.assertEqual(
            _prepare(sql),
            "SELECT * FROM clinics WHERE slug = %(s)s AND status = %(status)s",
        )

    def test_fetch_first_and_offset(self):
        self.assertEqual(
            _prepare("SELECT * FROM patients ORDER BY patient_id FETCH FIRST 8 ROWS ONLY"),
            "SELECT * FROM patients ORDER BY patient_id LIMIT 8",
        )
        self.assertEqual(
            _prepare("SELECT * FROM appointments ORDER BY id "
                     "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"),
            "SELECT * FROM appointments ORDER BY id LIMIT %(limit)s OFFSET %(offset)s",
        )

    def test_oracle_functions_and_dual(self):
        self.assertEqual(
            _prepare("SELECT NVL(email, ' ') FROM users"),
            "SELECT COALESCE(email, ' ') FROM users",
        )
        self.assertEqual(
            _prepare("SELECT TO_CHAR(patient_id) FROM patients"),
            "SELECT CAST(patient_id AS CHAR) FROM patients",
        )
        self.assertEqual(
            _prepare("SELECT 1 AS ok FROM dual"),
            "SELECT 1 AS ok",
        )
        self.assertEqual(
            _prepare("SELECT TRUNC(CAST(appointment_datetime AS DATE)) FROM appointments"),
            "SELECT DATE(appointment_datetime) FROM appointments",
        )

    def test_datetime_format_escapes_percent_for_pymysql(self):
        prepared = _prepare(
            "SELECT 1 FROM appointments "
            "WHERE TO_CHAR(appointment_datetime, 'YYYY-MM-DD\"T\"HH24:MI') = :w"
        )
        self.assertEqual(
            prepared,
            "SELECT 1 FROM appointments "
            "WHERE DATE_FORMAT(appointment_datetime, '%%Y-%%m-%%dT%%H:%%i') = %(w)s",
        )


if __name__ == "__main__":
    unittest.main()
