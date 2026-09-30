# tik-tok-downloader

A small Node.js server that downloads TikTok videos without third-party packages. It accepts a share link or numeric video ID over HTTP, talks to TikTok the same way a browser session would—establishing cookies, passing the homepage WAF challenge, signing requests with `X-Bogus`—then reads video metadata from the page payload, picks the highest-quality H.264 stream, and returns the file as MP4.

The implementation is plain ES modules on Node.js 20.11 or newer, using only built-in APIs (`http`, `fetch`, `vm`, `crypto`). There is no `package.json` dependency tree beyond the runtime itself.

```bash
npm start
```

Default listen address: `http://localhost:3000`. Set `PORT` to change the port.

---

## Architecture

The server splits into three layers: an HTTP surface (`index.js`, `http.js`), a single download orchestrator (`handlers.js`), and TikTok-facing modules grouped by concern—input parsing, session bootstrap, anti-bot crypto, metadata fetch, and stream selection.

```
                    ┌─────────────────────────────────────────┐
                    │              HTTP layer                 │
                    │  index.js ──► http.js (body, static)    │
                    └────────────────────┬────────────────────┘
                                         │ POST /api/download
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │           handlers.js                   │
                    │     (session cache, orchestration)      │
                    └────────────────────┬────────────────────┘
           ┌─────────────────────────────┼─────────────────────────────┐
           ▼                             ▼                             ▼
   parse/link.js              tiktok/session.js              tiktok/video.js
   (link → videoId)            (cookies, WAF, MSSDK)          (CDN URL pick)
                                       │
                         ┌─────────────┴─────────────┐
                         ▼                           ▼
                  crypto/waf.js              crypto/xbogus.js
                  (challenge solve)          (vendor webmssdk.js)
                         │
                         ▼
                  tiktok/item.js
                  (page / oEmbed / CDN bytes)
```

**External calls:** `www.tiktok.com` (home, WAF, video pages, oEmbed), `mssdk-sg.tiktok.com/web/common` (signed POST), CDN hosts from `itemStruct.video`.

**Download request path**

1. Client sends `{"input"}` to `/api/download`.
2. `parseLink` normalizes the string to a video ID and/or page URL; short links are resolved first.
3. A process-wide session is created once (`createSession`): homepage cookies, optional WAF proof, MSSDK handshake with `X-Bogus`.
4. `itemStruct` is loaded from the video page rehydration JSON (direct page URL or oEmbed → page).
5. `pickStreamUrl` chooses the best H.264 URL; `downloadStream` pulls bytes from the CDN and the handler responds with MP4. Failed downloads clear the cached session.

### Layout

```
tik-tok-downloader/
├── src/
│   ├── index.js              HTTP server and routing
│   ├── http.js               Request body, JSON, static files
│   ├── handlers.js           /api/download orchestration
│   ├── parse/link.js         Input → videoId / pageUrl
│   ├── tiktok/
│   │   ├── session.js        Cookie jar and session bootstrap
│   │   ├── item.js           Page fetch, oEmbed, CDN download
│   │   └── video.js          Stream URL selection
│   ├── crypto/
│   │   ├── waf.js            Homepage WAF challenge
│   │   └── xbogus.js         MSSDK VM and URL signing
│   └── vendor/webmssdk.js    TikTok signing SDK (bundled)
└── public/                   Static UI (calls /api/download)
```

---

## HTTP server

`src/index.js` — `node:http` on `Number(process.env.PORT) || 3000`.

| Condition | Handler |
|-----------|---------|
| `POST` + pathname `/api/download` | `readBody(req)` → `handleDownload` |
| `GET` + pathname not starting `/api` | `staticFile(res, publicDir, pathname)` |
| else | `404` `{"error":"not found"}` |
| uncaught in route `try` | `500` `{"error":"<message>"}` |

`src/http.js`:

