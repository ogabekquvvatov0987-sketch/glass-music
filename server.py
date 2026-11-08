#!/usr/bin/env python3
"""
Glass Music server — YouTube search + SQLite (users, history, likes)
Run:  YOUTUBE_API_KEY=xxx python3 server.py
"""
import json
import os
import re
import html
import sqlite3
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glass_music.db")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        surname TEXT,
        photo TEXT,
        google_id TEXT,
        created_at REAL,
        updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        query TEXT NOT NULL,
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        video_id TEXT NOT NULL,
        title TEXT,
        channel TEXT,
        thumbnail TEXT,
        created_at REAL,
        UNIQUE(user_email, video_id)
    );
    CREATE INDEX IF NOT EXISTS idx_history_email ON history(user_email);
    CREATE INDEX IF NOT EXISTS idx_likes_email ON likes(user_email);
    """)
    conn.commit()
    conn.close()


def clean_title(text):
    if not text:
        return "Nomsiz video"
    text = html.unescape(text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip() or "Nomsiz video"


class GlassMusicHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/search":
            self.handle_search(
                qs.get("q", [""])[0],
                qs.get("pageToken", [""])[0],
                qs.get("type", ["video"])[0],
                qs.get("max", ["30"])[0],
            )
            return
        if path == "/api/user":
            self.handle_get_user(qs.get("email", [""])[0])
            return
        if path == "/api/history":
            self.handle_get_history(qs.get("email", [""])[0])
            return
        if path == "/api/likes":
            self.handle_get_likes(qs.get("email", [""])[0])
            return

        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}

        if parsed.path == "/api/user":
            self.handle_save_user(data)
            return
        if parsed.path == "/api/history":
            self.handle_add_history(data)
            return
        if parsed.path == "/api/like":
            self.handle_toggle_like(data)
            return

        self.send_json(404, {"error": "Not found"})

    def handle_search(self, query, page_token="", search_type="video", max_str="30"):
        query = query.strip()
        if not query:
            self.send_json(400, {"error": "Qidiruv matni bo‘sh."})
            return
        if not API_KEY:
            self.send_json(500, {
                "error": "YouTube API kaliti topilmadi. YOUTUBE_API_KEY ni o‘rnating."
            })
            return
        try:
            max_results = max(1, min(50, int(max_str)))
        except ValueError:
            max_results = 30

        extra = ""
        if search_type == "shorts":
            if "shorts" not in query.lower():
                query = query + " #shorts"
            extra = "&videoDuration=short"

        url = (
            "https://www.googleapis.com/youtube/v3/search"
            "?part=snippet&type=video&videoEmbeddable=true"
            f"&maxResults={max_results}"
            "&q=" + quote(query)
            + "&key=" + quote(API_KEY)
            + extra
        )
        if page_token:
            url += "&pageToken=" + quote(page_token)

        request = Request(url, headers={"User-Agent": "GlassMusic/1.3"})
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
                message = details.get("error", {}).get("message", "YouTube API xatosi")
            except Exception:
                message = "YouTube API HTTP xatosi: " + str(exc.code)
            code = exc.code if 400 <= exc.code < 600 else 502
            self.send_json(code, {"error": message})
            return
        except (URLError, TimeoutError) as exc:
            self.send_json(502, {"error": "YouTube API bilan bog‘lanib bo‘lmadi: " + str(exc)})
            return
        except Exception as exc:
            self.send_json(500, {"error": "Server xatosi: " + str(exc)})
            return

        videos = []
        for item in payload.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id:
                continue
            thumbs = snippet.get("thumbnails", {})
            thumb = (
                thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url")
                or thumbs.get("default", {}).get("url")
                or ""
            )
            videos.append({
                "id": video_id,
                "title": clean_title(snippet.get("title", "")),
                "channel": clean_title(snippet.get("channelTitle", "YouTube")),
                "thumbnail": thumb,
            })

        self.send_json(200, {
            "videos": videos,
            "nextPageToken": payload.get("nextPageToken") or "",
            "total": len(videos),
        })

    def handle_save_user(self, data):
        email = (data.get("email") or "").strip().lower()
        if not email:
            self.send_json(400, {"error": "Email kerak"})
            return
        now = time.time()
        conn = db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE email=?", (email,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE users SET name=?, surname=?, photo=?, google_id=?, updated_at=? WHERE email=?",
                (data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                 data.get("google_id") or "", now, email),
            )
        else:
            c.execute(
                "INSERT INTO users (email, name, surname, photo, google_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (email, data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                 data.get("google_id") or "", now, now),
            )
        conn.commit()
        conn.close()
        self.send_json(200, {"ok": True, "email": email})

    def handle_get_user(self, email):
        email = (email or "").strip().lower()
        if not email:
            self.send_json(400, {"error": "Email kerak"})
            return
        conn = db()
        c = conn.cursor()
        c.execute("SELECT email, name, surname, photo, google_id FROM users WHERE email=?", (email,))
        row = c.fetchone()
        conn.close()
        if not row:
            self.send_json(404, {"error": "User not found"})
            return
        self.send_json(200, dict(row))

    def handle_add_history(self, data):
        email = (data.get("email") or "").strip().lower()
        query = (data.get("query") or "").strip()
        if not email or not query:
            self.send_json(400, {"error": "email va query kerak"})
            return
        conn = db()
        c = conn.cursor()
        c.execute("INSERT INTO history (user_email, query, created_at) VALUES (?,?,?)", (email, query, time.time()))
        c.execute(
            """DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 50
            ) AND user_email=?""",
            (email, email),
        )
        conn.commit()
        conn.close()
        self.send_json(200, {"ok": True})

    def handle_get_history(self, email):
        email = (email or "").strip().lower()
        if not email:
            self.send_json(400, {"error": "Email kerak"})
            return
        conn = db()
        c = conn.cursor()
        c.execute(
            "SELECT query FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 40",
            (email,),
        )
        rows = [r["query"] for r in c.fetchall()]
        conn.close()
        seen = set()
        unique = []
        for q in rows:
            if q.lower() not in seen:
                seen.add(q.lower())
                unique.append(q)
        self.send_json(200, {"history": unique})

    def handle_toggle_like(self, data):
        email = (data.get("email") or "").strip().lower()
        video_id = (data.get("video_id") or "").strip()
        if not email or not video_id:
            self.send_json(400, {"error": "email va video_id kerak"})
            return
        conn = db()
        c = conn.cursor()
        c.execute("SELECT id FROM likes WHERE user_email=? AND video_id=?", (email, video_id))
        row = c.fetchone()
        if row:
            c.execute("DELETE FROM likes WHERE user_email=? AND video_id=?", (email, video_id))
            liked = False
        else:
            c.execute(
                "INSERT INTO likes (user_email, video_id, title, channel, thumbnail, created_at) VALUES (?,?,?,?,?,?)",
                (email, video_id, data.get("title") or "", data.get("channel") or "",
                 data.get("thumbnail") or "", time.time()),
            )
            liked = True
        conn.commit()
        conn.close()
        self.send_json(200, {"liked": liked})

    def handle_get_likes(self, email):
        email = (email or "").strip().lower()
        if not email:
            self.send_json(400, {"error": "Email kerak"})
            return
        conn = db()
        c = conn.cursor()
        c.execute(
            "SELECT video_id as id, title, channel, thumbnail FROM likes WHERE user_email=? ORDER BY created_at DESC",
            (email,),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        self.send_json(200, {"likes": rows})

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print("[%s] %s" % (self.log_date_time_string(), format % args))


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), GlassMusicHandler)
    print(f"Glass Music: http://127.0.0.1:{PORT}")
    print("DB:", DB_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nToxtatildi.")
    finally:
        server.server_close()
