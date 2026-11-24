-- Sample records for the default clinic. Safe to re-run: each row is keyed by
-- (clinic_id, phone), and appointments are only inserted when missing.
INSERT INTO patients (clinic_id, full_name, phone, email, gender)
SELECT c.clinic_id, 'Aarav Patil', '9876543210', 'aarav@example.com', 'Male'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (SELECT 1 FROM patients p
                    WHERE p.clinic_id = c.clinic_id AND p.phone = '9876543210');

INSERT INTO patients (clinic_id, full_name, phone, email, gender)
SELECT c.clinic_id, 'Anaya Shah', '9876543211', 'anaya@example.com', 'Female'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (SELECT 1 FROM patients p
                    WHERE p.clinic_id = c.clinic_id AND p.phone = '9876543211');

INSERT INTO doctors (clinic_id, full_name, specialization, phone)
SELECT c.clinic_id, 'Dr. Neha Kulkarni', 'General Physician', '9123456780'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (SELECT 1 FROM doctors d
                    WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456780');

INSERT INTO doctors (clinic_id, full_name, specialization, phone)
SELECT c.clinic_id, 'Dr. Rahul Deshmukh', 'Dermatologist', '9123456781'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (SELECT 1 FROM doctors d
                    WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456781');
COMMIT;

SELECT clinic_id, patient_id, full_name, phone FROM patients ORDER BY patient_id;
SELECT clinic_id, doctor_id, full_name, specialization FROM doctors ORDER BY doctor_id;

INSERT INTO appointments (clinic_id, patient_id, doctor_id,
                          appointment_datetime, token_no, status, reason)
SELECT c.clinic_id,
       (SELECT p.patient_id FROM patients p
         WHERE p.clinic_id = c.clinic_id AND p.phone = '9876543210'),
       (SELECT d.doctor_id FROM doctors d
         WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456780'),
       TIMESTAMP '2026-11-05 10:00:00', 1, 'BOOKED', 'Regular check-up'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (
     SELECT 1 FROM appointments a
      WHERE a.clinic_id = c.clinic_id
        AND a.appointment_datetime = TIMESTAMP '2026-11-05 10:00:00'
        AND a.token_no = 1
        AND a.doctor_id = (SELECT d.doctor_id FROM doctors d
                            WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456780'));

INSERT INTO appointments (clinic_id, patient_id, doctor_id,
                          appointment_datetime, token_no, status, reason)
SELECT c.clinic_id,
       (SELECT p.patient_id FROM patients p
         WHERE p.clinic_id = c.clinic_id AND p.phone = '9876543211'),
       (SELECT d.doctor_id FROM doctors d
         WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456780'),
       TIMESTAMP '2026-11-05 10:15:00', 2, 'BOOKED', 'Fever and cough'
  FROM clinics c
 WHERE c.slug = 'default-clinic'
   AND NOT EXISTS (
     SELECT 1 FROM appointments a
      WHERE a.clinic_id = c.clinic_id
        AND a.appointment_datetime = TIMESTAMP '2026-11-05 10:15:00'
        AND a.token_no = 2
        AND a.doctor_id = (SELECT d.doctor_id FROM doctors d
                            WHERE d.clinic_id = c.clinic_id AND d.phone = '9123456780'));
COMMIT;
