import { parseLink, videoIdFromUrl } from "./parse/link.js";
import { createSession } from "./tiktok/session.js";
import { fetchFromPage, fetchByVideoId, followRedirect, downloadStream } from "./tiktok/item.js";
import { pickStreamUrl } from "./tiktok/video.js";
import { json } from "./http.js";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

let session;

export async function handleDownload(res, body) {
  try {
    const { input } = JSON.parse(body.toString("utf8") || "{}");
    let { videoId, pageUrl, needsRedirect } = parseLink(input ?? "");
    const s = await (session ??= await createSession());
    if (needsRedirect && pageUrl) {
      pageUrl = await followRedirect(s, pageUrl);
      videoId = videoIdFromUrl(pageUrl);
      if (!videoId) throw new Error("could not resolve link");
    }
    const item = pageUrl ? await fetchFromPage(s, pageUrl) : await fetchByVideoId(s, videoId);
    const url = pickStreamUrl(item);
    if (!url) throw new Error("no video url");
    const upstream = await downloadStream(s, url);
    const safeAuthor = String(item.author?.uniqueId ?? "tiktok").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 60);
    const safeId = String(item.id ?? "video").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 60);
    const headers = {
      "Content-Type": "video/mp4",
      "Content-Disposition": `attachment; filename="${safeAuthor}_${safeId}.mp4"`,
      "Cache-Control": "no-store",
    };
    const contentLength = upstream.headers.get("content-length");
    if (contentLength && /^\d+$/.test(contentLength)) headers["Content-Length"] = contentLength;
    res.writeHead(200, headers);
    await pipeline(Readable.fromWeb(upstream.body), res);
  } catch (e) {
    session = null;
    if (res.headersSent) {
      res.destroy(e);
    } else {
      json(res, 400, { error: e.message });
    }
  }
}
