-- ============================================================
-- MediFlow migration: sign-up fields + doctor availability
-- Safe for an existing clinic schema. Run once as the clinic user:
--   sqlplus clinic/ChangeMe_123@localhost:1521/FREEPDB1
--   SQL> @sql/05_auth_and_availability_migration.sql
--
-- This script is idempotent: columns/indexes that already exist are
-- skipped. No table is dropped and no patient, doctor, appointment or
-- user record is deleted.
-- ============================================================

PROMPT Adding clinic account fields (full_name, email) ...

BEGIN
  EXECUTE IMMEDIATE 'ALTER TABLE users ADD (full_name VARCHAR2(100))';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1430 THEN  -- ORA-01430: column already exists
      RAISE;
    END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'ALTER TABLE users ADD (email VARCHAR2(100))';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1430 THEN
      RAISE;
    END IF;
END;
/

-- Older accounts have no profile name. Backfill from the username so the
-- column can be mandatory for every account from this point forward.
UPDATE users
   SET full_name = username
 WHERE full_name IS NULL;

COMMIT;

DECLARE
  v_nullable VARCHAR2(1);
BEGIN
  SELECT nullable
    INTO v_nullable
    FROM user_tab_columns
   WHERE table_name = 'USERS'
     AND column_name = 'FULL_NAME';

  IF v_nullable = 'Y' THEN
    EXECUTE IMMEDIATE 'ALTER TABLE users MODIFY (full_name NOT NULL)';
  END IF;
END;
/

-- Case-insensitive uniqueness for usernames and emails. The existing
-- case-sensitive USERNAME unique constraint is kept for compatibility.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM user_indexes
   WHERE index_name = 'UQ_USERS_USERNAME_CI';

  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'CREATE UNIQUE INDEX uq_users_username_ci ON users (UPPER(username))';
  END IF;
END;
/

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM user_indexes
   WHERE index_name = 'UQ_USERS_EMAIL_CI';

  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'CREATE UNIQUE INDEX uq_users_email_ci ON users (LOWER(email))';
  END IF;
END;
/

PROMPT Adding doctor availability ...

BEGIN
  EXECUTE IMMEDIATE 'ALTER TABLE doctors ADD (availability VARCHAR2(20) DEFAULT ''AVAILABLE'' NOT NULL)';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -1430 THEN
      RAISE;
    END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'ALTER TABLE doctors ADD CONSTRAINT chk_doctor_availability CHECK (availability IN (''AVAILABLE'',''BUSY'',''OFFLINE''))';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE != -2264 THEN  -- ORA-02264: name already used by an existing constraint
      RAISE;
    END IF;
END;
/

COMMIT;

PROMPT Migration complete. Verifying ...
COLUMN COLUMN_NAME FORMAT A20
SELECT table_name, column_name, nullable
  FROM user_tab_columns
 WHERE (table_name = 'USERS'   AND column_name IN ('FULL_NAME', 'EMAIL'))
    OR (table_name = 'DOCTORS' AND column_name = 'AVAILABILITY')
 ORDER BY table_name, column_name;
