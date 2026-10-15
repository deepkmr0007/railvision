-- RAILVISION PostgreSQL schema (generated from the SQLite schema in railvision/services/store.py).
-- Production: due-task claim uses SELECT ... FOR UPDATE SKIP LOCKED; add PostGIS geometry to stations when a station master is loaded.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, display_name TEXT, token_hash TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS tracking_tasks (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
  train_number TEXT NOT NULL, service_date TEXT NOT NULL,
  reference_station TEXT NOT NULL, end_station TEXT,
  interval_min INTEGER NOT NULL, delay_threshold_min INTEGER,
  destinations JSONB NOT NULL, status TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0, quiet_start TEXT, quiet_end TEXT,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  next_run_at TIMESTAMPTZ, last_run_at TIMESTAMPTZ, last_result JSONB, fail_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_tasks_due ON tracking_tasks(status, next_run_at);
CREATE TABLE IF NOT EXISTS observations (
  id BIGSERIAL PRIMARY KEY, trace_id TEXT, train_number TEXT, service_date TEXT,
  kind TEXT, station_code TEXT, event_time TIMESTAMPTZ, km_after REAL, source_id TEXT,
  source_time TIMESTAMPTZ, retrieved_at TIMESTAMPTZ, delay_min INTEGER, is_demo INTEGER, payload JSONB,
  UNIQUE(train_number, service_date, kind, station_code, event_time, km_after, source_id));
CREATE TABLE IF NOT EXISTS passage_events (
  id BIGSERIAL PRIMARY KEY, task_id TEXT, train_number TEXT, service_date TEXT,
  station_code TEXT, event_class TEXT, verification TEXT, crossed INTEGER,
  event_time TIMESTAMPTZ, est_earliest TIMESTAMPTZ, est_latest TIMESTAMPTZ, est_central TIMESTAMPTZ, confidence REAL,
  method TEXT, method_version TEXT, source_id TEXT, source_time TIMESTAMPTZ, is_demo INTEGER,
  created_at TIMESTAMPTZ NOT NULL, supersedes INTEGER);
CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT, type TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL, title TEXT, body TEXT, payload JSONB, is_demo INTEGER,
  created_at TIMESTAMPTZ NOT NULL, read_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS delivery_attempts (
  id BIGSERIAL PRIMARY KEY, notification_id INTEGER, destination TEXT, status TEXT,
  detail TEXT, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS consent_records (
  id BIGSERIAL PRIMARY KEY, subject_hash TEXT NOT NULL, channel TEXT NOT NULL,
  action TEXT NOT NULL, evidence TEXT, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS wa_links (
  phone_hash TEXT PRIMARY KEY, phone TEXT NOT NULL, user_id TEXT, opted_in INTEGER NOT NULL,
  last_inbound_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_links (chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS link_codes (code TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS processed_messages (msg_id TEXT PRIMARY KEY, received_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS chat_context (session_id TEXT PRIMARY KEY, user_id TEXT, context JSONB, updated_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS channel_queue (
  id BIGSERIAL PRIMARY KEY, channel TEXT, text TEXT, task_id TEXT, created_at TIMESTAMPTZ, published_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS audit_events (
  id BIGSERIAL PRIMARY KEY, actor TEXT, action TEXT, target TEXT, detail JSONB, created_at TIMESTAMPTZ NOT NULL);

-- CREATE EXTENSION IF NOT EXISTS postgis;
