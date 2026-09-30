import { parseLink, videoIdFromUrl } from "./parse/link.js";
import { createSession } from "./tiktok/session.js";
import { fetchFromPage, fetchByVideoId, followRedirect, downloadStream } from "./tiktok/item.js";
import { pickStreamUrl } from "./tiktok/video.js";
import { json, send } from "./http.js";

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
    const file = await downloadStream(s, url);
    send(res, 200, {
      "Content-Type": "video/mp4",
      "Content-Disposition": `attachment; filename="${item.author?.uniqueId ?? "tiktok"}_${item.id}.mp4"`,
      "Content-Length": file.length,
    }, file);
  } catch (e) {
    session = null;
    json(res, 400, { error: e.message });
  }
}
