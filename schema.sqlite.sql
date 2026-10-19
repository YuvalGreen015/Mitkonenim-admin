-- Analytics schema (SQLite). Timestamps are unix seconds in UTC.
-- Every event carries a client-generated event_id (UUID). The PRIMARY KEY on it makes
-- ingestion idempotent: an app that retries after a timeout can never be double counted.

CREATE TABLE IF NOT EXISTS install_events (
  event_id     TEXT    PRIMARY KEY,
  device_id    TEXT    NOT NULL,
  platform     TEXT,
  installed_at INTEGER NOT NULL,
  received_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_install_at ON install_events (installed_at);

-- Every user gets a server-generated ID. PRIMARY KEY = a duplicate ID can never be stored.
-- The client keeps a secret token (generated on the device); only its SHA-256 hash is stored,
-- and UNIQUE(token_hash) makes registration idempotent (a retry finds the same user).
CREATE TABLE IF NOT EXISTS users (
  user_id    TEXT    PRIMARY KEY,
  token_hash TEXT    UNIQUE,                 -- NULL only for legacy users migrated from old data
  created_at INTEGER NOT NULL,
  legacy     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_events (
  event_id     TEXT    PRIMARY KEY,
  user_id      TEXT    NOT NULL REFERENCES users(user_id),   -- a login can only belong to a real user
  logged_in_at INTEGER NOT NULL,
  received_at  INTEGER NOT NULL
);
-- Covers the rolling-window scan: range on time, then group by user.
CREATE INDEX IF NOT EXISTS idx_login_at_user ON login_events (logged_in_at, user_id);

-- Counters that are incremented in the SAME transaction that inserts a NEW event.
CREATE TABLE IF NOT EXISTS counters (          -- 'installs_total' = all-time cumulative installs
  name  TEXT    PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_installs (    -- installs per local day; today's row = "live installs"
  day TEXT    PRIMARY KEY,                     -- 'YYYY-MM-DD' in the dashboard time zone
  n   INTEGER NOT NULL
);
