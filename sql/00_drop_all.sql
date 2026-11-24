-- Reset helper: removes every project table (children first).
-- WARNING: destroys all tenants and all clinical data. Never run on a live database.
DROP TABLE appointments PURGE;
DROP TABLE users PURGE;
DROP TABLE doctors PURGE;
DROP TABLE patients PURGE;
DROP TABLE clinics PURGE;
