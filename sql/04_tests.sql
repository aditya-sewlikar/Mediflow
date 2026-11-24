-- Baseline testing checklist. Statements marked EXPECTED ERROR should fail.
-- These tests operate on the default clinic; use sql/07_multi_tenant_tests.sql
-- for explicit cross-tenant isolation checks.

-- 1. Duplicate phone inside the SAME clinic must be rejected (ORA-00001)
INSERT INTO patients (clinic_id, full_name, phone)
VALUES ((SELECT clinic_id FROM clinics WHERE slug = 'default-clinic'),
        'Duplicate Person', '9876543210');

-- 2. Missing required field must be rejected (ORA-01400)
INSERT INTO patients (clinic_id, full_name, phone)
VALUES ((SELECT clinic_id FROM clinics WHERE slug = 'default-clinic'), NULL, '9000000001');

-- 3. A patient without a clinic must be rejected (ORA-01400)
INSERT INTO patients (full_name, phone) VALUES ('No Clinic', '9000000002');

-- 4. Invalid status must be rejected (ORA-02290)
UPDATE appointments SET status = 'ABSENT' WHERE ROWID = (SELECT MIN(ROWID) FROM appointments);

-- 5. Appointment for a non-existent patient (ORA-02291)
INSERT INTO appointments (clinic_id, patient_id, doctor_id, appointment_datetime, token_no)
VALUES ((SELECT clinic_id FROM clinics WHERE slug = 'default-clinic'), 999999,
        (SELECT MIN(doctor_id) FROM doctors
          WHERE clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')),
        SYSTIMESTAMP, 99);

-- 6. Same doctor + same time + same token must be rejected (ORA-00001)
INSERT INTO appointments (clinic_id, patient_id, doctor_id, appointment_datetime, token_no)
SELECT clinic_id, patient_id, doctor_id, appointment_datetime, token_no
FROM appointments FETCH FIRST 1 ROWS ONLY;

-- 7. A non-platform account without a clinic must be rejected (ORA-02290)
INSERT INTO users (full_name, username, password_hash, role)
VALUES ('No Scope', 'no_scope_user', 'x', 'RECEPTIONIST');

-- 8. Parent row cannot be deleted while appointments exist (ORA-02292)
DELETE FROM doctors WHERE doctor_id = (SELECT MIN(doctor_id) FROM doctors);

-- 9. Queue is returned in token order
SELECT token_no, status FROM appointments ORDER BY token_no;

ROLLBACK;
