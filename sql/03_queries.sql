-- Demonstration queries (multi-tenant safe).
-- Replace 'default-clinic' with your clinic's slug, or bind :clinic_id from
-- the authenticated session in application code. Every clinical query must
-- carry a tenant predicate; the application derives it from the session.

-- A. All appointments with patient and doctor names for ONE clinic
SELECT a.appointment_id, p.full_name AS patient_name,
       d.full_name AS doctor_name, d.specialization,
       a.appointment_datetime, a.token_no, a.status
FROM appointments a
JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
JOIN doctors  d ON a.doctor_id  = d.doctor_id  AND d.clinic_id = a.clinic_id
WHERE a.clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
ORDER BY a.appointment_datetime;

-- B. Waiting queue for one doctor and date (same clinic only)
SELECT a.token_no, p.full_name AS patient_name,
       a.appointment_datetime, a.status
FROM appointments a
JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
WHERE a.clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
  AND a.doctor_id = (SELECT MIN(doctor_id) FROM doctors
                      WHERE clinic_id = (SELECT clinic_id FROM clinics
                                          WHERE slug = 'default-clinic'))
  AND TRUNC(CAST(a.appointment_datetime AS DATE)) = DATE '2026-11-05'
  AND a.status IN ('BOOKED','WAITING','IN_PROGRESS')
ORDER BY a.token_no;

-- C. Check in a patient (tenant-qualified UPDATE)
UPDATE appointments SET status = 'WAITING'
 WHERE token_no = 1 AND status = 'BOOKED'
   AND clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
   AND doctor_id = (SELECT MIN(doctor_id) FROM doctors
                     WHERE clinic_id = (SELECT clinic_id FROM clinics
                                         WHERE slug = 'default-clinic'));
COMMIT;

-- D. Consultation in progress, then completed
UPDATE appointments SET status = 'IN_PROGRESS'
 WHERE token_no = 1 AND status = 'WAITING'
   AND clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic');
UPDATE appointments SET status = 'COMPLETED'
 WHERE token_no = 1 AND status = 'IN_PROGRESS'
   AND clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic');
COMMIT;

-- E. Cancel an appointment
UPDATE appointments SET status = 'CANCELLED'
 WHERE token_no = 2
   AND clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic');
COMMIT;

-- F. Count appointments by status for one clinic
SELECT status, COUNT(*) AS total
FROM appointments
WHERE clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
GROUP BY status ORDER BY status;

-- G. Daily count (extra report) for one clinic
SELECT TRUNC(CAST(appointment_datetime AS DATE)) AS appt_date, COUNT(*) AS total
FROM appointments
WHERE clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
GROUP BY TRUNC(CAST(appointment_datetime AS DATE))
ORDER BY appt_date;

-- H. Doctor-wise patient statistics (canonical definition used by the app),
-- scoped to one clinic. LEFT JOIN keeps doctors with no visits and
-- COUNT(DISTINCT) counts patients, not appointment rows.
SELECT d.doctor_id, d.full_name AS doctor_name, d.specialization,
       COUNT(DISTINCT a.patient_id) AS patient_count,
       COUNT(a.appointment_id) AS total,
       COUNT(CASE WHEN a.status = 'BOOKED' THEN 1 END) AS booked,
       COUNT(CASE WHEN a.status = 'WAITING' THEN 1 END) AS waiting,
       COUNT(CASE WHEN a.status = 'IN_PROGRESS' THEN 1 END) AS in_progress,
       COUNT(CASE WHEN a.status = 'COMPLETED' THEN 1 END) AS completed,
       COUNT(CASE WHEN a.status = 'CANCELLED' THEN 1 END) AS cancelled,
       COUNT(CASE WHEN a.status IN ('BOOKED','WAITING','IN_PROGRESS')
                  THEN 1 END) AS open_count
FROM doctors d
LEFT JOIN appointments a
       ON a.doctor_id = d.doctor_id AND a.clinic_id = d.clinic_id
WHERE d.clinic_id = (SELECT clinic_id FROM clinics WHERE slug = 'default-clinic')
GROUP BY d.doctor_id, d.full_name, d.specialization
ORDER BY d.full_name;

-- I. Platform administrator: totals across every clinic
SELECT (SELECT COUNT(*) FROM clinics) AS clinics,
       (SELECT COUNT(*) FROM users) AS accounts,
       (SELECT COUNT(*) FROM patients) AS patients,
       (SELECT COUNT(*) FROM doctors) AS doctors,
       (SELECT COUNT(*) FROM appointments) AS appointments
FROM dual;
