-- Analytics schema (PostgreSQL 13+). Same model as schema.sqlite.sql.
-- NOTE: written for production use; the runnable demo in this folder uses SQLite.

CREATE TABLE IF NOT EXISTS install_events (
  event_id     uuid        PRIMARY KEY,                 -- client-generated => idempotent ingestion
  device_id    text        NOT NULL,
  platform     text,
  installed_at timestamptz NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_install_at ON install_events (installed_at);

CREATE TABLE IF NOT EXISTS users (
  user_id    text        PRIMARY KEY,                 -- server-generated; PK => duplicates are impossible
  token_hash text        UNIQUE,                      -- sha256 of the device's secret token (idempotent registration)
  created_at timestamptz NOT NULL DEFAULT now(),
  legacy     boolean     NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS login_events (
  event_id     uuid        PRIMARY KEY,
  user_id      text        NOT NULL REFERENCES users(user_id),
  logged_in_at timestamptz NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_at_user ON login_events (logged_in_at, user_id);

CREATE TABLE IF NOT EXISTS counters (
  name  text   PRIMARY KEY,
  value bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_installs (
  day date   PRIMARY KEY,                               -- local day in the reporting time zone
  n   bigint NOT NULL
);

-- Record one install. Counters move only if the event was actually new.
-- $1 event_id, $2 device_id, $3 platform, $4 installed_at
WITH ins AS (
  INSERT INTO install_events (event_id, device_id, platform, installed_at)
  VALUES ($1, $2, $3, $4)
  ON CONFLICT (event_id) DO NOTHING
  RETURNING installed_at
), total AS (
  INSERT INTO counters (name, value)
  SELECT 'installs_total', count(*) FROM ins HAVING count(*) > 0
  ON CONFLICT (name) DO UPDATE SET value = counters.value + EXCLUDED.value
)
INSERT INTO daily_installs (day, n)
SELECT (installed_at AT TIME ZONE 'Asia/Jerusalem')::date, count(*) FROM ins GROUP BY 1
ON CONFLICT (day) DO UPDATE SET n = daily_installs.n + EXCLUDED.n;

-- Active users: more than 2 logins (i.e. 3 or more) in the rolling last 7 days.
SELECT count(*) AS active_users
FROM (
  SELECT user_id
  FROM login_events
  WHERE logged_in_at >  now() - interval '7 days'
    AND logged_in_at <= now()
  GROUP BY user_id
  HAVING count(*) > 2
) t;
