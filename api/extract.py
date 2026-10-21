import json
import os
import subprocess
import tempfile
import time
import warnings
import sys
import signal
import shutil
import re
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, urljoin
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

import yt_dlp


ALLOWED = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "t.co",
    "pinterest.com", "pin.it",
    "reddit.com", "redd.it",
    "snapchat.com"
)

YOUTUBE = (
    "youtube.com", "youtu.be", "youtube-nocookie.com"
)

SNAPCHAT = ("snapchat.com",)
SNAPCHAT_CDN = ("sc-cdn.net", "snapcdn.net", "snapchat.com", "snap.com")

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



# Snapchat public-profile helpers are bundled into this entrypoint intentionally.
# Vercel Python Functions may exclude non-entrypoint sibling modules at runtime.
"""Read media exposed on public Snapchat profile pages. No login or private APIs."""
import html
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

PROFILE_PATTERN = re.compile(r"^/(?:add/|@)([A-Za-z][A-Za-z0-9_.-]{1,30})/?$")
CDNS = ("sc-cdn.net", "snapcdn.net", "snapchat.com", "snap.com")
SNAP = ("snapchat.com",)
SPOTLIGHT_ID = re.compile(r"(?:/|%2F)spotlight(?:/|%2F)([A-Za-z0-9_-]{20,128})", re.I)
USER_AGENT = "Mozilla/5.0 (compatible; VidKeep/1.0)"


def host_allowed(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def safe_media_url(url):
    try:
        u = urlsplit(str(url))
        return (u.scheme == "https" and u.username is None and u.password is None
                and u.port in (None, 443) and host_allowed((u.hostname or "").lower(), CDNS))
    except (ValueError, TypeError):
        return False


def profile_username(url):
    try:
        u = urlsplit(url)
        if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
                or not host_allowed((u.hostname or "").lower(), SNAP)):
            return None
        match = PROFILE_PATTERN.fullmatch(u.path)
        if match:
            return match.group(1)
        if (u.hostname or "").lower() == "story.snapchat.com":
            match = re.fullmatch(r"/([A-Za-z][A-Za-z0-9_.-]{1,30})/?", u.path)
            return match.group(1) if match else None
        return None
    except (ValueError, TypeError):
        return None


class OnlySnapchatRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urljoin
        target = urljoin(req.full_url, newurl)
        parsed = urlsplit(target)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not host_allowed((parsed.hostname or "").lower(), SNAP)):
            raise ValueError("Snapchat redirected outside its trusted domain")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _read_public_json(page_url):
    """Read public Snapchat page metadata plus the resolved share URL."""
    request = Request(page_url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
    })
    with build_opener(OnlySnapchatRedirects()).open(request, timeout=15) as response:
        final_url = response.geturl()
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Snapchat page is larger than supported")
        markup = raw.decode("utf-8", "replace")

    # The primary page may offer the shared video as og:video. Never parse
    # recommended videos or social thumbnails as a substitute for the story.
    primary_meta = {}
    for tag in re.findall(r"<meta\b[^>]{0,4096}>", markup, flags=re.I):
        attrs = dict((k.lower(), html.unescape(v)) for k, _, v in re.findall(
            r'([A-Za-z_:][-A-Za-z0-9_:]*)\s*=\s*(["\x27])(.*?)\2', tag, flags=re.S))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in ("og:video", "og:video:url", "og:video:secure_url", "og:url", "twitter:player:stream"):
            value = attrs.get("content", "")
            if value:
                primary_meta[key] = value

    pattern = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
    for match in pattern.finditer(markup):
        attributes, body = match.group(1), match.group(2)
        if not (re.search(r"type\s*=\s*['\"]application/json['\"]", attributes, re.I)
                or "__NEXT_DATA__" in attributes):
            continue
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            try:
                data = json.loads(html.unescape(body))
            except (ValueError, TypeError):
                continue
        props = data.get("props", {}).get("pageProps") if isinstance(data, dict) else None
        if isinstance(props, dict):
            props = dict(props)
            props["__vidkeep_primary_meta"] = primary_meta
            props["__vidkeep_final_url"] = final_url
            return props

    return {"__vidkeep_primary_meta": primary_meta,
            "__vidkeep_final_url": final_url}



