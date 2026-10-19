/* Drop-in client. Include it in your app and call trackLogin() each time a user logs in / opens a session.
 *
 *   <script>window.ANALYTICS_URL = "https://your-server.example";</script>
 *   <script src="analytics-client.js"></script>
 *   ... trackLogin();
 *
 * What it guarantees:
 *  - The device creates a secret random token ONCE and keeps it. The server turns it into a permanent,
 *    unique user ID automatically (the first call registers; later calls find the same user).
 *  - Every login gets its own event_id and is queued locally until the server confirms it, so a login that
 *    happens offline, or whose reply gets lost, is retried and still counted exactly once.
 */
(function () {
  var BASE = (window.ANALYTICS_URL || "").replace(/\/$/, "");
  var TK = "an_token", UK = "an_user", QK = "an_queue", busy = false;
  function get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } }
  function rand(n) { var a = new Uint8Array(n); crypto.getRandomValues(a); return Array.from(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join(""); }
  function uuid() { return crypto.randomUUID ? crypto.randomUUID() : rand(16); }
  function token() { var t = get(TK); if (!t) { t = "t_" + rand(32); set(TK, t); } return t; }   // 256 random bits

  function toast(code) {                                  // small, subtle "Your code: X" popup; tap to copy
    try {
      var el = document.createElement("div");
      el.textContent = "Your code: " + code;
      el.setAttribute("role", "status");
      el.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99999;padding:7px 14px;" +
        "border-radius:999px;background:rgba(30,27,75,.88);color:#fff;font:500 13px/1.2 system-ui,sans-serif;" +
        "letter-spacing:.3px;box-shadow:0 4px 14px rgba(0,0,0,.25);opacity:0;transition:opacity .4s;cursor:pointer";
      el.onclick = function () { try { navigator.clipboard.writeText(code); el.textContent = "Copied"; } catch (e) {} };
      document.body.appendChild(el);
      setTimeout(function () { el.style.opacity = "1"; }, 30);
      setTimeout(function () { el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 500); }, 6000);
    } catch (e) {}
  }

  function flush() {
    if (busy || !BASE) return Promise.resolve();
    busy = true;
    var loop = function () {
      var q = get(QK) || [];
      if (!q.length) return;
      var ev = q[0];
      return fetch(BASE + "/api/session", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ event_id: ev.id, ts: ev.ts, token: token(), user_id: get(UK) || undefined })
      }).then(function (r) {
        if (r.status >= 400 && r.status < 500 && r.status !== 429) { q.shift(); set(QK, q); return loop(); }  // permanently rejected: drop, don't loop forever
        if (!r.ok) throw new Error("retry later");
        return r.json().then(function (j) { if (get(UK) !== j.user_id) toast(j.user_id); set(UK, j.user_id); q.shift(); set(QK, q); return loop(); });
      });
    };
    return Promise.resolve(loop()).catch(function () { /* offline or server down: stays queued */ }).then(function () { busy = false; });
  }

  window.trackLogin = function () {
    var q = get(QK) || [];
    q.push({ id: uuid(), ts: Math.floor(Date.now() / 1000) });
    set(QK, q.slice(-200));
    return flush();
  };
  window.analyticsUserId = function () { return get(UK); };
  addEventListener("online", flush);
  addEventListener("load", flush);                      // send anything left over from last time
})();
