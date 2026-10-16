import json
import os
import tempfile
from http.cookiejar import MozillaCookieJar
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

YOUTUBE = (
    "youtube.com", "youtu.be", "youtube-nocookie.com"
)

ORIGINS = {
    "https://www.vidkeep.net",
    "https://vidkeep.net"
}


def matches_domain(host, domains):
    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def select_video(ctx):
    formats = reversed(ctx.get("formats") or [])

    candidates = [
        f for f in formats
        if str(f.get("url") or "").startswith("https://")
        and f.get("vcodec") != "none"
        and f.get("acodec") != "none"
        and not f.get("has_drm")
        and f.get("ext") not in (
            "mhtml", "jpg", "jpeg", "png", "webp"
        )
        and (
            f.get("protocol") == "https"
            or (
                not f.get("protocol")
                and f.get("ext") in ("mp4", "webm", "mov")
            )
        )
    ]

    for item in candidates:
        if item.get("ext") == "mp4":
            yield item
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
                self.respond(403, {
                    "error": "Origin not allowed"
                })
                return

            length = int(
                self.headers.get("Content-Length", "0")
            )

            if not 1 <= length <= 10000:
                raise ValueError()

            payload = json.loads(self.rfile.read(length))

            if not isinstance(payload, dict):
                raise ValueError()

            url = str(payload.get("url", "")).strip()
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()

            if (
                parsed.scheme not in ("http", "https")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 80, 443)
                or not matches_domain(host, ALLOWED)
            ):
                raise ValueError()

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

            # مجلد مؤقت منفصل لكل طلب ويُحذف بعد انتهائه.
            with tempfile.TemporaryDirectory(
                prefix="vidkeep-"
            ) as temp_dir:
                if matches_domain(host, YOUTUBE):
                    cookies = os.environ.get(
                        "YOUTUBE_COOKIES", ""
                    )

                    if not cookies.strip():
                        self.respond(503, {
                            "error": (
                                "لم يتم إعداد جلسة يوتيوب "
                                "على خادم التحميل."
                            )
                        })
                        return

                    cookies = cookies.replace(
                        "\r\n", "\n"
                    ).replace("\r", "\n").lstrip("\ufeff")

                    cookie_path = os.path.join(
                        temp_dir, "youtube-cookies.txt"
                    )

                    with open(
                        cookie_path,
                        "w",
                        encoding="utf-8",
                        newline="\n"
                    ) as cookie_file:
                        cookie_file.write(
                            cookies.rstrip("\n") + "\n"
                        )

                    os.chmod(cookie_path, 0o600)

                    # التحقق دون طباعة بيانات الجلسة.
                    try:
                        jar = MozillaCookieJar(cookie_path)
                        jar.load(
                            ignore_discard=True,
                            ignore_expires=True
                        )
                        valid = any(
                            cookie.domain.lstrip(".")
                            == "youtube.com"
                            or cookie.domain.lstrip(".")
                            .endswith(".youtube.com")
                            for cookie in jar
                        )
                        if not valid:
                            raise ValueError()
                    except Exception:
                        self.respond(503, {
                            "error": (
                                "ملف جلسة يوتيوب غير صالح. "
                                "صدّره بصيغة Netscape والصق "
                                "محتواه كاملًا في "
                                "YOUTUBE_COOKIES."
                            )
                        })
                        return

                    opts["cookiefile"] = cookie_path

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
                self.respond(400, {
                    "error": "تعذر استخراج بيانات الفيديو."
                })
                return

            direct = info.get("url") or ""

            if not direct.startswith("https://"):
                self.respond(400, {
                    "error": (
                        "لم يتوفر ملف مباشر يجمع "
                        "الفيديو والصوت."
                    )
                })
                return

            # لا نُرجع Cookies أو ترويسات المصادقة للزائر.
            self.respond(200, {"url": direct})

        except yt_dlp.utils.DownloadError as exc:
            detail = str(exc).lower()

            if "not a bot" in detail:
                message = (
                    "يوتيوب ما زال يطلب التحقق من "
                    "خادم التحميل رغم استخدام الجلسة."
                )
            elif "requested format" in detail:
                message = (
                    "لم تتوفر صيغة مباشرة تجمع "
                    "الفيديو والصوت لهذا الرابط."
                )
            elif any(
                word in detail
                for word in (
                    "login", "sign in", "cookies",
                    "private", "checkpoint"
                )
            ):
                message = (
                    "رفضت المنصة الوصول للفيديو. "
                    "قد تحتاج الجلسة إلى تجديد أو "
                    "لا تملك صلاحية الوصول للفيديو."
                )
            else:
                message = (
                    "تعذر استخراج الفيديو من المنصة."
                )

            self.respond(400, {"error": message})

        except (ValueError, TypeError):
            self.respond(400, {
                "error": "تحقق من الرابط وصحة الطلب."
            })

        except Exception:
            self.respond(500, {
                "error": "حدث خطأ في خدمة التحميل."
            })

    def respond(self, status, data):
        output = json.dumps(
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
            "Content-Length", str(len(output))
        )
        self.end_headers()
        self.wfile.write(output)
