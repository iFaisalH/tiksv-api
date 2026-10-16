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


def select_video(ctx):
    formats = list(reversed(ctx.get("formats") or []))

    candidates = [
        f for f in formats
        if f.get("url", "").startswith("https://")
        and f.get("vcodec") != "none"
        and f.get("ext") not in (
            "mhtml", "jpg", "jpeg", "png", "webp"
        )
        and not f.get("has_drm")
        and (
            f.get("protocol") == "https"
            or (
                not f.get("protocol")
                and f.get("ext") in ("mp4", "webm", "mov")
            )
        )
        and f.get("acodec") != "none"
    ]

    for f in candidates:
        if f.get("ext") == "mp4":
            yield f
            return

    if candidates:
        yield candidates[0]


class handler(BaseHTTPRequestHandler):

    def cors_headers(self):
        origin = self.headers.get("Origin", "")

        if origin in ORIGINS:
            self.send_header(
                "Access-Control-Allow-Origin", origin
            )

        self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )
        self.send_header(
            "Access-Control-Max-Age", "86400"
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            origin = self.headers.get("Origin", "")

            if origin and origin not in ORIGINS:
                self.respond(
                    403, {"error": "Origin not allowed"}
                )
                return

            n = int(
                self.headers.get("Content-Length", "0")
            )

            if n < 1 or n > 10000:
                raise ValueError("طلب غير صالح")

            payload = json.loads(self.rfile.read(n))

            if not isinstance(payload, dict):
                raise ValueError("طلب غير صالح")

            url = str(payload.get("url", "")).strip()
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()

            if (
                parsed.scheme not in ("http", "https")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 80, 443)
                or not any(
                    host == domain
                    or host.endswith("." + domain)
                    for domain in ALLOWED
                )
            ):
                raise ValueError(
                    "الرابط غير صالح أو الموقع غير مدعوم"
                )

            opts = {
                "js_runtimes": {"node": {}},
                "quiet": True,
                "no_warnings": False,
                "noplaylist": True,
                "playlistend": 1,
                "skip_download": True,
                "socket_timeout": 12,
                "retries": 1,
                "cachedir": False,
                "format": select_video
            }

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    url, download=False
                )

            while info and "entries" in info:
                info = next(
                    (
                        item
                        for item in (info.get("entries") or [])
                        if item
                    ),
                    None
                )

            if not info:
                raise ValueError(
                    "تعذر استخراج بيانات الفيديو"
                )

            direct = info.get("url", "")

            if not direct.startswith("https://"):
                raise ValueError(
                    "لم يتوفر ملف فيديو مباشر لهذا الرابط"
                )

            self.respond(200, {
                "url": direct,
                "headers": info.get("http_headers") or {}
            })

        except yt_dlp.utils.DownloadError as exc:
            detail = str(exc)
            lower_detail = detail.lower()
            print("VidKeep extraction error:", detail)

            if (
                "not a bot" in lower_detail
                or "confirm you're" in lower_detail
                or "confirm you’re" in lower_detail
            ):
                message = (
                    "يوتيوب يطلب التحقق من خادم التحميل "
                    "بأنه ليس روبوتًا. تعذر استخراج الفيديو."
                )
            elif "requested format" in lower_detail:
                message = (
                    "لم تتوفر صيغة مباشرة تجمع الفيديو "
                    "والصوت لهذا الرابط."
                )
            elif any(
                word in lower_detail
                for word in (
                    "login", "sign in", "cookies",
                    "private", "checkpoint"
                )
            ):
                message = (
                    "تعذر الوصول للفيديو؛ قد يكون خاصًا "
                    "أو تتطلب المنصة تسجيل الدخول."
                )
            else:
                message = (
                    "تعذر جلب الفيديو من المنصة. "
                    "جرّب رابط فيديو عام آخر."
                )

            self.respond(400, {"error": message})

        except (ValueError, TypeError) as exc:
            print("VidKeep request error:", str(exc))
            self.respond(400, {
                "error": "تحقق من الرابط وصحة الطلب"
            })

        except Exception as exc:
            print("VidKeep server error:", str(exc))
            self.respond(500, {
                "error": "حدث خطأ في خدمة التحميل"
            })

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
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Length", str(len(out))
        )
        self.end_headers()
        self.wfile.write(out)
