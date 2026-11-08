#!/usr/bin/env python3
"""
Glass Music — simple YouTube search + static file server.
Run:  YOUTUBE_API_KEY=your_key python3 server.py
"""
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()


class GlassMusicHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/search":
            self.handle_search(parse_qs(parsed.query).get("q", [""])[0])
            return

        # Default: serve index.html for root
        if parsed.path in ("/", ""):
            self.path = "/index.html"

        return super().do_GET()

    def handle_search(self, query):
        query = query.strip()
        if not query:
            self.send_json(400, {"error": "Qidiruv matni bo‘sh."})
            return

        if not API_KEY:
            self.send_json(500, {
                "error": "YouTube API kaliti topilmadi. YOUTUBE_API_KEY muhit o‘zgaruvchisini o‘rnating."
            })
            return

        url = (
            "https://www.googleapis.com/youtube/v3/search"
            "?part=snippet&type=video&videoEmbeddable=true&maxResults=12"
            "&q=" + quote(query)
            + "&key=" + quote(API_KEY)
        )

        request = Request(url, headers={"User-Agent": "GlassMusic/1.1"})
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
                message = details.get("error", {}).get("message", "YouTube API xatosi")
            except Exception:
                message = "YouTube API HTTP xatosi: " + str(exc.code)
            self.send_json(exc.code if 400 <= exc.code < 600 else 502, {"error": message})
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
            thumbnails = snippet.get("thumbnails", {})
            thumb = (
                thumbnails.get("high", {}).get("url")
                or thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
                or ""
            )
            videos.append({
                "id": video_id,
                "title": snippet.get("title", "Nomsiz video"),
                "channel": snippet.get("channelTitle", "YouTube"),
                "thumbnail": thumb,
            })

        self.send_json(200, {"videos": videos})

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print("[%s] %s" % (self.log_date_time_string(), format % args))


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = ThreadingHTTPServer((HOST, PORT), GlassMusicHandler)
    print(f"Glass Music ishlayapti: http://127.0.0.1:{PORT}")
    print("To‘xtatish: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer to‘xtatildi.")
    finally:
        server.server_close()