- **`readBody(req, limit)`** — default limit **1_048_576** bytes; overrun → `Error("body too large")` and `req.destroy()`.
- **`json` / `send`** — write response and end.
- **`staticFile(res, root, urlPath)`** — `/` maps to `/index.html`; resolves under `root` with prefix check (`403` plain `Forbidden` on escape); MIME from extension (`.html`, `.css`, `.js`, else `application/octet-stream`); `ENOENT` → `false` (caller returns 404).

### `POST /api/download`

| | |
|---|---|
| Body | `{"input":"<string>"}` — `Content-Type: application/json` |
| `200` | MP4 buffer |
| `200` headers | `Content-Type: video/mp4`, `Content-Disposition: attachment; filename="<author.uniqueId\|tiktok>_<item.id>.mp4"`, `Content-Length` |
| `400` | `{"error":"<message>"}` — all failures inside `handleDownload` |

`public/assets/app.js` posts the same endpoint and saves the blob client-side.

---

## Download handler

`src/handlers.js` implements the path described above. Module-level `let session` is lazy-initialized and reused; any caught error sets `session = null` before returning `400`.

Filename: `attachment; filename="${item.author?.uniqueId ?? "tiktok"}_${item.id}.mp4"`.

---

## Link parsing

`src/parse/link.js`

**Constants**

| Name | Pattern |
|------|---------|
| `ID` | `(\d{15,25})` |
| `HOST` | optional scheme; subdomains `www`/`m`/`vm`/`vt`/`v`; `tiktok.com` or `*.tiktok.com` |
| `VIDEO` | `/(?:@[\w.-]+/)?video/(\d{15,25})` |
| `PHOTO` | `/(?:@[\w.-]+/)?photo/(\d{15,25})` |

**`parseLink(raw)`** — trim; throws `empty input` if blank.

1. Whole string matches `^\d{15,25}$` → `{ videoId, pageUrl: null, needsRedirect: false }`
2. Prepend `https://` when no scheme
3. `HOST` fails → first `ID` match in raw string, or throw `not a TikTok link or video id`
4. `PHOTO` match → `{ videoId, pageUrl: url, needsRedirect: false }`
5. `VIDEO` match → same
6. `\/(\d{15,25})(?:\?|$|\/)` → same
7. else → `{ videoId: null, pageUrl: url, needsRedirect: true }`

**`videoIdFromUrl(url)`** — `PHOTO` capture, else `VIDEO`, else `\/(\d{15,25})`, else `null`.

---

## Session

`src/tiktok/session.js`

**`USER_AGENT`** — fixed Chrome 131 / macOS string (also used in `xbogus.js` navigator stub).

**`mergeCookies(jar, setCookie[])`** — split existing `jar` on `;`, parse `name=value`; overlay each `Set-Cookie` header’s first segment; later keys win; rejoin `; `.

**`htmlHeaders(session)`** — `{ User-Agent, Accept: "text/html", Cookie: session.cookie }`.

**`createSession()`** sequence:

1. `GET https://www.tiktok.com/` — `User-Agent`, `Accept: text/html` — merge cookies
2. `readChallenge(html)` on body — if present:
   - `challengeCookies(fields, solveChallenge(fields.cs))` merged in
   - `GET` home again with `htmlHeaders` — merge cookies
3. `POST https://mssdk-sg.tiktok.com/web/common` — URL from `signUrl(MSSDK)`; headers `User-Agent`, `Content-Type: application/json`, `Cookie`; body `"{}"` — merge cookies
4. Return `{ cookie }`

---

## WAF challenge

`src/crypto/waf.js`

**`readChallenge(html)`** — regex on elements `id="cs"|"wci"|"rci"|"rs"|"rs_id"` with `class="…"` values. Requires `cs` and `wci`; else `null`. Returns `{ cs, wci, rci?, rs?, rsId? }`.

