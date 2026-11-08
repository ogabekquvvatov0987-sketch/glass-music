#!/usr/bin/env python3
"""Glass Music server — YouTube qidiruv (kesh + API kalitlarni almashtirish), tarix, sevimlilar (SQLite)"""
import json, os, re, html, sqlite3, time, random, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
# Bir nechta API kalit vergul, nuqta-vergul yoki bo‘sh joy bilan ajratilishi mumkin.
# Biri limitga urilsa, server avtomatik keyingisiga o‘tadi.
API_KEYS = [k for k in re.split(r"[\s,;]+", os.environ.get("YOUTUBE_API_KEY", "")) if k]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
DB_PATH = os.path.join(BASE_DIR, "glass_music.db")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

MAX_BODY = 4 * 1024 * 1024       # so‘rov hajmi chegarasi (rasm uchun)
SEARCH_TTL = 6 * 3600            # qidiruv natijasi 6 soat yangi hisoblanadi
COMMENTS_TTL = 3 * 3600          # izohlar 3 soat
STALE_MAX = 3 * 86400            # limit tugasa, shu muddatgacha eski kesh beriladi
KEY_COOLDOWN = 6 * 3600          # limitga urilgan kalit shuncha vaqt "dam oladi"

QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
KEY_REASONS = {"keyInvalid", "accessNotConfigured", "keyExpired", "ipRefererBlocked"}
QUOTA_MSG = ("Bugungi YouTube qidiruv limiti tugadi. Ertaga qayta urinib ko‘ring "
             "yoki serverga qo‘shimcha YOUTUBE_API_KEY kiriting.")

SHORT_SEEDS = [
    "shorts viral", "funny shorts", "music shorts", "dance shorts",
    "uzbekistan shorts", "trending shorts", "comedy shorts",
    "song shorts", "reels music", "tiktok music shorts",
]

dead_keys = {}                   # kalit -> qachongacha ishlatilmasligi (epoch)
dead_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class QuotaError(Exception):
    pass