def _unwrap(value):
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _media_url(snap):
    if not isinstance(snap, dict):
        return None
    choices = [
        snap.get("mediaUrl"),
        snap.get("snapUrls", {}).get("mediaUrl") if isinstance(snap.get("snapUrls"), dict) else None,
        snap.get("videoMetadata", {}).get("contentUrl") if isinstance(snap.get("videoMetadata"), dict) else None,
        snap.get("contentUrl"),
    ]
    for candidate in choices:
        candidate = _unwrap(candidate)
        if isinstance(candidate, str) and safe_media_url(candidate):
            return candidate
    return None


def _video(snap, kind):
    if not isinstance(snap, dict):
        return False
    media_type = str(_unwrap(snap.get("snapMediaType")) or _unwrap(snap.get("mediaType")) or "").lower()
    if "image" in media_type or "photo" in media_type or media_type in ("0", "image"):
        return False
    # Spotlight entries describe videos, while stories may have photo snaps.
    if kind == "spotlight":
        return bool(_spotlight_id(snap) or _media_url(snap))
    metadata = snap.get("videoMetadata")
    if isinstance(metadata, dict) and metadata.get("contentUrl"):
        return True
    media = _media_url(snap)
    return bool(media and not re.search(r"\.(?:png|jpe?g|webp)(?:\?|$)", media, re.I))


def _spotlight_id(data):
    if not isinstance(data, dict):
        return None
    sid = _unwrap(data.get("storyId")) or _unwrap(data.get("snapId"))
    if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", sid):
        return sid
    one_link = data.get("oneLinkParams")
    deep_link = one_link.get("deepLinkUrl") if isinstance(one_link, dict) else ""
    found = SPOTLIGHT_ID.search(str(_unwrap(deep_link) or ""))
    return found.group(1) if found else None


def collect_public_items(page_props, username):
    """Return only this public profile's story, highlight and Spotlight items."""
    items, seen = [], set()

    def append(kind, snap, pos):
        if not isinstance(snap, dict) or not _video(snap, kind):
            return
        media = _media_url(snap)
        sid = _spotlight_id(snap)
        if kind == "spotlight" and sid:
            source = "https://www.snapchat.com/spotlight/" + sid
            mode = "video"
        elif media:
            source = media
            mode = "media"
        else:
            return
        identity = sid or source
        if identity in seen:
            return
        seen.add(identity)
        title = kind + " " + str(len(items) + 1)
        items.append({"kind": kind, "title": title, "url": source, "mode": mode})

    story = page_props.get("story")
    if isinstance(story, dict):
        for i, snap in enumerate(story.get("snapList") or []):
            append("story", snap, i)
    public = page_props.get("publicUserProfile")
    if isinstance(public, dict):
        if isinstance(public.get("story"), dict):
            for i, snap in enumerate(public["story"].get("snapList") or []):
                append("story", snap, i)
    for group in page_props.get("curatedHighlights") or []:
        if isinstance(group, dict):
            for i, snap in enumerate(group.get("snapList") or []):
                append("highlight", snap, i)
    for entry in page_props.get("spotlightHighlights") or []:
        append("spotlight", entry, len(items))
    for entry in page_props.get("spotlightStoryMetadata") or []:
        append("spotlight", entry, len(items))
    # Some profile versions provide a nested publicProfileInfo.story snapList.
    profile = page_props.get("publicProfileInfo")
    if isinstance(profile, dict):
        profile_story = profile.get("story")
        if isinstance(profile_story, dict):
            for i, snap in enumerate(profile_story.get("snapList") or []):
                append("story", snap, i)

    # No arbitrary item quota: return every public video present in this page.
    # Some profiles expose only a subset, such as the latest 16 Spotlights.
    return {"username": username, "items": items, "count": len(items),
            "truncated": False, "scope": "public-page-visible"}



