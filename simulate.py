#!/usr/bin/env python3
"""DEMO DATA GENERATOR - sends SIMULATED events to a running server so you can see the dashboard move.
Never run this against a production database.
  python3 simulate.py            # seed 14 days of history, then stream live events
  python3 simulate.py --seed-only
"""
import json, random, sys, time, uuid, urllib.request
BASE = "http://localhost:8080"
KEY = ""   # set to your INGEST_KEY if the server uses one

def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json", **({"X-API-Key": KEY} if KEY else {})})
    return json.load(urllib.request.urlopen(req, timeout=10))

def install(ts=None): return post("/api/events/install", {"event_id": str(uuid.uuid4()), "device_id": "dev-" + uuid.uuid4().hex[:8], "platform": random.choice(["android", "ios"]), **({"ts": ts} if ts else {})})
def login(user, ts=None):
    """user = a simulated device name; its secret token is derived from it. The server assigns the real user ID."""
    return post("/api/session", {"event_id": str(uuid.uuid4()), "token": ("sim-" + user + "-" * 40)[:40] + "x" * 8, **({"ts": ts} if ts else {})})

def seed():
    now = int(time.time()); users = [f"user{i}" for i in range(60)]
    for d in range(13, 0, -1):
        for _ in range(random.randint(8, 30)): install(now - d * 86400 + random.randint(0, 80000))
    for u in users:
        for _ in range(random.choice([0, 1, 2, 3, 4, 5, 7])): login(u, now - random.randint(0, 7 * 86400 - 60))
    print("seeded history")
    return users

if __name__ == "__main__":
    users = seed()
    if "--seed-only" in sys.argv: sys.exit()
    print("streaming live events (Ctrl+C to stop)")
    while True:
        time.sleep(random.uniform(0.5, 2.5))
        install() if random.random() < .5 else login(random.choice(users))
