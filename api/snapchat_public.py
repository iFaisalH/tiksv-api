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
        return match.group(1) if match else None
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
    request = Request(page_url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
    })
    with build_opener(OnlySnapchatRedirects()).open(request, timeout=12) as response:
        markup = response.read(3_000_001)
        if len(markup) > 3_000_000:
            raise ValueError("Snapchat page is larger than supported")
        markup = markup.decode("utf-8", "replace")
    # Snapchat's public profile currently exposes Next.js page props, but
    # don't assume the __NEXT_DATA__ id will always be present.
    pattern = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
    for match in pattern.finditer(markup):
        attributes, body = match.group(1), match.group(2)
        if not (re.search(r"type\s*=\s*['\"]application/json['\"]", attributes, re.I)
                or "__NEXT_DATA__" in attributes):
            continue
        try:
            data = json.loads(html.unescape(body))
        except (ValueError, TypeError):
            continue
        props = data.get("props", {}).get("pageProps") if isinstance(data, dict) else None
        if isinstance(props, dict):
            return props
    return {}


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
    ]
    for candidate in choices:
        candidate = _unwrap(candidate)
        if isinstance(candidate, str) and safe_media_url(candidate):
            return candidate
    return None


def _video(snap, kind):
    if not isinstance(snap, dict):
        return False
    media_type = str(_unwrap(snap.get("mediaType")) or "").lower()
    if "image" in media_type or "photo" in media_type or media_type in ("0", "image"):
        return False
    # Spotlight entries describe videos, while stories may have photo snaps.
    if kind == "spotlight":
        return True
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
        identity = (kind, sid or source)
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

    # A profile can expose a partial set rather than a full historical archive.
    return {"username": username, "items": items[:200], "count": min(200, len(items)),
            "truncated": len(items) > 200, "scope": "public-page-visible"}


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
