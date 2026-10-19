import re
import os, tempfile, unittest
from datetime import datetime
from zoneinfo import ZoneInfo
os.environ["DASH_TZ"] = "Asia/Jerusalem"
import server as S

def tok(name): return (name + "-" * 40)[:40] + "x" * 8

NOW = int(datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem")).timestamp())
D = 86400

class T(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.f.close()
        self.db = S.connect(self.f.name); S.init_db(self.db); S.clock = lambda: NOW
    def tearDown(self): self.db.close(); os.unlink(self.f.name)

    def inst(self, eid, ts=None): return S.record_install(self.db, eid, "dev-" + eid, "android", ts)
    def login(self, eid, user, ts):                      # user = a device name; its token is derived from it
        return S.create_session(self.db, eid, tok(user), ts=ts)[2]

    def test_install_counts_and_idempotency(self):
        self.assertTrue(self.inst("a")); self.assertTrue(self.inst("b"))
        self.assertFalse(self.inst("a"))                 # retry of the same event
        self.assertEqual(S.total_installs(self.db), 2); self.assertEqual(S.live_installs(self.db), 2)

    def test_live_is_today_total_is_all_time(self):
        self.inst("today", NOW - 3600); self.inst("yday", NOW - D); self.inst("old", NOW - 30 * D)
        self.assertEqual(S.total_installs(self.db), 3); self.assertEqual(S.live_installs(self.db), 1)

    def test_local_midnight_boundary(self):
        # 2026-10-18 21:30 UTC is already 2026-10-19 00:30 in Jerusalem (UTC+3) -> counts as today
        ts = int(datetime(2026, 10, 18, 21, 30, tzinfo=ZoneInfo("UTC")).timestamp())
        self.inst("late", ts); self.assertEqual(S.live_installs(self.db), 1)
        ts2 = int(datetime(2026, 10, 18, 20, 30, tzinfo=ZoneInfo("UTC")).timestamp())  # 23:30 local yesterday
        self.inst("early", ts2); self.assertEqual(S.live_installs(self.db), 1)

    def test_future_timestamp_is_clamped(self):
        self.inst("skew", NOW + 10 * D); self.assertEqual(S.live_installs(self.db), 1)

    def test_active_users_threshold_is_more_than_two(self):
        for i in range(2): self.login(f"two{i}", "u2", NOW - i * 3600)
        for i in range(3): self.login(f"three{i}", "u3", NOW - i * 3600)
        self.assertEqual(S.active_users(self.db), 1)     # only u3

    def test_rolling_window_and_exact_boundary(self):
        self.login("a", "u", NOW - 6 * D); self.login("b", "u", NOW - 3 * D); self.login("c", "u", NOW - 60)
        self.assertEqual(S.active_users(self.db), 1)
        S.clock = lambda: NOW + 2 * D                    # 6-day-old login is now 8 days old -> expired
        self.assertEqual(S.active_users(self.db), 0)
        self.login("d", "w", NOW - 7 * D + 2 * D)        # exactly 7 days before the new "now": excluded
        self.login("e", "w", NOW + 2 * D - 100); self.login("f", "w", NOW + 2 * D - 50)
        self.assertEqual(S.active_users(self.db), 0)

    def test_duplicate_login_events_not_double_counted(self):
        for _ in range(5): self.login("same", "u", NOW - 10)
        self.login("x", "u", NOW - 20)
        self.assertEqual(S.active_users(self.db), 0)     # only 2 distinct login events

    def test_users_counted_once_however_many_logins(self):
        for i in range(10): self.login(f"l{i}", "heavy", NOW - i * 60)
        self.assertEqual(S.active_users(self.db), 1)

    def test_rebuild_counters_matches_log(self):
        for i in range(5): self.inst(f"i{i}", NOW - i * D)
        self.db.execute("UPDATE counters SET value=999"); self.db.commit()
        S.rebuild_counters(self.db)
        self.assertEqual(S.total_installs(self.db), 5); self.assertEqual(S.live_installs(self.db), 1)

    def test_series_has_14_days_with_zero_fill(self):
        self.inst("a", NOW); s = S.installs_by_day(self.db)
        self.assertEqual(len(s), 14); self.assertEqual(s[-1]["n"], 1); self.assertEqual(s[0]["n"], 0)


class Auth(unittest.TestCase):
    def test_session_cookie_valid_expired_and_tampered(self):
        c = S.make_session(1000)
        self.assertTrue(S.check_session(c, 1000 + 60))
        self.assertFalse(S.check_session(c, 1000 + 31 * 86400))                       # expired
        exp, sig = c.split("."); self.assertFalse(S.check_session(f"{int(exp)+999999}.{sig}", 1000))   # exp edited
        self.assertFalse(S.check_session(exp + "." + "0" * 64, 1000)); self.assertFalse(S.check_session("junk", 1000))
    def test_password_check(self):
        old = S.PASSWORD; S.PASSWORD = "s3cret-pass"
        try: self.assertTrue(S.password_ok("s3cret-pass")); self.assertFalse(S.password_ok("s3cret-pas")); self.assertFalse(S.password_ok(""))
        finally: S.PASSWORD = old

class Users(T):
    def test_first_login_creates_unique_user_automatically(self):
        uid, created, counted = S.create_session(self.db, "e1", tok("alice"))
        self.assertTrue(created and counted and re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", uid))
        uid2, created2, counted2 = S.create_session(self.db, "e2", tok("alice"))   # same device later
        self.assertEqual(uid2, uid); self.assertFalse(created2); self.assertTrue(counted2)

    def test_ids_are_unique_across_many_users(self):
        ids = {S.create_session(self.db, f"e{i}", f"{i:040d}")[0] for i in range(5000)}
        self.assertEqual(len(ids), 5000)
        self.assertEqual(S.registered_users(self.db), 5000)

    def test_duplicate_id_is_impossible_even_on_collision(self):
        seq = iter(["usr_same", "usr_same", "usr_other"]); real = S._new_user_id
        S._new_user_id = lambda: next(seq)
        try:
            a = S.create_session(self.db, "e1", tok("a"))[0]
            b = S.create_session(self.db, "e2", tok("b"))[0]      # first draw collides -> redrawn
        finally: S._new_user_id = real
        self.assertEqual((a, b), ("usr_same", "usr_other"))
        self.assertEqual(self.db.execute("SELECT COUNT(DISTINCT user_id) FROM users").fetchone()[0], 2)

    def test_retry_of_lost_response_does_not_make_a_second_user(self):
        a = S.create_session(self.db, "e1", tok("p"))
        b = S.create_session(self.db, "e1", tok("p"))             # same token + same event (client retried)
        self.assertEqual(a[0], b[0]); self.assertFalse(b[2])      # login not double counted
        self.assertEqual(S.registered_users(self.db), 1)

    def test_login_must_belong_to_a_real_user(self):
        with self.assertRaises(S.AuthError): S.record_login(self.db, "x", "usr_ghost", NOW)
        with self.assertRaises(S.AuthError): S.create_session(self.db, "x", "short")
        uid = S.create_session(self.db, "e1", tok("owner"))[0]
        with self.assertRaises(S.AuthError): S.create_session(self.db, "e2", tok("thief"), user_id=uid)
        with self.assertRaises(S.AuthError): S.create_session(self.db, "e3", tok("nobody"), user_id="usr_made_up")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM login_events").fetchone()[0], 1)

    def test_yesterdays_single_login_is_logged_in_but_not_active(self):
        self.login("y", "visitor", NOW - D)                        # one login yesterday
        self.assertEqual(S.logged_in_7d(self.db), 1)               # counted as having logged in...
        self.assertEqual(S.active_users(self.db), 0)               # ...but not "active" (needs 3+)
        self.login("y2", "visitor", NOW - 3600); self.login("y3", "visitor", NOW - 60)
        self.assertEqual(S.active_users(self.db), 1)

    def test_active_threshold_is_configurable(self):
        self.login("a", "u", NOW - 60); S.ACTIVE_MIN_LOGINS = 1
        try: self.assertEqual(S.active_users(self.db), 1)
        finally: S.ACTIVE_MIN_LOGINS = 3

    def test_migration_from_v1_schema_keeps_logins(self):
        import sqlite3
        path = self.f.name + ".v1"; db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE login_events(event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,logged_in_at INTEGER NOT NULL,received_at INTEGER NOT NULL);
          CREATE INDEX idx_login_at_user ON login_events(logged_in_at,user_id);""")
        for i in range(3): db.execute("INSERT INTO login_events VALUES(?,?,?,?)", (f"o{i}", "old-user", NOW - i * 60, NOW))
        db.commit(); db.close()
        d = S.connect(path); S.init_db(d)
        self.assertEqual(S.active_users(d), 1)                     # same answer as before the migration
        self.assertEqual(d.execute("SELECT legacy FROM users WHERE user_id='old-user'").fetchone()[0], 1)
        self.assertTrue(d.execute("PRAGMA foreign_key_list(login_events)").fetchall())
        S.init_db(d)                                               # idempotent
        d.close(); os.unlink(path)

if __name__ == "__main__": unittest.main()