**`solveChallenge(csB64)`** — decode `cs` JSON; `prefix = base64(c.v.a)`; `expect = hex(base64(c.v.c))`; loop `i` from `0` to `1_000_000`: if `sha256(prefix ‖ String(i)) === expect`, set `c.d = base64(String(i))`, return base64(JSON.stringify(c)); else throw `waf unsolved`.

**`challengeCookies(fields, token)`** — `${wci}=${token}`; if `rs`: `${rci}=${rs}`; if `rsId`: `waforigin_id=${rsId}`.

---

## X-Bogus signing

`src/crypto/xbogus.js`

Reads `src/vendor/webmssdk.js` synchronously. **`getAcrawler()`** — singleton: builds `node:vm` context with browser stubs (`navigator`, `document`, `localStorage`/`sessionStorage` no-ops, stub `fetch`/`XMLHttpRequest`, `atob`/`btoa`, `_mssdk` config with `apiHost: https://www.tiktok.com`), runs SDK (`timeout: 20000`), caches `env.byted_acrawler`.

**`signUrl(url)`** — `bogus = acrawler.frontierSign({ url })["X-Bogus"]`; append `?` or `&` + `X-Bogus=` + `encodeURIComponent(bogus)`.

---

## Item resolution

`src/tiktok/item.js`

**`readItemFromPage(html)`**

- Match `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">…</script>`
- `JSON.parse` → `__DEFAULT_SCOPE__["webapp.video-detail"]`
- If `statusCode` set and ≠ `0` → throw `statusMsg` or `video unavailable`
- Return `itemInfo.itemStruct` or `null`

**`followRedirect(session, url)`** — `fetch` with `htmlHeaders`, `redirect: "follow"` → `res.url`.

**`fetchFromPage(session, pageUrl)`** — `fetch` with `htmlHeaders`, `redirect: "follow"`; `session.cookie = mergeCookies(..., getSetCookie)`; `readItemFromPage` on text — throw `video data not in page` if null.

**`fetchByVideoId(session, videoId)`**

- `GET https://www.tiktok.com/oembed?url=` + encoded `https://www.tiktok.com/video/${videoId}` — `User-Agent` only
- `!ok` → `oembed failed`
- Parse `cite="…"` from `html` field — missing → `oembed missing page url`
- `fetchFromPage(session, pageUrl)`

**`downloadStream(session, url)`** — `GET` with `User-Agent`, `Referer: https://www.tiktok.com/`, `Cookie: session.cookie`; `!ok` → `cdn ${status}`; return `Buffer` from `arrayBuffer()`.

---

## Stream selection

`src/tiktok/video.js` — **`pickStreamUrl(item)`**

1. No `item.video` → `null`
2. For each `video.bitrateInfo[]` entry: URL from `gear.PlayAddr.UrlList` — prefer first matching `/^https:\/\/v\d+-/` else `[0]`; collect `{ url, bitrate: gear.Bitrate ?? 0, codec: gear.CodecType ?? "" }`
3. If `video.playAddr`: push `{ url: playAddr, bitrate: video.bitrate ?? 0, codec: video.codecType ?? "h264" }`
4. `bestUrl` = sort by bitrate desc, take first URL
5. Return `bestUrl(h264 streams)` where codec matches `/h264/i`, else `bestUrl(all)`

---

## Errors

| Message | Where |
|---------|--------|
| `body too large` | `readBody` |
| `empty input` | `parseLink` |
| `not a TikTok link or video id` | `parseLink` |
| `could not resolve link` | `handlers` after redirect |
| `waf unsolved` | `solveChallenge` |
| `video unavailable` / `detail.statusMsg` | `readItemFromPage` |
| `video data not in page` | `fetchFromPage` |
| `oembed failed` | `fetchByVideoId` |
| `oembed missing page url` | `fetchByVideoId` |
| `no video url` | `handlers` |
| `cdn <status>` | `downloadStream` |
| route / parse failures in handler | `400` + session cleared |
| uncaught outside handler catch | `500` from `index.js` |
