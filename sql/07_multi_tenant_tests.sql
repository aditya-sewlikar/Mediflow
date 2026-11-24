-- Database-level isolation proof. Creates two throwaway clinics, checks that
-- the composite foreign keys reject cross-tenant patient/doctor pairings, then
-- removes only the rows it created. Run after 06_multi_tenant_migration.sql:
--
--   SQL> SET SERVEROUTPUT ON
--   SQL> @sql/07_multi_tenant_tests.sql
--
-- Expected output: PASS for both cross-tenant INSERT attempts.
SET SERVEROUTPUT ON
WHENEVER SQLERROR EXIT FAILURE

DECLARE
  v_suffix VARCHAR2(40) := TO_CHAR(SYSTIMESTAMP, 'YYYYMMDDHH24MISSFF');
  v_a      NUMBER;
  v_b      NUMBER;
  v_pa     NUMBER;
  v_pb     NUMBER;
  v_da     NUMBER;
  v_db     NUMBER;
  v_pass   NUMBER := 0;
BEGIN
  INSERT INTO clinics (clinic_name, slug)
  VALUES ('Isolation Test A', 'tenant-test-a-' || v_suffix)
  RETURNING clinic_id INTO v_a;
  INSERT INTO clinics (clinic_name, slug)
  VALUES ('Isolation Test B', 'tenant-test-b-' || v_suffix)
  RETURNING clinic_id INTO v_b;

  -- The same phone number is valid in two different clinics now.
  INSERT INTO patients (clinic_id, full_name, phone)
  VALUES (v_a, 'Tenant A Patient', '7000000001') RETURNING patient_id INTO v_pa;
  INSERT INTO patients (clinic_id, full_name, phone)
  VALUES (v_b, 'Tenant B Patient', '7000000001') RETURNING patient_id INTO v_pb;
  DBMS_OUTPUT.PUT_LINE('PASS: identical phone allowed in two clinics');

  INSERT INTO doctors (clinic_id, full_name, specialization)
  VALUES (v_a, 'Dr. Tenant A', 'General Physician') RETURNING doctor_id INTO v_da;
  INSERT INTO doctors (clinic_id, full_name, specialization)
  VALUES (v_b, 'Dr. Tenant B', 'General Physician') RETURNING doctor_id INTO v_db;

  -- Clinic A's appointment must not accept Clinic B's patient.
  BEGIN
    INSERT INTO appointments (clinic_id, patient_id, doctor_id,
                              appointment_datetime, token_no)
    VALUES (v_a, v_pb, v_da, SYSTIMESTAMP + 1, 9001);
    DBMS_OUTPUT.PUT_LINE('FAIL: cross-tenant patient was accepted');
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE = -2291 THEN
        v_pass := v_pass + 1;
        DBMS_OUTPUT.PUT_LINE('PASS: cross-tenant patient rejected (ORA-02291)');
      ELSE
        RAISE;
      END IF;
  END;

  -- Clinic A's appointment must not accept Clinic B's doctor.
  BEGIN
    INSERT INTO appointments (clinic_id, patient_id, doctor_id,
                              appointment_datetime, token_no)
    VALUES (v_a, v_pa, v_db, SYSTIMESTAMP + 1, 9002);
    DBMS_OUTPUT.PUT_LINE('FAIL: cross-tenant doctor was accepted');
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE = -2291 THEN
        v_pass := v_pass + 1;
        DBMS_OUTPUT.PUT_LINE('PASS: cross-tenant doctor rejected (ORA-02291)');
      ELSE
        RAISE;
      END IF;
  END;

  -- Remove exactly the test rows.
  DELETE FROM appointments WHERE clinic_id IN (v_a, v_b);
  DELETE FROM patients     WHERE clinic_id IN (v_a, v_b);
  DELETE FROM doctors      WHERE clinic_id IN (v_a, v_b);
  DELETE FROM users        WHERE clinic_id IN (v_a, v_b);
  DELETE FROM clinics      WHERE clinic_id IN (v_a, v_b);
  COMMIT;

  IF v_pass <> 2 THEN
    RAISE_APPLICATION_ERROR(-20001, 'Tenant isolation constraint tests failed');
  END IF;
  DBMS_OUTPUT.PUT_LINE('All database-level tenant isolation tests passed.');
END;
/