def public_story_items_from_share(url):
    """Return primary shared Story/Highlight videos, never recommended media.

    For /t/ links this may contain more than one story snap. A caller can
    display each one rather than rejecting the entire public collection.
    """
    u = urlsplit(url)
    path = u.path
    if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
            or not host_allowed((u.hostname or "").lower(), SNAP)):
        return []
    if not (re.fullmatch(r"/t/[A-Za-z0-9]{8}/?", path)
            or re.fullmatch(r"/@[A-Za-z0-9_.-]+/highlight/[A-Za-z0-9-]{16,128}/?", path)
            or re.fullmatch(r"/o/[A-Za-z0-9_-]{20,128}/?", path)
            or re.fullmatch(r"/@[A-Za-z0-9_.-]+/story/[A-Za-z0-9_-]{12,128}/?", path)):
        return []

    try:
        props = _read_public_json(url)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return []

    unique, items = set(), []
    def add_media(snap):
        if not isinstance(snap, dict) or not _video(snap, "story"):
            return
        media = _media_url(snap)
        if media and media not in unique:
            unique.add(media)
            items.append({"kind": "story", "title": "story " + str(len(items) + 1),
                          "url": media, "mode": "media"})

    def primary_snaps(source, depth=0):
        """Walk only explicitly named primary/story containers (not suggestions)."""
        if depth > 5 or not isinstance(source, (dict, list)):
            return
        if isinstance(source, list):
            for entry in source:
                primary_snaps(entry, depth + 1)
            return
        if _media_url(source):
            add_media(source)
        for key in ("snapList", "snaps", "story", "storyData", "selectedStory",
                    "preselectedStory", "playerStory", "premiumStory",
                    "highlightStory", "snap", "snapUrls"):
            value = source.get(key)
            if isinstance(value, (dict, list)):
                primary_snaps(value, depth + 1)

    for key in ("selectedStory", "preselectedStory", "playerStory", "premiumStory",
                "highlightStory", "story", "storyData"):
        primary_snaps(props.get(key))

    # Use main-page canonical video metadata only when no story list exists.
    # Never use thumbnail fields or random video URLs embedded in page scripts.
    if not items:
        meta = props.get("__vidkeep_primary_meta") or {}
        for field in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
            media = meta.get(field)
            if media and safe_media_url(media):
                items.append({"kind": "story", "title": "story 1",
                              "url": media, "mode": "media"})
                break
    return items


def single_public_story_from_share(url):
    items = public_story_items_from_share(url)
    return items[0]["url"] if len(items) == 1 else None



def list_public_profile(url):
    username = profile_username(url)
    if not username:
        raise ValueError("Not a Snapchat public profile URL")
    pages = ("https://www.snapchat.com/add/" + username,
             "https://www.snapchat.com/@" + username)
    last_error = None
    for page in pages:
        try:
            props = _read_public_json(page)
            if props:
                result = collect_public_items(props, username)
                if result["count"] > 0:
                    return result
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last_error = error
    if last_error:
        raise ValueError("Cannot read public Snapchat profile right now") from None
    return {"username": username, "items": [], "count": 0,
            "truncated": False, "scope": "public-page-visible"}


def snapchat_story_items_for_url(url):
    """Resolve only public Story and Highlight videos associated with this URL."""
    if profile_username(url):
        listing = list_public_profile(url)
        return [item for item in listing["items"]
                if item.get("kind") in ("story", "highlight") and item.get("mode") == "media"]
    return [item for item in public_story_items_from_share(url)
            if item.get("mode") == "media"]


