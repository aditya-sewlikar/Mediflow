-- ============================================================
-- MediFlow - allow multiple doctors without a phone number
-- ------------------------------------------------------------
-- Oracle treats NULLs as values in a composite unique index when another
-- column (clinic_id) is non-NULL. With the old (clinic_id, phone) index only
-- one doctor per clinic could leave the optional phone field blank; the next
-- one failed with ORA-00001 and the website reported a duplicate phone.
--
-- The replacement index still rejects duplicate phone numbers inside a clinic,
-- but maps a blank phone to a marker that is unique per doctor. Existing rows
-- are untouched. Safe to re-run.
-- ============================================================

BEGIN
  FOR r IN (
    SELECT index_name
      FROM user_indexes
     WHERE table_name = 'DOCTORS'
       AND index_name = 'UQ_DOCTORS_CLINIC_PHONE'
  ) LOOP
    EXECUTE IMMEDIATE 'DROP INDEX "' || r.index_name || '"';
  END LOOP;
END;
/

CREATE UNIQUE INDEX uq_doctors_clinic_phone
    ON doctors (clinic_id, NVL(phone, 'NO-PHONE-' || TO_CHAR(doctor_id)));

COMMIT;

PROMPT Optional doctor phones are now supported.