# ---------- SQLite ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
        name TEXT, surname TEXT, photo TEXT, google_id TEXT, created_at REAL, updated_at REAL);
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT NOT NULL, query TEXT NOT NULL, created_at REAL);
    CREATE TABLE IF NOT EXISTS likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT NOT NULL, video_id TEXT NOT NULL,
        title TEXT, channel TEXT, thumbnail TEXT, created_at REAL, UNIQUE(user_email, video_id));
    CREATE TABLE IF NOT EXISTS cache (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_h ON history(user_email);
    CREATE INDEX IF NOT EXISTS idx_l ON likes(user_email);
    """)
    conn.commit()
    conn.close()


def cache_read(key):
    """(qiymat, yoshi_sekundda) yoki (None, None)"""
    conn = db()
    row = conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return None, None
    try:
        return json.loads(row["value"]), time.time() - row["ts"]
    except ValueError:
        return None, None


def cache_write(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO cache (key, value, ts) VALUES (?,?,?)",
                 (key, json.dumps(value, ensure_ascii=False), time.time()))
    conn.execute("DELETE FROM cache WHERE ts < ?", (time.time() - STALE_MAX,))
    conn.commit()
    conn.close()


# ---------- YouTube API ----------
def clean_title(text):
    if not text:
        return "Nomsiz video"
    text = html.unescape(text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip() or "Nomsiz video"


def yt_get(url):
    req = Request(url, headers={"User-Agent": "GlassMusic/2.0", "Accept": "application/json"})
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def google_error(exc):
    try:
        err = json.loads(exc.read().decode("utf-8")).get("error", {})
        errors = err.get("errors") or [{}]
        return errors[0].get("reason", ""), err.get("message", "")
    except Exception:
        return "", ""


def uz_error(reason, msg):
    if reason == "commentsDisabled":
        return "Bu video uchun izohlar o‘chirilgan."
    if reason == "videoNotFound":
        return "Video topilmadi."
    return "YouTube xatosi: " + (msg or reason or "noma'lum")


def yt_api(endpoint, params):
    """YouTube API ga so‘rov. Kalit limitga urilsa, keyingi kalit bilan qayta urinadi."""
    if not API_KEYS:
        raise ApiError(500, "YOUTUBE_API_KEY sozlanmagan.")
    now = time.time()
    with dead_lock:
        alive = [k for k in API_KEYS if dead_keys.get(k, 0) < now]
    if not alive:
        raise QuotaError()
    for key in alive:
        url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urlencode({**params, "key": key})
        try:
            return yt_get(url)
        except HTTPError as exc:
            reason, msg = google_error(exc)
            if reason in QUOTA_REASONS or exc.code == 429 or reason in KEY_REASONS:
                with dead_lock:
                    dead_keys[key] = time.time() + KEY_COOLDOWN
                continue
            raise ApiError(exc.code if 400 <= exc.code < 600 else 502, uz_error(reason, msg))
        except (URLError, OSError):
            raise ApiError(502, "YouTube bilan aloqa yo‘q. Internetni tekshiring.")
    raise QuotaError()


def enrich_stats(videos):
    """Statistika va kanal rasmini qo‘shadi. Xato bo‘lsa, videolar o‘zgarishsiz qaytadi."""
    ids = [v["id"] for v in videos if v.get("id")]
    if not ids:
        return videos
    try:
        payload = yt_api("videos", {"part": "statistics,snippet", "id": ",".join(ids[:50])})
    except (ApiError, QuotaError):
        return videos
    by_id = {}
    for item in payload.get("items", []):
        st = item.get("statistics", {})
        sn = item.get("snippet", {})
        by_id[item.get("id")] = {
            "views": int(st.get("viewCount") or 0),
            "likes": int(st.get("likeCount") or 0),
            "comments": int(st.get("commentCount") or 0),
            "channelId": sn.get("channelId", ""),
        }
    for v in videos:
        v.update(by_id.get(v["id"], {}))
    ch_ids = list({v.get("channelId") for v in videos if v.get("channelId")})
    if ch_ids:
        try:
            cp = yt_api("channels", {"part": "snippet", "id": ",".join(ch_ids[:50])})
            ch_map = {}
            for it in cp.get("items", []):
                th = it.get("snippet", {}).get("thumbnails", {})
                ch_map[it.get("id")] = (th.get("default") or th.get("medium") or {}).get("url", "")
            for v in videos:
                v["channelThumb"] = ch_map.get(v.get("channelId"), "")
        except (ApiError, QuotaError):
            pass
    return videos


# ---------- HTTP handler ----------
class H(BaseHTTPRequestHandler):
    server_version = "GlassMusic/2.0"

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        def g(key, default=""):
            return qs.get(key, [default])[0]

        if path == "/":
            return self.serve_index()
        if path == "/api/search":
            return self.search_videos(g("q"), g("pageToken"), g("type", "video"), g("max", "24"))
        if path == "/api/user":
            return self.get_user(g("email"))
        if path == "/api/history":
            return self.get_history(g("email"))
        if path == "/api/likes":
            return self.get_likes(g("email"))
        if path == "/api/comments":
            return self.comments(g("id"))
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        # Boshqa fayllar (server.py, glass_music.db) ochiq berilmaydi
        self.send_json(404, {"error": "Topilmadi"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return self.send_json(413, {"error": "So‘rov juda katta."})
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if path == "/api/user":
            return self.save_user(data)
        if path == "/api/history":
            return self.add_history(data)
        if path == "/api/like":
            return self.toggle_like(data)
        self.send_json(404, {"error": "Topilmadi"})

    # ----- sahifa -----
    def serve_index(self):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                page = f.read()
        except OSError:
            return self.send_json(500, {"error": "index.html topilmadi"})
        page = page.replace("YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com", GOOGLE_CLIENT_ID)
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ----- qidiruv -----
    def search_videos(self, q, page_token, kind, max_str):
        q = (q or "").strip()
        shorts = kind == "shorts"
        if shorts:
            if not q:
                q = random.choice(SHORT_SEEDS)
            if "shorts" not in q.lower():
                q += " #shorts"
        if not q:
            return self.send_json(400, {"error": "Qidiruv matni bo‘sh."})
        try:
            maxr = max(1, min(50, int(max_str)))
        except ValueError:
            maxr = 24

        params = {"part": "snippet", "type": "video", "videoEmbeddable": "true",
                  "maxResults": maxr, "q": q}
        if shorts:
            params["videoDuration"] = "short"
        if page_token:
            params["pageToken"] = page_token

        key = "search:" + json.dumps(params, sort_keys=True)
        cached, age = cache_read(key)
        if cached is not None and age <= SEARCH_TTL:
            return self.send_json(200, cached)

        try:
            payload = yt_api("search", params)
        except QuotaError:
            if cached is not None and age <= STALE_MAX:
                return self.send_json(200, {**cached, "stale": True})
            return self.send_json(429, {"error": QUOTA_MSG})
        except ApiError as e:
            return self.send_json(e.status, {"error": e.message})

        videos = []
        for item in payload.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            sn = item.get("snippet", {})
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
        data = {
            "videos": videos,
            "nextPageToken": payload.get("nextPageToken") or "",
            "total": len(videos),
        }
        cache_write(key, data)
        self.send_json(200, data)

    # ----- izohlar -----
    def comments(self, video_id):
        video_id = (video_id or "").strip()
        if not video_id:
            return self.send_json(400, {"error": "id kerak"})
        params = {"part": "snippet", "order": "relevance", "maxResults": 20,
                  "textFormat": "plainText", "videoId": video_id}
        key = "comments:" + video_id
        cached, age = cache_read(key)
        if cached is not None and age <= COMMENTS_TTL:
            return self.send_json(200, cached)
        try:
            payload = yt_api("commentThreads", params)
        except QuotaError:
            if cached is not None:
                return self.send_json(200, cached)
            return self.send_json(200, {"comments": [], "error": QUOTA_MSG})
        except ApiError as e:
            return self.send_json(200, {"comments": [], "error": e.message})

        items = []
        for item in payload.get("items", []):
            sn = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            items.append({
                "author": clean_title(sn.get("authorDisplayName", "User")),
                "text": clean_title(sn.get("textDisplay", "")),
                "likes": int(sn.get("likeCount") or 0),
                "photo": sn.get("authorProfileImageUrl", ""),
            })
        data = {"comments": items}
        cache_write(key, data)
        self.send_json(200, data)

    # ----- foydalanuvchi -----
    def save_user(self, data):
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return self.send_json(400, {"error": "Email kerak"})
        now = time.time()
        conn = db()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE email=?", (email,))
        if c.fetchone():
            c.execute("UPDATE users SET name=?,surname=?,photo=?,google_id=?,updated_at=? WHERE email=?",
                      (data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                       data.get("google_id") or "", now, email))
        else:
            c.execute("INSERT INTO users (email,name,surname,photo,google_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                      (email, data.get("name") or "", data.get("surname") or "", data.get("photo") or "",
                       data.get("google_id") or "", now, now))
        conn.commit()
        conn.close()
        self.send_json(200, {"ok": True})

    def get_user(self, email):
        email = (email or "").strip().lower()
        if not email:
            return self.send_json(400, {"error": "Email kerak"})
        conn = db()
        row = conn.execute("SELECT email,name,surname,photo,google_id FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if not row:
            return self.send_json(404, {"error": "topilmadi"})
        self.send_json(200, dict(row))

    # ----- tarix -----
    def add_history(self, data):
        email = (data.get("email") or "").strip().lower()
        query = (data.get("query") or "").strip()
        if not email or not query:
            return self.send_json(400, {"error": "email va query kerak"})
        conn = db()
        conn.execute("INSERT INTO history (user_email,query,created_at) VALUES (?,?,?)", (email, query, time.time()))
        conn.execute("""DELETE FROM history WHERE user_email=? AND id NOT IN (
            SELECT id FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 50)""", (email, email))
        conn.commit()
        conn.close()
        self.send_json(200, {"ok": True})

    def get_history(self, email):
        email = (email or "").strip().lower()
        if not email:
            return self.send_json(400, {"error": "Email kerak"})
        conn = db()
        rows = [r["query"] for r in conn.execute(
            "SELECT query FROM history WHERE user_email=? ORDER BY created_at DESC LIMIT 40", (email,)).fetchall()]
        conn.close()
        seen, unique = set(), []
        for q in rows:
            if q.lower() not in seen:
                seen.add(q.lower())
                unique.append(q)
        self.send_json(200, {"history": unique})

    # ----- sevimlilar -----
    def toggle_like(self, data):
        email = (data.get("email") or "").strip().lower()
        vid = (data.get("video_id") or "").strip()
        if not email or not vid:
            return self.send_json(400, {"error": "email va video_id kerak"})
        conn = db()
        c = conn.cursor()
        c.execute("SELECT id FROM likes WHERE user_email=? AND video_id=?", (email, vid))
        if c.fetchone():
            c.execute("DELETE FROM likes WHERE user_email=? AND video_id=?", (email, vid))
            liked = False
        else:
            c.execute("INSERT INTO likes (user_email,video_id,title,channel,thumbnail,created_at) VALUES (?,?,?,?,?,?)",
                      (email, vid, data.get("title") or "", data.get("channel") or "",
                       data.get("thumbnail") or "", time.time()))
            liked = True
        conn.commit()
        conn.close()
        self.send_json(200, {"liked": liked})

    def get_likes(self, email):
        email = (email or "").strip().lower()
        if not email:
            return self.send_json(400, {"error": "Email kerak"})
        conn = db()
        rows = [dict(r) for r in conn.execute(
            "SELECT video_id AS id, title, channel, thumbnail FROM likes WHERE user_email=? ORDER BY created_at DESC",
            (email,)).fetchall()]
        conn.close()
        self.send_json(200, {"likes": rows})

    # ----- yordamchi -----
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    init_db()
    if not API_KEYS:
        print("OGOHLANTIRISH: YOUTUBE_API_KEY o‘rnatilmagan — qidiruv ishlamaydi.")
    s = Server((HOST, PORT), H)
    print(f"Glass Music: http://127.0.0.1:{PORT}  (API kalitlar: {len(API_KEYS)} ta)")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        print("\nto‘xtatildi")
    finally:
        s.server_close()