def safe_snapchat_media(url):
    """Never proxy an untrusted URL as a video download."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https" and not parsed.username and not parsed.password
        and parsed.port in (None, 443) and matches_domain(host, SNAPCHAT_CDN)
    )


class SnapchatPageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        parsed = urlsplit(target)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not matches_domain((parsed.hostname or "").lower(), SNAPCHAT)):
            raise ProviderError(422, "رابط سناب شات تحول إلى موقع غير موثوق.")
        return super().redirect_request(req, fp, code, msg, headers, target)


class SnapchatMediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not safe_snapchat_media(target):
            raise ProviderError(502, "مصدر فيديو سناب شات غير موثوق.")
        return super().redirect_request(req, fp, code, msg, headers, target)


def canonical_snapchat_url(url):
    """Resolve public /t/ shares to one explicit Spotlight video, never a profile/feed."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not matches_domain((parsed.hostname or "").lower(), SNAPCHAT)
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError()
    if re.fullmatch(r"/t/[A-Za-z0-9]{8}/?", parsed.path):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; VidKeep/1.0)"})
        try:
            with build_opener(SnapchatPageRedirect()).open(request, timeout=12) as response:
                url = response.geturl()
        except (HTTPError, URLError, TimeoutError):
            raise ProviderError(502, "تعذر فتح رابط مشاركة سناب شات. تأكد أنه عام ومتاح.") from None
        parsed = urlsplit(url)

    if not matches_domain((parsed.hostname or "").lower(), SNAPCHAT):
        raise ProviderError(422, "رابط سناب شات غير صالح.")
    match = re.fullmatch(r"/(?:@[^/]+/)?spotlight/([A-Za-z0-9_-]{20,128})/?", parsed.path)
    if not match:
        raise ProviderError(422, "هذا الرابط ليس لمقطع أضواء عام في سناب شات.")
    # Explicit /spotlight/ URL triggers yt-dlp's dedicated Spotlight extractor.
    return "https://www.snapchat.com/spotlight/" + match.group(1)


