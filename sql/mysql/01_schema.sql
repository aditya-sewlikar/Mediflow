-- MediFlow multi-tenant schema for MySQL 8 / MariaDB 10.5+
-- Fresh installs only. Creates the default tenant used by migrations and demo mode.
-- All tables use InnoDB so the tenant foreign keys are enforced.

CREATE TABLE clinics (
  clinic_id     BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  clinic_name   VARCHAR(150) NOT NULL,
  slug          VARCHAR(80)  NOT NULL,
  email         VARCHAR(100),
  status        VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
  is_default    TINYINT(1)   NOT NULL DEFAULT 0,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  default_marker TINYINT(1) GENERATED ALWAYS AS (CASE WHEN is_default = 1 THEN 1 END) STORED,
  CONSTRAINT chk_clinic_status CHECK (status IN ('ACTIVE','SUSPENDED')),
  CONSTRAINT chk_clinic_default CHECK (is_default IN (0,1)),
  UNIQUE KEY uq_clinics_slug (slug),
  UNIQUE KEY uq_clinics_default (default_marker)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE patients (
  patient_id    BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  clinic_id     BIGINT NOT NULL,
  full_name     VARCHAR(100) NOT NULL,
  phone         VARCHAR(15)  NOT NULL,
  email         VARCHAR(100),
  gender        VARCHAR(10),
  date_of_birth DATE,
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_patient_clinic FOREIGN KEY (clinic_id) REFERENCES clinics(clinic_id),
  CONSTRAINT uq_patient_tenant_id UNIQUE (patient_id, clinic_id),
  UNIQUE KEY uq_patients_clinic_phone (clinic_id, phone),
  KEY idx_patients_clinic (clinic_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE doctors (
  doctor_id      BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  clinic_id      BIGINT NOT NULL,
  full_name      VARCHAR(100) NOT NULL,
  specialization VARCHAR(80)  NOT NULL,
  phone          VARCHAR(15),
  availability   VARCHAR(20)  NOT NULL DEFAULT 'AVAILABLE',
  CONSTRAINT chk_doctor_availability CHECK (availability IN ('AVAILABLE','BUSY','OFFLINE')),
  CONSTRAINT fk_doctor_clinic FOREIGN KEY (clinic_id) REFERENCES clinics(clinic_id),
  CONSTRAINT uq_doctor_tenant_id UNIQUE (doctor_id, clinic_id),
  -- MySQL/MariaDB unique indexes allow multiple NULLs, so blank doctor phones
  -- are naturally handled without a generated column.
  UNIQUE KEY uq_doctors_clinic_phone (clinic_id, phone),
  KEY idx_doctors_clinic (clinic_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE appointments (
  appointment_id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  clinic_id            BIGINT NOT NULL,
  patient_id           BIGINT NOT NULL,
  doctor_id            BIGINT NOT NULL,
  appointment_datetime DATETIME NOT NULL,
  token_no             INT,
  status               VARCHAR(20) NOT NULL DEFAULT 'BOOKED',
  reason               VARCHAR(250),
  CONSTRAINT fk_appt_clinic FOREIGN KEY (clinic_id) REFERENCES clinics(clinic_id),
  CONSTRAINT fk_appt_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
  CONSTRAINT fk_appt_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id),
  CONSTRAINT fk_appt_patient_tenant FOREIGN KEY (patient_id, clinic_id)
      REFERENCES patients(patient_id, clinic_id),
  CONSTRAINT fk_appt_doctor_tenant FOREIGN KEY (doctor_id, clinic_id)
      REFERENCES doctors(doctor_id, clinic_id),
  CONSTRAINT chk_appt_status CHECK
      (status IN ('BOOKED','WAITING','IN_PROGRESS','COMPLETED','CANCELLED')),
  CONSTRAINT uq_doctor_token UNIQUE (doctor_id, appointment_datetime, token_no),
  KEY fk_appt_patient_tenant_idx (patient_id, clinic_id),
  KEY fk_appt_doctor_tenant_idx (doctor_id, clinic_id),
  KEY idx_appt_doctor_time (doctor_id, appointment_datetime),
  KEY idx_appt_patient (patient_id),
  KEY idx_appt_clinic (clinic_id, appointment_datetime)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE users (
  user_id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  clinic_id     BIGINT,
  full_name     VARCHAR(100) NOT NULL,
  username      VARCHAR(50)  NOT NULL,
  email         VARCHAR(100),
  password_hash VARCHAR(255) NOT NULL,
  role          VARCHAR(20)  NOT NULL DEFAULT 'RECEPTIONIST',
  CONSTRAINT chk_user_role CHECK (role IN
      ('ADMIN','PLATFORM_ADMIN','CLINIC_ADMIN','RECEPTIONIST','DOCTOR')),
  CONSTRAINT fk_users_clinic FOREIGN KEY (clinic_id) REFERENCES clinics(clinic_id),
  CONSTRAINT chk_users_clinic_scope CHECK (
      (role IN ('ADMIN','PLATFORM_ADMIN') AND clinic_id IS NULL) OR
      (role NOT IN ('ADMIN','PLATFORM_ADMIN') AND clinic_id IS NOT NULL)),
  UNIQUE KEY uq_users_username (username),
  UNIQUE KEY uq_users_email (email),
  KEY idx_users_clinic (clinic_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO clinics (clinic_name, slug, email, status, is_default)
VALUES ('MediFlow Clinic', 'default-clinic', NULL, 'ACTIVE', 1);
