# Install analytics: three live metrics

| Metric | Definition (as implemented) | How it is computed |
|---|---|---|
| **Live installs** | Counter that increments on every new install event; counts the current local day and starts again at midnight (`DASH_TZ`). | Today's row in `daily_installs`, incremented in the same transaction that inserts the event. |
| **Total installs** | All-time cumulative count of install events. | `counters.installs_total`, incremented in the same transaction. O(1) read. |
| **Active users** | Distinct users with **more than 2** login events (3+) in the rolling last 7 days (`ACTIVE_MIN_LOGINS` to change). The dashboard also shows how many logged in at least once. | `GROUP BY user_id HAVING COUNT(*) >= 3` over `(now-7d, now]`, served by index `(logged_in_at, user_id)`. |

## Run
    python3 server.py                  # http://localhost:8080  (Python 3.9+, no packages)
    python3 simulate.py                # DEMO ONLY: sends simulated events so the dashboard moves (never on a real database)
    python3 -m unittest -v test_metrics

Env: `DASHBOARD_PASSWORD` (**set this**), `PORT`, `DB_PATH`, `DASH_TZ` (e.g. `Asia/Jerusalem`), `ALLOWED_ORIGIN` (your app's address, e.g. `https://yourname.github.io`), `COOKIE_SECURE=1` (when served over https), `TRUST_PROXY=1` (behind a reverse proxy), `INGEST_KEY`, `ACTIVE_MIN_LOGINS`.

## Privacy: only you can see it
- The dashboard and its data (`/`, `/api/metrics`, `/api/stream`) need your password. Without it a visitor sees only a login page; the numbers return 401.
- Set `DASHBOARD_PASSWORD` (10+ characters). If you forget, the server makes a random password for that run and prints it, so it is never open.
- Login makes a signed, HttpOnly, SameSite=Strict cookie valid for 30 days (Log out button on the dashboard). After 8 wrong passwords an address is locked out for 15 minutes.
- Serve it over **https** and set `COOKIE_SECURE=1`; otherwise the password travels unencrypted.
- Only the two endpoints your app must call (`/api/session`, `/api/events/install`) are public, since app users are not you. They accept events only and never return statistics. Set `ALLOWED_ORIGIN` to your app's address so other websites can't call them from a browser.

## Does changing a link reset the count?
- **The server address in `ANALYTICS_URL`:** counts live in the database, not in the link. Pointing the app at a new address that serves the **same database** changes nothing. A new address with a **new, empty database** starts at zero, and every device is registered again with a new user ID on its next login.
- **The address of your app (e.g. GitHub Pages):** browsers keep each device's token per site (scheme + domain), not per page path. Renaming the repository or editing the path under the same `yourname.github.io` keeps the users. Changing the domain or username (a different `*.github.io`, or a custom domain) gives a fresh, empty storage, so every user is counted as a new user on their next login.

## Users and logins (how IDs and tracking work)
- **Unique ID, automatically.** The first time a device calls `POST /api/session`, the server creates a user with a server-generated ID (a short code such as `K7QM-9XDA`; the device shows it once in a small "Your code: …" popup). `users.user_id` is the PRIMARY KEY, so a duplicate can never be stored: if a collision ever happened the insert is rejected and the server draws another ID. (Mathematically "zero chance" is not something random IDs can promise; the database constraint is what makes duplicates impossible.)
- **Logins tied to that ID.** The device keeps a secret random token (made once on the device); the server stores only its SHA-256 hash and uses it to find the user on every later login. A login is stored in the same transaction as the lookup, under the real user ID, and `login_events.user_id` is a foreign key, so a login for an unknown user is rejected instead of silently lost or miscounted. Nobody can log in as someone else's ID without that user's token.
- **Reliable delivery.** Each login has its own `event_id`. `analytics-client.js` queues logins on the device until the server confirms them, so offline logins and lost replies are retried and counted exactly once. A retried registration finds the same user (no duplicate users).

Client: include `analytics-client.js`, set `window.ANALYTICS_URL`, then call `trackLogin()` on every login.

## API
- `POST /api/session` `{"event_id":"<uuid>","token":"<random, 32+ chars>","user_id":"optional","ts":1760000000}` returns `{"user_id","new_user","login_counted"}`. Errors: 401 (token does not match user), 429 (too many new registrations from one address).
- `POST /api/events/install` `{"event_id":"<uuid>","device_id":"...","platform":"android","ts":...}` (needs `X-API-Key` if `INGEST_KEY` is set)
- `GET /api/metrics`: the three numbers, `logged_in_7d`, `registered_users`, installs per day. `GET /api/stream`: Server-Sent Events feed (pushes on each new event, and every 10 s).
- `ts` is optional (unix seconds, UTC); future values are clamped to now + 5 min.

## Upgrading an existing database
Start the new `server.py` on the old `analytics.db`: it migrates automatically. Old user names in `login_events` become `legacy` users (their logins and counts are kept; they have no token, so new logins from them start fresh IDs).

## Design decisions to review
1. **Idempotency.** Clients generate `event_id`; the primary key makes retries safe, and counters move only when a row was really inserted. A reinstall on the same device with a new `event_id` counts as a new install (make `device_id` unique if you want first-install-only).
2. **Live installs** = "since local midnight". If you meant "last N minutes" or "currently installed devices", only `live_installs()` in `server.py` changes.
3. **"Logged in >2 times"** counts login events, not distinct days. For distinct days use `COUNT(DISTINCT date(logged_in_at,'unixepoch'))`.
4. `daily_installs` days use the time zone in force at write time. If you change `DASH_TZ` later, run `rebuild_counters()`.
5. **Scale.** Counters make installs O(1). The active-user query scans 7 days of logins; at tens of millions of rows keep a `login_daily(user_id, day, n)` rollup and sum 7 rows per user.
6. The dashboard endpoints are unauthenticated. Put them behind your auth before exposing them.

`schema.postgres.sql` is the production variant (upserts + active-users query). It was **not executed** here; the tests run against the SQLite version.

## Getting in without typing a password

When the server starts it prints **YOUR PRIVATE LINK** (also saved next to the database as `analytics.db.owner-link.txt`):
`https://your-server/?key=...`. Open it once on your phone; the browser is signed in for 30 days and the key
disappears from the address bar. The key is permanent (derived from the server secret), so keep the link private:
anyone who has it can see the dashboard. The password page remains only as a fallback.