def canonical_youtube_url(url):
    """Only process a single video, never a user-controlled redirect or playlist."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not matches_domain(host, YOUTUBE):
        raise ValueError()
    parts = parsed.path.strip("/").split("/")
    if host == "youtu.be":
        video_id = parts[0]
    elif len(parts) == 2 and parts[0] in ("shorts", "embed", "live", "v"):
        video_id = parts[1]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        raise ValueError()
    if len(video_id) != 11 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in video_id):
        raise ValueError()
    return "https://www.youtube.com/watch?v=" + video_id


def configured_youtube_cookiefile(directory):
    """Reuse only the YouTube session explicitly configured on this server."""
    cookies = os.environ.get("YOUTUBE_COOKIES", "")
    if not cookies.strip():
        return None
    cookies = cookies.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cookie_path = os.path.join(directory, "youtube-cookies.txt")
    jar = MozillaCookieJar()
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix="session-", delete=False) as source:
            source.write(cookies.rstrip("\n") + "\n")
            source_path = source.name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            jar.load(source_path, ignore_discard=True, ignore_expires=True)
        youtube_jar = MozillaCookieJar(cookie_path)
        for cookie in jar:
            if (matches_domain(cookie.domain.lstrip(".").lower(), YOUTUBE)
                    and (cookie.expires in (None, 0) or cookie.expires > time.time())):
                youtube_jar.set_cookie(cookie)
        if not len(youtube_jar):
            raise ValueError()
        youtube_jar.save(ignore_discard=True, ignore_expires=False)
        os.chmod(cookie_path, 0o600)
    except Exception:
        raise ProviderError(503, "جلسة يوتيوب المحفوظة غير صالحة أو منتهية. يلزم تجديدها في إعدادات الخادم.") from None
    finally:
        if "source_path" in locals():
            os.unlink(source_path)
    return cookie_path


def native_youtube_worker(url, directory):
    """Our downloader: no hosted download API, credentials or paid fallback."""
    import imageio_ffmpeg

    class SafeLogger:
        def debug(self, message):
            pass

        def warning(self, message):
            pass

        def error(self, message):
            pass

    node_path = shutil.which("node")
    if not node_path:
        import nodejs_wheel
        node_path = os.path.join(os.path.dirname(nodejs_wheel.__file__), "bin", "node")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # yt-dlp discovers ffmpeg by its conventional name within this directory.
    os.symlink(ffmpeg, os.path.join(directory, "ffmpeg"))
    limit = 256 * 1024 * 1024

    def limit_disk(progress):
        total = sum(entry.stat().st_size for entry in os.scandir(directory)
                    if entry.is_file(follow_symlinks=False))
        if total > limit * 2 or (progress.get("total_bytes") or 0) > limit:
            raise ProviderError(413, "حجم الفيديو يتجاوز سعة المعالجة الحالية.")

    opts = {
        "quiet": True, "logger": SafeLogger(), "no_warnings": True,
        "noplaylist": True, "playlistend": 1, "cachedir": False,
        "socket_timeout": 10, "retries": 0, "extractor_retries": 0,
        # Use the operating system's trusted CA store; TLS checks stay enabled.
        "compat_opts": {"no-certifi"},
        "fragment_retries": 0, "concurrent_fragment_downloads": 2,
        "js_runtimes": {"node": {"path": node_path}},
        "ffmpeg_location": directory,
        "format": "bv*+ba/b", "format_sort": ["res", "fps", "vcodec:h264"],
        "merge_output_format": "mp4", "max_filesize": limit,
        "outtmpl": os.path.join(directory, "video.%(ext)s"),
        "match_filter": lambda info, **kwargs: "البث المباشر غير مدعوم" if info.get("is_live") else None,
        "progress_hooks": [limit_disk],
        "postprocessor_args": {"Merger+ffmpeg_o": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]},
    }
    cookie_path = configured_youtube_cookiefile(directory)
    if cookie_path:
        opts["cookiefile"] = cookie_path
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(canonical_youtube_url(url), download=True)
    if not info or info.get("is_live"):
        raise ProviderError(422, "البث المباشر غير مدعوم في محرك التنزيل.")
    path = os.path.join(directory, "video.mp4")
    if not os.path.isfile(path) or not 0 < os.path.getsize(path) <= limit:
        raise ProviderError(422, "لم ينتج محرك التنزيل ملف MP4 صالحًا ضمن سعة المعالجة.")
    return {"file": "video.mp4", "height": info.get("height"), "width": info.get("width")}


def run_native_youtube(url, directory):
    canonical = canonical_youtube_url(url)
    # Child interpreters must inherit Vercel runtime dependency paths.
    worker_env = os.environ.copy()
    worker_env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys(os.path.abspath(path or os.getcwd()) for path in sys.path)
    )
    process = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--native-worker", canonical, directory],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        start_new_session=True,
        env=worker_env,
    )
    try:
        output, _ = process.communicate(timeout=35)
    except subprocess.TimeoutExpired:
        # Kill ffmpeg children too, then let TemporaryDirectory remove partials.
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ProviderError(504, "انتهت مهلة محرك التنزيل الخاص أثناء تجهيز الفيديو.") from None
    try:
        result = json.loads(output)
    except ValueError:
        raise ProviderError(502, "تعذر تشغيل محرك التنزيل الخاص.") from None
    if process.returncode or "error" in result:
        raise ProviderError(result.get("status", 502), result.get("error", "تعذر تنزيل الفيديو."))
    path = os.path.join(directory, "video.mp4")
    if result.get("file") != "video.mp4" or not os.path.isfile(path):
        raise ProviderError(502, "لم ينتج محرك التنزيل ملفًا صالحًا.")
    return path, result


def extract_youtube_provider(url, download_id=None, asynchronous=False):
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
            with urlopen(req, timeout=min(25 if asynchronous else 12, remaining)) as response:
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

    if download_id is not None:
        job_id = str(download_id)
        if not job_id or len(job_id) > 128 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in job_id):
            raise ValueError()
        job = request(base + "/api/v1/download/" + job_id)
    else:
        job = request(base + "/api/v1/download?wait=false", {
            "url": canonical_youtube_url(url), "format": "video", "quality": "best", "outputFormat": "mp4"
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
        if asynchronous:
            # Resume this provider job in a later short request.
            return {"status": status, "downloadId": job_id, "retryAfter": 3}
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

    def send_native_video(self, path, metadata):
        with open(path, "rb") as video:
            self.send_response(200)
            self.cors_headers()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.fstat(video.fileno()).st_size))
            self.send_header("Content-Disposition", 'attachment; filename="VidKeep-YouTube.mp4"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-VidKeep-Engine", "native")
            self.send_header("X-Video-Height", str(metadata.get("height") or 0))
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Disposition, X-VidKeep-Engine, X-Video-Height")
            self.end_headers()
            try:
                shutil.copyfileobj(video, self.wfile, length=65536)
            except OSError:
                self.close_connection = True

    def send_snapchat_story_merged(self, original_url):
        """Losslessly concatenate public, compatible Story video streams into MP4.

        We do not reconstruct or remove burned-in watermarks. If the source
        videos have incompatible dimensions/codecs, fail rather than silently
        transcoding and reducing the requested source quality.
        """
        import imageio_ffmpeg

        try:
            items = snapchat_story_items_for_url(original_url)
        except ValueError:
            raise ProviderError(502, "تعذر فتح الستوري العام من سناب شات.") from None
        if not items:
            raise ProviderError(422, "لا توجد مقاطع ستوري عامة متاحة للدمج في هذا الرابط.")
        if len(items) == 1:
            self.send_snapchat_video(items[0]["url"], {})
            return
        # Avoid reporting a partial story as the whole story.
        if len(items) > 40:
            raise ProviderError(413, "عدد مقاطع الستوري يتجاوز سعة الدمج الحالية (40 مقطعًا).")

        total_limit = 180 * 1024 * 1024
        max_single = 85 * 1024 * 1024
        started = time.monotonic()
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="vidkeep-snap-") as directory:
            paths, total = [], 0
            fingerprints = []
            for index, item in enumerate(items):
                media_url = item.get("url", "")
                if not safe_snapchat_media(media_url):
                    raise ProviderError(422, "أحد روابط ملفات الستوري غير موثوق.")
                if time.monotonic() - started > 85:
                    raise ProviderError(504, "استغرق جلب الستوري أكثر من المهلة المتاحة.")
                request = Request(media_url, headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.snapchat.com/",
                    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.5",
                })
                try:
                    upstream = build_opener(SnapchatMediaRedirect()).open(request, timeout=12)
                    with upstream:
                        content_type = upstream.headers.get("Content-Type", "").lower()
                        if not (content_type.startswith("video/")
                                or content_type.startswith("application/octet-stream")):
                            raise ProviderError(422, "أحد مقاطع الستوري ليس فيديو MP4.")
                        path = os.path.join(directory, "snap-%03d.mp4" % index)
                        length = 0
                        with open(path, "wb") as file:
                            while True:
                                chunk = upstream.read(64 * 1024)
                                if not chunk:
                                    break
                                length += len(chunk)
                                total += len(chunk)
                                if length > max_single or total > total_limit:
                                    raise ProviderError(413, "الستوري يتجاوز سعة الدمج على الخادم (180 ميجابايت).")
                                file.write(chunk)
                        if length == 0:
                            raise ProviderError(502, "تعذر استلام أحد مقاطع الستوري.")
                except (HTTPError, URLError, TimeoutError, OSError):
                    raise ProviderError(502, "فشل جلب أحد مقاطع الستوري من المصدر.") from None

                try:
                    probe = subprocess.run(
                        [ffmpeg, "-hide_banner", "-i", path],
                        capture_output=True, text=True, timeout=10,
                    ).stderr
                except (OSError, subprocess.TimeoutExpired):
                    raise ProviderError(502, "تعذر فحص توافق صيغ فيديوهات الستوري.") from None
                video = re.search(
                    r"Video:\s*([^,\s]+)[^\r\n]*?(\d{2,5}x\d{2,5})",
                    probe, re.I
                )
                audio = re.search(r"Audio:\s*([^,\s]+)", probe, re.I)
                if not video:
                    raise ProviderError(422, "إحدى لقطات الستوري ليست فيديو مدعومًا.")
                fingerprints.append(
                    (video.group(1).lower(), video.group(2),
                     audio.group(1).lower() if audio else None)
                )
                paths.append(path)

            if len(set(fingerprints)) > 1:
                raise ProviderError(
                    422,
                    "تختلف صيغ أو أبعاد بعض مقاطع الستوري، "
                    "ولا يمكن دمجها دون إعادة ترميز قد تقلل الجودة."
                )

            concat_path = os.path.join(directory, "inputs.txt")
            with open(concat_path, "w", encoding="utf-8") as file:
                for path in paths:
                    # Every path is constructed in our own temporary directory.
                    file.write("file '" + path + "'\n")
            output_path = os.path.join(directory, "story.mp4")
            try:
                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", concat_path,
                     "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                     "-movflags", "+faststart", "-y", output_path],
                    capture_output=True, timeout=50,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ProviderError(504, "تجاوز دمج فيديوهات الستوري المهلة المتاحة.") from None
            if result.returncode != 0 or not os.path.isfile(output_path):
                raise ProviderError(502, "تعذر دمج ملفات الستوري دون إعادة ترميز.")

            size = os.path.getsize(output_path)
            if size < 1024 or size > 220 * 1024 * 1024:
                raise ProviderError(413, "حجم ملف الستوري المدمج خارج السعة المتاحة.")
            with open(output_path, "rb") as merged:
                self.send_response(200)
                self.cors_headers()
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition",
                                 'attachment; filename="VidKeep-Snapchat-Story-%d.mp4"' % len(items))
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Expose-Headers",
                                 "Content-Length, Content-Disposition")
                self.end_headers()
                try:
                    shutil.copyfileobj(merged, self.wfile, length=65536)
                except (OSError, TimeoutError):
                    self.close_connection = True


    def send_snapchat_video(self, direct, http_headers):
        """Send the original MP4 as an attachment rather than opening a player."""
        if not safe_snapchat_media(direct):
            raise ProviderError(502, "لم يرجع سناب شات رابط فيديو من مصدر موثوق.")
        headers = {
            "User-Agent": http_headers.get("User-Agent") or "Mozilla/5.0",
            "Referer": "https://www.snapchat.com/",
        }
        request = Request(direct, headers=headers)
        try:
            upstream = build_opener(SnapchatMediaRedirect()).open(request, timeout=25)
        except (HTTPError, URLError, TimeoutError):
            raise ProviderError(502, "تعذر جلب ملف سناب شات من المصدر.") from None
        with upstream:
            content_type = upstream.headers.get("Content-Type", "video/mp4")
            if not (content_type.startswith("video/") or content_type.startswith("application/octet-stream")):
                raise ProviderError(502, "المصدر لم يرجع فيديو قابلاً للتنزيل.")
            self.send_response(200)
            self.cors_headers()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", 'attachment; filename="VidKeep-Snapchat.mp4"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Disposition")
            length = upstream.headers.get("Content-Length", "")
            if length.isdigit():
                self.send_header("Content-Length", length)
            self.end_headers()
            try:
                shutil.copyfileobj(upstream, self.wfile, length=65536)
            except (OSError, TimeoutError):
                self.close_connection = True

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
            is_snapchat = matches_domain(host, SNAPCHAT)
            original_url = url

            if is_snapchat and payload.get("action") == "snapchat_story_info":
                try:
                    items = snapchat_story_items_for_url(url)
                except ValueError:
                    raise ProviderError(502, "تعذر قراءة الستوري العام من سناب شات.") from None
                self.respond(200, {
                    "type": "snapchat_story",
                    "count": len(items),
                    "items": items,
                    "can_merge": len(items) > 1,
                })
                return

            if is_snapchat and payload.get("action") == "snapchat_merge":
                self.send_snapchat_story_merged(url)
                return

            if is_snapchat and payload.get("action") == "snapchat_share":
                # Explicit discovery action for multi-snap public Story shares.
                # The normal download route still returns a single video.
                items = public_story_items_from_share(url)
                self.respond(200, {
                    "type": "snapchat_collection",
                    "items": items,
                    "count": len(items),
                    "scope": "public-shared-story",
                    "username": "share",
                })
                return

            if is_snapchat and payload.get("action") == "snapchat_profile":
                if profile_username(url) is None:
                    raise ProviderError(422, "استخدم رابط حساب سناب عام، مثل snapchat.com/add/username.")
                try:
                    listing = list_public_profile(url)
                except ValueError:
                    raise ProviderError(502, "تعذر قراءة مقاطع الحساب العامة حاليًا. حاول لاحقًا.") from None
                self.respond(200, {"type": "snapchat_collection", **listing})
                return

            if is_snapchat and payload.get("action") == "snapchat_asset":
                media = str(payload.get("media", ""))
                if not safe_media_url(media):
                    raise ProviderError(422, "عنوان ملف سناب غير صالح.")
                self.send_snapchat_video(media, {})
                return

            if is_snapchat:
                try:
                    url = canonical_snapchat_url(url)
                except ProviderError as original_error:
                    # A /t/ share can point to a public story rather than Spotlight.
                    # Use only the explicitly shared story when it has one video.
                    media = single_public_story_from_share(original_url)
                    if not media:
                        raise ProviderError(
                            422,
                            "تعذر استخراج فيديو الستوري من الرابط العام. "
                            "قد تكون الستوري منتهية أو متعددة المقاطع. افتح رابطها كقائمة فيديوهات."
                        ) from None
                    if payload.get("input") == original_url:
                        self.send_snapchat_video(media, {})
                    else:
                        self.respond(200, {"url": media})
                    return

            if is_youtube:
                # Test explicitly before enabling the native engine site-wide.
                if payload.get("engine") == "native" or os.environ.get("YOUTUBE_ENGINE") == "native":
                    with tempfile.TemporaryDirectory(prefix="vidkeep-native-") as directory:
                        path, metadata = run_native_youtube(url, directory)
                        self.send_native_video(path, metadata)
                    return
                direct = extract_youtube_provider(
                    url, download_id=payload.get("downloadId"),
                    asynchronous=payload.get("async") is True,
                )
                if isinstance(direct, dict):
                    self.respond(202, direct)
                    return
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
            if is_snapchat and not direct:
                direct = next(select_video(info), {}).get("url") or ""

            if not direct.startswith("https://"):
                self.respond(400, {
                    "error": (
                        "لم يتوفر ملف مباشر يجمع "
                        "الفيديو والصوت."
                    )
                })
                return

            if is_snapchat:
                if not safe_snapchat_media(direct):
                    raise ProviderError(502, "لم يرجع سناب شات رابط MP4 موثوقًا.")
                http_headers = info.get("http_headers") or {}
                safe_headers = {
                    "User-Agent": str(http_headers.get("User-Agent") or "Mozilla/5.0"),
                    "Referer": "https://www.snapchat.com/",
                }
                if payload.get("input") == original_url or payload.get("download") is True:
                    self.send_snapchat_video(direct, safe_headers)
                else:
                    self.respond(200, {"url": direct, "headers": safe_headers})
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
            # Never leak traceback, file paths, private URL parameters or
            # credentials to the visitor. Keep the exception category in logs.
            print("VidKeep server error:", type(exc).__name__, flush=True)
            self.respond(502 if "is_snapchat" in locals() and is_snapchat else 500, {
                "error": (
                    "تعذر معالجة رابط سناب من الخادم. تحقق أن الستوري عام ومتاح وحاول مجددًا."
                    if "is_snapchat" in locals() and is_snapchat
                    else "حدث خطأ في خدمة التحميل."
                )
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


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--native-worker":
    try:
        print(json.dumps(native_youtube_worker(sys.argv[2], sys.argv[3])))
    except ProviderError as exc:
        print(json.dumps({"status": exc.status, "error": str(exc)}))
        sys.exit(1)
    except yt_dlp.utils.DownloadError as exc:
        detail = str(exc).lower()
        blocked = "not a bot" in detail or "sign in to confirm" in detail
        print(json.dumps({"status": 503 if blocked else 422, "error": (
            "يوتيوب رفض طلب محرك التنزيل من الخادم وطلب التحقق."
            if blocked else "تعذر تنزيل هذا الفيديو عبر المحرك الخاص."
        )}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"status": 502, "error": "تعذر تشغيل أحد مكونات محرك التنزيل."}))
        sys.exit(1)
