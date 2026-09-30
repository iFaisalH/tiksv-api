import { htmlHeaders, mergeCookies, USER_AGENT } from "./session.js";

function readItemFromPage(html) {
  const blob = html.match(
    /<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)<\/script>/,
  )?.[1];
  if (!blob) return null;
  const detail = JSON.parse(blob).__DEFAULT_SCOPE__?.["webapp.video-detail"];
  if (!detail) return null;
  if (detail.statusCode != null && detail.statusCode !== 0) {
    throw new Error(detail.statusMsg || "video unavailable");
  }
  return detail.itemInfo?.itemStruct ?? null;
}

export async function followRedirect(session, url) {
  const res = await fetch(url, { headers: htmlHeaders(session), redirect: "follow" });
  return res.url;
}

export async function fetchFromPage(session, pageUrl) {
  const res = await fetch(pageUrl, { headers: htmlHeaders(session), redirect: "follow" });
  session.cookie = mergeCookies(session.cookie, res.headers.getSetCookie?.() ?? []);
  const item = readItemFromPage(await res.text());
  if (!item) throw new Error("video data not in page");
  return item;
}

export async function fetchByVideoId(session, videoId) {
  const oembed = await fetch(
    `https://www.tiktok.com/oembed?url=${encodeURIComponent(`https://www.tiktok.com/video/${videoId}`)}`,
    { headers: { "User-Agent": USER_AGENT } },
  );
  if (!oembed.ok) throw new Error("oembed failed");
  const pageUrl = (await oembed.json()).html?.match(/cite="([^"]+)"/)?.[1];
  if (!pageUrl) throw new Error("oembed missing page url");
  return fetchFromPage(session, pageUrl);
}

export async function downloadStream(session, url) {
  const res = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Referer: "https://www.tiktok.com/", Cookie: session.cookie },
  });
  if (!res.ok) throw new Error(`cdn ${res.status}`);
  return Buffer.from(await res.arrayBuffer());
}
