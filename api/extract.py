import json
import os
import subprocess
import tempfile
import time
import warnings
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


class ProviderError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def extract_youtube_provider(url):
    """Use the documented public API without accounts, credits, or API keys."""
    base = "https://save.brianrianrehan.com"
    deadline = time.monotonic() + 35

    def request(endpoint, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(504, "استغرق تجهيز الفيديو وقتًا أطول من المتوقع. حاول لاحقًا.")
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(endpoint, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VidKeep/1.0"
        })
        try:
            with urlopen(req, timeout=min(12, remaining)) as response:
                result = json.loads(response.read(1024 * 1024))
            if not isinstance(result, dict):
                raise ValueError()
            if result.get("success") is False or result.get("error"):
                raise ProviderError(502, "تعذر تجهيز الفيديو لدى خدمة يوتيوب المجانية.")
            data = result.get("data", result)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except HTTPError as exc:
            if exc.code == 429:
                raise ProviderError(503, "خدمة يوتيوب المجانية وصلت إلى حد الاستخدام المؤقت. حاول لاحقًا.") from None
            if exc.code == 413:
                raise ProviderError(413, "حجم هذا الفيديو يتجاوز حد الخدمة المجانية.") from None
            raise ProviderError(502, "تعذر تجهيز الفيديو لدى خدمة يوتيوب المجانية.") from None
        except (URLError, TimeoutError, ValueError):
            raise ProviderError(502, "تعذر الاتصال بخدمة يوتيوب المجانية أو قراءة استجابتها.") from None

    job = request(base + "/api/v1/download?wait=false", {
        "url": url, "format": "video", "quality": "best", "outputFormat": "mp4"
    })
    job_id = str(job.get("downloadId") or "")
    if not job_id or len(job_id) > 128 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in job_id):
        raise ProviderError(502, "لم ترجع خدمة يوتيوب رقم طلب صالحًا.")
    while True:
        status = job.get("status")
        if status == "completed":
            direct = str(job.get("downloadUrl") or "")
            parsed = urlsplit(direct)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or (parsed.hostname or "").lower() != "save.brianrianrehan.com"):
                raise ProviderError(502, "لم ترجع خدمة يوتيوب رابط تنزيل صالحًا.")
            return direct
        if status == "failed":
            raise ProviderError(422, "تعذر تنزيل هذا المقطع. قد يكون خاصًا أو محذوفًا أو مقيدًا.")
        if status not in ("pending", "processing", "downloading", "converting"):
            raise ProviderError(502, "استجابة غير متوقعة من خدمة يوتيوب المجانية.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(504, "استغرق تجهيز الفيديو وقتًا أطول من المتوقع. حاول لاحقًا.")
        time.sleep(min(1.5, remaining))
        job = request(base + "/api/v1/download/" + job_id)


def select_video(ctx):
    candidates = [
        item
        for item in reversed(ctx.get("formats") or [])
        if str(item.get("url") or "").startswith("https://")
        and item.get("vcodec") != "none"
        and item.get("acodec") != "none"
        and not item.get("has_drm")
        and item.get("ext") not in (
            "mhtml", "jpg", "jpeg", "png", "webp"
        )
        and (
            item.get("protocol") == "https"
            or (
                not item.get("protocol")
                and item.get("ext") in ("mp4", "webm", "mov")
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

    def send_youtube_video(self, direct):
        """Stream provider media through our CORS-enabled endpoint."""
        req = Request(direct, headers={"User-Agent": "VidKeep/1.0"})
        try:
            upstream = urlopen(req, timeout=30)
        except (HTTPError, URLError, TimeoutError):
            raise ProviderError(502, "تعذر نقل ملف الفيديو من مزود يوتيوب.") from None
        with upstream:
            content_type = upstream.headers.get("Content-Type", "video/mp4")
            if not (content_type.startswith("video/") or content_type.startswith("application/octet-stream")):
                raise ProviderError(502, "لم يرجع مزود يوتيوب ملف فيديو صالحًا.")
            self.send_response(200)
            self.cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", 'attachment; filename="vidkeep_youtube.mp4"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Disposition")
            length = upstream.headers.get("Content-Length", "")
            if length.isdigit():
                self.send_header("Content-Length", length)
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (OSError, TimeoutError):
                # Headers are already sent: never append JSON to a media file.
                self.close_connection = True

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

            is_youtube = matches_domain(host, YOUTUBE)

            if is_youtube:
                direct = extract_youtube_provider(url)
                # The existing Blogger downloader sends both fields and
                # accepts video bytes. Internal API callers still get JSON.
                if payload.get("input") == url:
                    self.send_youtube_video(direct)
                else:
                    self.respond(200, {"url": direct})
                return

            opts = {
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

            if is_youtube:
                try:
                    import nodejs_wheel

                    node_path = os.path.join(
                        os.path.dirname(nodejs_wheel.__file__),
                        "bin",
                        "node"
                    )

                    if not os.path.isfile(node_path):
                        raise RuntimeError(
                            "Bundled Node executable not found"
                        )

                    result = subprocess.run(
                        [node_path, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=True
                    )

                    print(
                        "VidKeep Node ready:",
                        result.stdout.strip(),
                        flush=True
                    )

                except Exception as exc:
                    print(
                        "VidKeep Node setup error:",
                        type(exc).__name__,
                        str(exc),
                        flush=True
                    )
                    self.respond(503, {
                        "error": (
                            "تعذر تشغيل Node على خادم التحميل. "
                            "تحقق من تثبيت nodejs-wheel-binaries "
                            "ونجاح النشر."
                        )
                    })
                    return

                opts["js_runtimes"] = {
                    "node": {"path": node_path}
                }
                opts["remote_components"] = ["ejs:github"]

            with tempfile.TemporaryDirectory(prefix="vidkeep-") as temp_dir:
                cookies = os.environ.get("YOUTUBE_COOKIES", "") if is_youtube else ""
                if cookies.strip():
                    cookies = cookies.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
                    cookie_path = os.path.join(temp_dir, "youtube-cookies.txt")
                    with open(cookie_path, "w", encoding="utf-8") as cookie_file:
                        cookie_file.write(cookies.rstrip("\n") + "\n")
                    os.chmod(cookie_path, 0o600)
                    try:
                        jar = MozillaCookieJar(cookie_path)
                        # CookieJar warnings may contain cookie contents.
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            jar.load(ignore_discard=True, ignore_expires=True)
                        valid = any(
                            matches_domain(cookie.domain.lstrip(".").lower(), YOUTUBE)
                            and (cookie.expires in (None, 0) or cookie.expires > time.time())
                            for cookie in jar
                        )
                        if not valid:
                            raise ValueError()
                    except Exception:
                        self.respond(503, {"error": "جلسة يوتيوب المحفوظة غير صالحة أو منتهية. حدّث YOUTUBE_COOKIES بصيغة Netscape."})
                        return
                    opts["cookiefile"] = cookie_path
                    print("VidKeep YouTube: using configured session", flush=True)
                elif is_youtube:
                    print("VidKeep YouTube: no configured session", flush=True)

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)

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

            self.respond(200, {"url": direct})

        except ProviderError as exc:
            self.respond(exc.status, {"error": str(exc)})

        except yt_dlp.utils.DownloadError as exc:
            detail = str(exc).lower()

            if "not a bot" in detail:
                message = (
                    "يوتيوب يطلب التحقق من خادم التحميل. "
                    "الجلسة الحالية لم تُقبل أو تحتاج إلى تجديد."
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
                    "رفضت المنصة الوصول للفيديو، "
                    "أو يتطلب الفيديو تسجيل الدخول."
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

        except Exception as exc:
            print(
                "VidKeep server error:",
                type(exc).__name__,
                flush=True
            )
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
