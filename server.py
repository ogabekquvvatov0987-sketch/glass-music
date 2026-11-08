#!/usr/bin/env python3
"""Glass Music server — search + stats + SQLite"""
import json, os, re, html, sqlite3, time, random
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", "8080"))
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glass_music.db")

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    conn = db(); c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        name TEXT, surname TEXT, photo TEXT, google_id TEXT, created_at REAL, updated_at REAL);
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT NOT NULL, query TEXT NOT NULL, created_at REAL);
    CREATE TABLE IF NOT EXISTS likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT NOT NULL, video_id TEXT NOT NULL,
        title TEXT, channel TEXT, thumbnail TEXT, created_at REAL, UNIQUE(user_email, video_id));
    CREATE INDEX IF NOT EXISTS idx_h ON history(user_email);
    CREATE INDEX IF NOT EXISTS idx_l ON likes(user_email);
    """)
    conn.commit(); conn.close()

def clean_title(text):
    if not text: return "Nomsiz video"
    text = html.unescape(text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip() or "Nomsiz video"

def yt_get(url):
    req = Request(url, headers={"User-Agent": "GlassMusic/1.4"})
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))

def enrich_stats(videos):
    """Attach statistics + better channel thumb via videos.list"""
    if not videos or not API_KEY:
        return videos
    ids = [v["id"] for v in videos if v.get("id")]
    if not ids:
        return videos
    # batch max 50
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        "?part=statistics,snippet&id=" + quote(",".join(ids[:50]))
        + "&key=" + quote(API_KEY)
    )
    try:
        payload = yt_get(url)
    except Exception:
        return videos
    by_id = {}
    for item in payload.get("items", []):
        vid = item.get("id")
        st = item.get("statistics", {})
        sn = item.get("snippet", {})
        ch_id = sn.get("channelId", "")
        by_id[vid] = {
            "views": int(st.get("viewCount") or 0),
            "likes": int(st.get("likeCount") or 0),
            "comments": int(st.get("commentCount") or 0),
            "channelId": ch_id,
            "publishedAt": sn.get("publishedAt", ""),
        }
    for v in videos:
        extra = by_id.get(v["id"], {})
        v.update(extra)
    # channel thumbnails
    ch_ids = list({v.get("channelId") for v in videos if v.get("channelId")})
    if ch_ids and API_KEY:
        try:
            curl = (
                "https://www.googleapis.com/youtube/v3/channels"
                "?part=snippet&id=" + quote(",".join(ch_ids[:50]))
                + "&key=" + quote(API_KEY)
            )
            cp = yt_get(curl)
            ch_map = {}
            for it in cp.get("items", []):
                thumbs = it.get("snippet", {}).get("thumbnails", {})
                ch_map[it["id"]] = (
                    thumbs.get("default", {}).get("url")
                    or thumbs.get("medium", {}).get("url")
                    or ""
                )
            for v in videos:
                cid = v.get("channelId")
                if cid and cid in ch_map:
                    v["channelThumb"] = ch_map[cid]
        except Exception:
            pass
    return videos

class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path); qs = parse_qs(p.query); path = p.path
        if path == "/api/search":
            self.handle_search(qs.get("q",[""])[0], qs.get("pageToken",[""])[0],
                               qs.get("type",["video"])[0], qs.get("max",["24"])[0])
            return
        if path == "/api/user":
            self.handle_get_user(qs.get("email",[""])[0]); return
        if path == "/api/history":
            self.handle_get_history(qs.get("email",[""])[0]); return
        if path == "/api/likes":
            self.handle_get_likes(qs.get("email",[""])[0]); return
        if path == "/api/comments":
            self.handle_comments(qs.get("id",[""])[0]); return
        if path in ("/", ""):
            file_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "index.html"
            )

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    page = f.read()

                page = page.replace(
                    "YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com",
                    GOOGLE_CLIENT_ID
                )

                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            except Exception:
                self.send_error(500, "Sahifa yuklanmadi")
                return

        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try: data = json.loads(self.rfile.read(n).decode("utf-8") if n else "{}")
        except: data = {}
        if p.path == "/api/user": self.handle_save_user(data); return
        if p.path == "/api/history": self.handle_add_history(data); return
        if p.path == "/api/like": self.handle_toggle_like(data); return
        self.send_json(404, {"error": "Not found"})

    def handle_search(self, query, page_token="", search_type="video", max_str="24"):
        query = query.strip()
        if search_type == "shorts" and not query:
            # random shorts seeds — user doesn't see this
            seeds = [
                "shorts viral", "funny shorts", "music shorts", "dance shorts",
                "uzbekistan shorts", "trending shorts", "comedy shorts",
                "song shorts", "reels music", "tiktok music shorts"
            ]
            query = random.choice(seeds)
        if not query:
            self.send_json(400, {"error": "Qidiruv matni bo‘sh."}); return
        if not API_KEY:
            self.send_json(500, {"error": "YOUTUBE_API_KEY topilmadi."}); return
        try: max_results = max(1, min(50, int(max_str)))
        except: max_results = 24
        extra = ""
        if search_type == "shorts":
            if "shorts" not in query.lower():
                query = query + " #shorts"
            extra = "&videoDuration=short"
        url = (
            "https://www.googleapis.com/youtube/v3/search"
            "?part=snippet&type=video&videoEmbeddable=true"
            f"&maxResults={max_results}&q=" + quote(query)
            + "&key=" + quote(API_KEY) + extra
        )
        if page_token: url += "&pageToken=" + quote(page_token)
        try:
            payload = yt_get(url)
        except HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
                msg = details.get("error", {}).get("message", "YouTube API xatosi")
            except Exception:
                msg = "HTTP " + str(exc.code)
            self.send_json(exc.code if 400 <= exc.code < 600 else 502, {"error": msg}); return
        except Exception as exc:
            self.send_json(502, {"error": str(exc)}); return

        videos = []
        for item in payload.get("items", []):
            vid = item.get("id", {}).get("videoId")
            sn = item.get("snippet", {})
            if not vid: continue
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            videos.append({
                "id": vid,
                "title": clean_title(sn.get("title", "")),
                "channel": clean_title(sn.get("channelTitle", "YouTube")),
                "channelId": sn.get("channelId", ""),
                "thumbnail": thumb,
                "channelThumb": "",
                "views": 0, "likes": 0, "comments": 0,
            })
        videos = enrich_stats(videos)
        self.send_json(200, {
            "videos": videos,
            "nextPageToken": payload.get("nextPageToken") or "",
            "total": len(videos),
        })

    def handle_save_user(self, data):
        email = (data.get("email") or "").strip().lower()
        if not email: self.send_json(400, {"error": "Email kerak"}); return
        now = time.time(); conn = db(); c = conn.cursor()
        c.execute("SELECT id FROM users WHERE email=?", (email,))
        if c.fetchone():
            c.execute("UPDATE users SET name=?,surname=?,photo=?,google_id=?,updated_at=? WHERE email=?",
                      (data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                       data.get("google_id") or "", now, email))
        else:
            c.execute("INSERT INTO users (email,name,surname,photo,google_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                      (email, data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                       data.get("google_id") or "", now, now))
        conn.commit(); conn.close()
        self.send_json(200, {"ok": True})

    def handle_get_user(self, email):
        email = (email or "").strip().lower()
        if not email: self.send_json(400, {"error": "Email"}); return
        conn = db(); c = conn.cursor()
        c.execute("SELECT email,name,surname,photo,google_id FROM users WHERE email=?", (email,))
        row = c.fetchone(); conn.close()
        if not row: self.send_json(404, {"error": "not found"}); return
        self.send_json(200, dict(row))

    def handle_add_history(self, data):
        email = (data.get("email") or "").strip().lower()
        query = (data.get("query") or "").strip()
        if not email or not query: self.send_json(400, {"error": "need email+query"}); return
        conn = db(); c = conn.cursor()
        c.execute("INSERT INTO history (user_email,query,created_at) VALUES (?,?,?)", (email, query, time.time()))
        c.execute("""DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 50) AND user_email=?""", (email, email))
        conn.commit(); conn.close(); self.send_json(200, {"ok": True})

    def handle_get_history(self, email):
        email = (email or "").strip().lower()
        if not email: self.send_json(400, {"error": "Email"}); return
        conn = db(); c = conn.cursor()
        c.execute("SELECT query FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 40", (email,))
        rows = [r["query"] for r in c.fetchall()]; conn.close()
        seen, unique = set(), []
        for q in rows:
            if q.lower() not in seen: seen.add(q.lower()); unique.append(q)
        self.send_json(200, {"history": unique})

    def handle_toggle_like(self, data):
        email = (data.get("email") or "").strip().lower()
        vid = (data.get("video_id") or "").strip()
        if not email or not vid: self.send_json(400, {"error": "need email+video_id"}); return
        conn = db(); c = conn.cursor()
        c.execute("SELECT id FROM likes WHERE user_email=? AND video_id=?", (email, vid))
        if c.fetchone():
            c.execute("DELETE FROM likes WHERE user_email=? AND video_id=?", (email, vid)); liked = False
        else:
            c.execute("INSERT INTO likes (user_email,video_id,title,channel,thumbnail,created_at) VALUES (?,?,?,?,?,?)",
                      (email, vid, data.get("title") or "", data.get("channel") or "", data.get("thumbnail") or "", time.time()))
            liked = True
        conn.commit(); conn.close(); self.send_json(200, {"liked": liked})

    def handle_get_likes(self, email):
        email = (email or "").strip().lower()
        if not email: self.send_json(400, {"error": "Email"}); return
        conn = db(); c = conn.cursor()
        c.execute("SELECT video_id as id, title, channel, thumbnail FROM likes WHERE user_email=? ORDER BY created_at DESC", (email,))
        rows = [dict(r) for r in c.fetchall()]; conn.close()
        self.send_json(200, {"likes": rows})


    def handle_comments(self, video_id):
        video_id = (video_id or "").strip()
        if not video_id:
            self.send_json(400, {"error": "id kerak"}); return
        if not API_KEY:
            self.send_json(500, {"error": "API key"}); return
        url = (
            "https://www.googleapis.com/youtube/v3/commentThreads"
            "?part=snippet&order=relevance&maxResults=20&textFormat=plainText"
            "&videoId=" + quote(video_id) + "&key=" + quote(API_KEY)
        )
        try:
            payload = yt_get(url)
        except Exception as e:
            self.send_json(200, {"comments": [], "error": str(e)}); return
        comments = []
        for item in payload.get("items", []):
            sn = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            comments.append({
                "author": clean_title(sn.get("authorDisplayName", "User")),
                "text": clean_title(sn.get("textDisplay", "")),
                "likes": int(sn.get("likeCount") or 0),
                "photo": sn.get("authorProfileImageUrl", ""),
            })
        self.send_json(200, {"comments": comments})

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_db()
    s = ThreadingHTTPServer((HOST, PORT), H)
    print(f"Glass Music: http://127.0.0.1:{PORT}")
    try: s.serve_forever()
    except KeyboardInterrupt: print("\nstop")
    finally: s.server_close()
