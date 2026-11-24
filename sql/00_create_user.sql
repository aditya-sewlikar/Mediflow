-- OPTIONAL: run once as a privileged user (SYS/SYSTEM) connected to the PDB, e.g.
--   sqlplus sys@localhost:1521/FREEPDB1 as sysdba
-- Skip this if your instructor already gave you a schema.
CREATE USER clinic IDENTIFIED BY "ChangeMe_123";
GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW TO clinic;
ALTER USER clinic QUOTA UNLIMITED ON USERS;
