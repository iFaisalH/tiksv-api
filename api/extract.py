
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
import yt_dlp

ALLOWED = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "t.co",
    "pinterest.com", "pin.it",
    "reddit.com", "redd.it"
)

ORIGINS = {
    "https://www.vidkeep.net",
    "https://vidkeep.net"
}


class handler(BaseHTTPRequestHandler):

    def cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Methods", "POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type"
        )
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            origin = self.headers.get("Origin", "")
            if origin and origin not in ORIGINS:
                self.respond(403, {"error": "Origin not allowed"})
                return

            n = int(self.headers.get("Content-Length", "0"))
            if n < 1 or n > 10000:
                raise ValueError("Invalid request")

            payload = json.loads(self.rfile.read(n))
            url = str(payload.get("url", "")).strip()

            u = urlsplit(url)
            host = (u.hostname or "").lower()

            if (
                u.scheme not in ("http", "https")
                or not any(
                    host == d or host.endswith("." + d)
                    for d in ALLOWED
                )
            ):
                raise ValueError("هذا الموقع غير مدعوم")

            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "socket_timeout": 12,
                "retries": 1,
                "format": (
                    "best[ext=mp4][vcodec!=none][acodec!=none]"
                    "/best[vcodec!=none][acodec!=none]"
                )
            }

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                raise ValueError("تعذر استخراج الفيديو")

            if info.get("entries"):
                info = next(
                    (x for x in info["entries"] if x),
                    None
                )

            direct = (info or {}).get("url")

            if not direct or not direct.startswith("https://"):
                raise ValueError(
                    "تعذر الحصول على رابط فيديو مباشر"
                )

            self.respond(200, {
                "url": direct,
                "headers": info.get("http_headers", {})
            })

        except Exception as e:
            self.respond(400, {"error": str(e)[:300]})

    def respond(self, status, data):
        out = json.dumps(
            data, ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.cors_headers()
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
