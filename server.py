"""SQUEEK HOURS. Stdlib server: accounts, focus sessions, live rooms, chain proxy."""

import hashlib
import json
import mimetypes
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "public"
DB_PATH = ROOT / "data" / "app.db"
API = "https://api.squeekchain.com"
CHAIN_ID = "11155111"
PORT = 4183

STAGES = [
    (0, "The First Burrow"),
    (800, "The Growing Village"),
    (2000, "The Hamster City"),
    (5000, "The Crystal Kingdom"),
    (10000, "The Infinite World"),
]
MISSION = {
    "id": "001",
    "name": "Build the First City",
    "sessions": 1000,
    "people": 500,
    "minutes": 10000,
    "days": 7,
}
PUBLIC_ROOMS = [
    ("global-focus", "Global Focus"),
    ("deep-work", "Deep Work"),
    ("coding-room", "Coding Room"),
    ("crypto-builders", "Crypto Builders"),
    ("creative-studio", "Creative Studio"),
    ("silent-study", "Silent Study"),
]
THEMES = [("burrow", 0), ("lamp", 100), ("studio", 300), ("crystal", 800)]

lock = threading.Lock()
chain_lock = threading.Lock()
chain_cache = {"at": 0, "etag": None, "body": None, "error": None, "retry_at": 0}


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              salt TEXT NOT NULL,
              theme TEXT NOT NULL DEFAULT 'burrow',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
              id INTEGER PRIMARY KEY,
              slug TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              invite TEXT,
              goal TEXT NOT NULL DEFAULT '',
              owner_id INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presence (
              user_id INTEGER PRIMARY KEY,
              room_id INTEGER NOT NULL,
              focus TEXT NOT NULL DEFAULT '',
              seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY,
              room_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS focus_sessions (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              room_id INTEGER,
              kind TEXT NOT NULL,
              planned_seconds INTEGER NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        created = iso(now())
        for slug, name in PUBLIC_ROOMS:
            connection.execute(
                "INSERT OR IGNORE INTO rooms (slug, name, kind, goal, created_at) VALUES (?, ?, 'public', '', ?)",
                (slug, name, created),
            )


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return salt, digest


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def user_from_request(connection, headers):
    cookie = headers.get("Cookie", "")
    token = ""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "sqh_session":
            token = value
    if not token:
        return None
    row = connection.execute(
        """
        SELECT users.* FROM auth_sessions
        JOIN users ON users.id = auth_sessions.user_id
        WHERE auth_sessions.token_hash = ?
        """,
        (token_hash(token),),
    ).fetchone()
    return row


def xp_for(kind, seconds):
    if kind == "25" and seconds >= 25 * 60:
        return 25
    if kind == "50" and seconds >= 50 * 60:
        return 50
    if kind == "custom" and seconds >= 5 * 60:
        return max(5, min(120, seconds // 60))
    return 0


def completed_rows(connection, user_id=None):
    query = "SELECT * FROM focus_sessions WHERE status = 'completed'"
    args = []
    if user_id is not None:
        query += " AND user_id = ?"
        args.append(user_id)
    return connection.execute(query, args).fetchall()


def stats_for(rows):
    minutes = 0
    days = []
    for row in rows:
        minutes += row["planned_seconds"] // 60
        ended = parse_time(row["ended_at"])
        if ended:
            days.append(ended.date())
    unique_days = sorted(set(days))
    streak = best = 0
    previous = None
    for day in unique_days:
        if previous and day == previous + timedelta(days=1):
            streak += 1
        else:
            streak = 1
        best = max(best, streak)
        previous = day
    today = now().date()
    if not unique_days or unique_days[-1] < today - timedelta(days=1):
        streak = 0
    total_xp = 0
    for row in rows:
        total_xp += xp_for(row["kind"], row["planned_seconds"])
    level = "Bronze"
    for name, floor in (("Silver", 100), ("Gold", 300), ("Diamond", 800), ("Legendary", 2000)):
        if total_xp >= floor:
            level = name
    return {
        "minutes": minutes,
        "sessions": len(rows),
        "streak": streak,
        "best": best,
        "xp": total_xp,
        "level": level,
        "days": len(unique_days),
    }


def achievements(connection, user):
    rows = completed_rows(connection, user["id"])
    info = stats_for(rows)
    early = night = False
    for row in rows:
        started = parse_time(row["started_at"])
        if not started:
            continue
        early = early or started.hour < 8
        night = night or started.hour >= 22
    messages = connection.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE user_id = ?", (user["id"],)
    ).fetchone()["n"]
    in_room = any(row["room_id"] for row in rows)
    earned = {
        "Early Bird": early,
        "Night Owl": night,
        "100 Sessions": info["sessions"] >= 100,
        "Focus Master": info["minutes"] >= 1000,
        "Community Builder": messages > 0 and in_room,
        "Expedition Pioneer": info["sessions"] > 0,
    }
    return [{"name": name, "earned": on} for name, on in earned.items()], info


def settle_active(connection, user_id):
    row = connection.execute(
        "SELECT * FROM focus_sessions WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    started = parse_time(row["started_at"])
    end = started + timedelta(seconds=row["planned_seconds"])
    if now() >= end:
        connection.execute(
            "UPDATE focus_sessions SET status = 'completed', ended_at = ? WHERE id = ?",
            (iso(end), row["id"]),
        )
        return None
    remaining = int((end - now()).total_seconds())
    return {
        "id": row["id"],
        "kind": row["kind"],
        "plannedSeconds": row["planned_seconds"],
        "remaining": max(0, remaining),
        "roomId": row["room_id"],
    }


def community(connection):
    rows = connection.execute(
        "SELECT user_id, planned_seconds, ended_at FROM focus_sessions WHERE status = 'completed'"
    ).fetchall()
    today = now().date()
    today_minutes = 0
    people = set()
    days = set()
    for row in rows:
        ended = parse_time(row["ended_at"])
        if not ended:
            continue
        people.add(row["user_id"])
        days.add(ended.date())
        if ended.date() == today:
            today_minutes += row["planned_seconds"] // 60
    active = connection.execute(
        "SELECT COUNT(*) AS n FROM presence WHERE seen_at >= ?",
        (iso(now() - timedelta(seconds=20)),),
    ).fetchone()["n"]
    consecutive = 0
    cursor = today
    while cursor in days or (cursor == today and today_minutes > 0):
        if cursor not in days and not (cursor == today and today_minutes > 0):
            break
        consecutive += 1
        cursor -= timedelta(days=1)
    return {
        "sessions": len(rows),
        "people": len(people),
        "minutes": sum(row["planned_seconds"] // 60 for row in rows),
        "todayMinutes": today_minutes,
        "activeUsers": active,
        "consecutiveDays": consecutive,
    }


def world_from(chain):
    if not chain or chain.get("visual") == "UNAVAILABLE" and chain.get("round") is None:
        return {"stage": None, "name": None, "nextRound": None, "reason": "chain unread"}
    round_no = int(chain["round"])
    current = STAGES[0][1]
    next_round = STAGES[1][0]
    index = 1
    for number, (floor, name) in enumerate(STAGES, start=1):
        if round_no >= floor:
            current = name
            index = number
            nxt = STAGES[number][0] if number < len(STAGES) else None
            next_round = nxt
    return {
        "stage": index,
        "name": current,
        "settledRounds": round_no,
        "nextRound": next_round,
        "provisionalGlow": bool(chain.get("blockProgress")),
    }


def fetch_chain():
    with chain_lock:
        moment = time.time()
        if chain_cache["retry_at"] > moment and chain_cache["body"]:
            return chain_cache["body"], chain_cache["error"]
        if chain_cache["body"] and moment - chain_cache["at"] < 8:
            return chain_cache["body"], None
        request = urllib.request.Request(API + "/v1/status", headers={"Accept": "application/json"})
        if chain_cache["etag"]:
            request.add_header("If-None-Match", chain_cache["etag"])
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status == 304 and chain_cache["body"]:
                    chain_cache["at"] = moment
                    return chain_cache["body"], None
                payload = json.loads(response.read().decode())
                chain_cache["etag"] = response.headers.get("ETag")
                interpreted = interpret(payload)
                chain_cache.update(at=moment, body=interpreted, error=None, retry_at=0)
                return interpreted, None
        except urllib.error.HTTPError as error:
            wait = int(error.headers.get("Retry-After") or "8")
            chain_cache["retry_at"] = moment + max(1, wait)
            chain_cache["error"] = f"HTTP {error.code}"
            if chain_cache["body"]:
                return chain_cache["body"], chain_cache["error"]
            return interpret(None), chain_cache["error"]
        except Exception:
            chain_cache["error"] = "unread"
            if chain_cache["body"]:
                return chain_cache["body"], "unread"
            return interpret(None), "unread"


def interpret(payload):
    empty = {
        "visual": "UNAVAILABLE",
        "settlement": "UNAVAILABLE",
        "round": None,
        "work": None,
        "blockHeight": None,
        "blockProgress": None,
        "rpm": None,
        "pendingRound": None,
        "chainId": None,
        "installationId": None,
        "reason": "Squeekchain status is unread. Missing data is not zero activity.",
    }
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        return empty
    chain = str((payload.get("builder") or {}).get("status") or "")
    measurement = payload.get("measurement") or {}
    anchor = (payload.get("builder") or {}).get("anchor") or {}
    if not anchor.get("settledThroughRound"):
        empty["reason"] = "Confirmed settlement is missing."
        return empty
    observed = parse_time(measurement.get("observedAt"))
    limit = float(measurement.get("staleAfterSeconds") or 120)
    fresh = measurement.get("freshness") == "online" and observed and (now() - observed).total_seconds() < limit
    status = measurement.get("coreStatus")
    if not fresh or not status:
        visual = "UNAVAILABLE"
    elif status == "ACTIVE":
        visual = "LIVE"
    elif status == "IDLE":
        visual = "RESTING"
    else:
        visual = "UNAVAILABLE"
    submission = (payload.get("settlementQueue") or {}).get("latestSubmission") or {}
    confirmed = submission.get("lifecycle") == "confirmed" and str(submission.get("settledThroughRound")) == str(anchor.get("settledThroughRound"))
    pending = submission.get("settledThroughRound") if submission and not confirmed else None
    settlement = "SETTLING" if pending else "CONFIRMED"
    return {
        "visual": visual,
        "settlement": settlement,
        "round": str(anchor.get("settledThroughRound")),
        "work": str(anchor.get("cumulativeWork")),
        "blockHeight": str(anchor.get("blockHeight")),
        "blockProgress": str(anchor.get("blockProgress") or "0"),
        "rpm": (float(measurement.get("rpmMilli") or 0) / 1000) if fresh else None,
        "pendingRound": str(pending) if pending else None,
        "chainId": CHAIN_ID,
        "installationId": payload.get("installationId"),
        "builder": chain,
        "reason": None if visual != "UNAVAILABLE" else "Fresh activity is not available. The hamster is not shown as resting.",
    }


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, code, payload, cookie=None, clear=False):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", f"sqh_session={cookie}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000")
        if clear:
            self.send_header("Set-Cookie", "sqh_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8000:
            return None
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self.api_get(path)
            return
        file_path = STATIC / "index.html" if path in ("/", "") else STATIC / path.lstrip("/")
        if not str(file_path.resolve()).startswith(str(STATIC.resolve())) or not file_path.is_file():
            file_path = STATIC / "index.html"
        data = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self.read_json()
        if payload is None:
            self.send_json(400, {"error": "Bad JSON"})
            return
        with lock:
            connection = db()
            try:
                self.api_post(connection, path, payload)
                connection.commit()
            finally:
                connection.close()

    def api_get(self, path):
        if path == "/api/chain":
            body, error = fetch_chain()
            self.send_json(200, {"chain": body, "warning": error, "world": world_from(body)})
            return
        with lock:
            connection = db()
            try:
                user = user_from_request(connection, self.headers)
                if path == "/api/me":
                    self.me(connection, user)
                elif path == "/api/rooms":
                    self.rooms(connection)
                elif path.startswith("/api/rooms/"):
                    self.room_detail(connection, path.split("/")[-1])
                elif path == "/api/leaderboard":
                    self.board(connection)
                elif path == "/api/expedition":
                    self.expedition(connection)
                else:
                    self.send_json(404, {"error": "Not found"})
            finally:
                connection.close()

    def me(self, connection, user):
        if not user:
            self.send_json(200, {"user": None})
            return
        active = settle_active(connection, user["id"])
        badges, info = achievements(connection, user)
        self.send_json(200, {
            "user": {"id": user["id"], "username": user["username"], "theme": user["theme"]},
            "stats": info,
            "achievements": badges,
            "active": active,
            "themes": [{"id": name, "xp": need, "open": info["xp"] >= need} for name, need in THEMES],
        })

    def rooms(self, connection):
        cutoff = iso(now() - timedelta(seconds=20))
        rows = connection.execute(
            """
            SELECT rooms.*, (SELECT COUNT(*) FROM presence WHERE room_id = rooms.id AND seen_at >= ?) AS live
            FROM rooms WHERE kind = 'public' ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
        self.send_json(200, {"rooms": [dict(row) for row in rows]})

    def room_detail(self, connection, slug):
        room = connection.execute("SELECT * FROM rooms WHERE slug = ?", (slug,)).fetchone()
        if not room:
            self.send_json(404, {"error": "No such room"})
            return
        cutoff = iso(now() - timedelta(seconds=20))
        people = connection.execute(
            """
            SELECT users.username, presence.focus, presence.seen_at
            FROM presence JOIN users ON users.id = presence.user_id
            WHERE presence.room_id = ? AND presence.seen_at >= ?
            ORDER BY users.username
            """,
            (room["id"], cutoff),
        ).fetchall()
        notes = connection.execute(
            """
            SELECT messages.body, messages.created_at, users.username
            FROM messages JOIN users ON users.id = messages.user_id
            WHERE messages.room_id = ? ORDER BY messages.id DESC LIMIT 30
            """,
            (room["id"],),
        ).fetchall()
        self.send_json(200, {"room": dict(room), "people": [dict(row) for row in people], "messages": [dict(row) for row in reversed(notes)]})

    def board(self, connection):
        users = connection.execute("SELECT id, username, theme FROM users").fetchall()
        ranked = []
        for user in users:
            info = stats_for(completed_rows(connection, user["id"]))
            if info["sessions"] == 0:
                continue
            ranked.append({"username": user["username"], "theme": user["theme"], **info})
        ranked.sort(key=lambda item: (item["xp"], item["minutes"]), reverse=True)
        self.send_json(200, {"leaders": ranked[:50]})

    def expedition(self, connection):
        facts = community(connection)
        chain, _error = fetch_chain()
        self.send_json(200, {
            "mission": MISSION,
            "community": facts,
            "hamster": world_from(chain),
        })

    def api_post(self, connection, path, payload):
        user = user_from_request(connection, self.headers)
        if path == "/api/register":
            self.register(connection, payload)
        elif path == "/api/login":
            self.login(connection, payload)
        elif path == "/api/logout":
            self.logout(connection)
        elif path == "/api/sessions":
            self.start_session(connection, user, payload)
        elif path.endswith("/complete") and path.startswith("/api/sessions/"):
            self.finish_session(connection, user, path.split("/")[3])
        elif path.endswith("/cancel") and path.startswith("/api/sessions/"):
            self.cancel_session(connection, user, path.split("/")[3])
        elif path == "/api/theme":
            self.set_theme(connection, user, payload)
        elif path.endswith("/join") and path.startswith("/api/rooms/"):
            self.join_room(connection, user, path.split("/")[3], payload)
        elif path.endswith("/messages") and path.startswith("/api/rooms/"):
            self.post_message(connection, user, path.split("/")[3], payload)
        elif path == "/api/rooms":
            self.create_room(connection, user, payload)
        else:
            self.send_json(404, {"error": "Not found"})

    def register(self, connection, payload):
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not username.replace("_", "").isalnum() or not 3 <= len(username) <= 20:
            self.send_json(400, {"error": "Username must be 3–20 letters, numbers, or underscores."})
            return
        if len(password) < 8:
            self.send_json(400, {"error": "Password must be at least 8 characters."})
            return
        if connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            self.send_json(409, {"error": "That username is taken."})
            return
        salt, digest = hash_password(password)
        connection.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, digest, salt, iso(now())),
        )
        self.issue(connection, username)

    def login(self, connection, payload):
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            self.send_json(401, {"error": "Wrong username or password."})
            return
        _salt, digest = hash_password(password, row["salt"])
        if digest != row["password_hash"]:
            self.send_json(401, {"error": "Wrong username or password."})
            return
        self.issue(connection, username)

    def issue(self, connection, username):
        user = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        token = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
            (token_hash(token), user["id"], iso(now())),
        )
        self.send_json(200, {"ok": True, "username": username}, cookie=token)

    def logout(self, connection):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sqh_session" and value:
                connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(value),))
        self.send_json(200, {"ok": True}, clear=True)

    def start_session(self, connection, user, payload):
        if not user:
            self.send_json(401, {"error": "Sign in to start a session."})
            return
        if settle_active(connection, user["id"]):
            self.send_json(409, {"error": "A session is already running."})
            return
        kind = str(payload.get("kind") or "")
        if kind == "25":
            seconds = 25 * 60
        elif kind == "50":
            seconds = 50 * 60
        elif kind == "custom":
            minutes = int(payload.get("minutes") or 0)
            if minutes < 1 or minutes > 120:
                self.send_json(400, {"error": "Custom sessions are 1 to 120 minutes."})
                return
            seconds = minutes * 60
        else:
            self.send_json(400, {"error": "Unknown session."})
            return
        room_id = None
        slug = payload.get("room")
        if slug:
            room = connection.execute("SELECT id FROM rooms WHERE slug = ?", (str(slug),)).fetchone()
            room_id = room["id"] if room else None
        stamp = iso(now())
        cursor = connection.execute(
            """
            INSERT INTO focus_sessions (user_id, room_id, kind, planned_seconds, started_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (user["id"], room_id, kind, seconds, stamp, stamp),
        )
        self.send_json(200, {"id": cursor.lastrowid, "remaining": seconds})

    def finish_session(self, connection, user, session_id):
        if not user:
            self.send_json(401, {"error": "Sign in."})
            return
        row = connection.execute(
            "SELECT * FROM focus_sessions WHERE id = ? AND user_id = ? AND status = 'active'",
            (session_id, user["id"]),
        ).fetchone()
        if not row:
            self.send_json(404, {"error": "No active session."})
            return
        started = parse_time(row["started_at"])
        elapsed = (now() - started).total_seconds()
        if elapsed + 2 < row["planned_seconds"]:
            self.send_json(400, {"error": "This session has not finished yet."})
            return
        ended = iso(started + timedelta(seconds=row["planned_seconds"]))
        connection.execute(
            "UPDATE focus_sessions SET status = 'completed', ended_at = ? WHERE id = ?",
            (ended, row["id"]),
        )
        self.send_json(200, {"ok": True, "xp": xp_for(row["kind"], row["planned_seconds"]), "minutes": row["planned_seconds"] // 60})

    def cancel_session(self, connection, user, session_id):
        if not user:
            self.send_json(401, {"error": "Sign in."})
            return
        connection.execute(
            "UPDATE focus_sessions SET status = 'cancelled', ended_at = ? WHERE id = ? AND user_id = ? AND status = 'active'",
            (iso(now()), session_id, user["id"]),
        )
        self.send_json(200, {"ok": True})

    def set_theme(self, connection, user, payload):
        if not user:
            self.send_json(401, {"error": "Sign in."})
            return
        theme = str(payload.get("theme") or "")
        _badges, info = achievements(connection, user)
        allowed = {name for name, need in THEMES if info["xp"] >= need}
        if theme not in allowed:
            self.send_json(400, {"error": "That theme is still locked."})
            return
        connection.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user["id"]))
        self.send_json(200, {"ok": True})

    def join_room(self, connection, user, slug, payload):
        if not user:
            self.send_json(401, {"error": "Sign in to join a room."})
            return
        room = connection.execute("SELECT * FROM rooms WHERE slug = ?", (slug,)).fetchone()
        if not room:
            self.send_json(404, {"error": "No such room"})
            return
        if room["kind"] == "private" and payload.get("invite") != room["invite"]:
            self.send_json(403, {"error": "Invite code required."})
            return
        focus = str(payload.get("focus") or "")[:80]
        connection.execute(
            """
            INSERT INTO presence (user_id, room_id, focus, seen_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET room_id = excluded.room_id, focus = excluded.focus, seen_at = excluded.seen_at
            """,
            (user["id"], room["id"], focus, iso(now())),
        )
        self.send_json(200, {"ok": True})

    def post_message(self, connection, user, slug, payload):
        if not user:
            self.send_json(401, {"error": "Sign in to talk."})
            return
        room = connection.execute("SELECT id FROM rooms WHERE slug = ?", (slug,)).fetchone()
        if not room:
            self.send_json(404, {"error": "No such room"})
            return
        body = " ".join(str(payload.get("body") or "").split())
        if not body or len(body) > 140:
            self.send_json(400, {"error": "Messages are 1–140 characters."})
            return
        present = connection.execute(
            "SELECT 1 FROM presence WHERE user_id = ? AND room_id = ?",
            (user["id"], room["id"]),
        ).fetchone()
        if not present:
            self.send_json(403, {"error": "Join the room first."})
            return
        connection.execute(
            "INSERT INTO messages (room_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (room["id"], user["id"], body, iso(now())),
        )
        self.send_json(200, {"ok": True})

    def create_room(self, connection, user, payload):
        if not user:
            self.send_json(401, {"error": "Sign in to create a room."})
            return
        name = " ".join(str(payload.get("name") or "").split())
        if not 3 <= len(name) <= 40:
            self.send_json(400, {"error": "Room name must be 3–40 characters."})
            return
        slug = "p-" + secrets.token_hex(4)
        invite = secrets.token_hex(3)
        goal = str(payload.get("goal") or "")[:80]
        connection.execute(
            """
            INSERT INTO rooms (slug, name, kind, invite, goal, owner_id, created_at)
            VALUES (?, ?, 'private', ?, ?, ?, ?)
            """,
            (slug, name, invite, goal, user["id"], iso(now())),
        )
        self.send_json(200, {"slug": slug, "invite": invite})


def main():
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), App)
    print(f"SQUEEK HOURS on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
