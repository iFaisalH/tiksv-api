import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { json } from "./http.js";

export async function handleMultiPlatform(res, input) {
  try {
    const url = /^https?:\/\//i.test(input) ? input : `https://${input}`;
    const hostname = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
    const hosts = ["youtube.com", "youtu.be", "youtube-nocookie.com", "instagram.com", "facebook.com", "fb.watch", "x.com", "twitter.com", "t.co", "pinterest.com", "pin.it", "reddit.com", "redd.it"];
    if (!hosts.some(h => hostname === h || hostname.endsWith(`.${h}`))) throw new Error("المنصة غير مدعومة");
    const base = process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : `http://localhost:${process.env.PORT || 3000}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    let extracted;
    try {
      const response = await fetch(`${base}/api/extract`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({url}), signal: controller.signal });
      extracted = await response.json();
      if (!response.ok) throw new Error(extracted.error || "تعذر استخراج الفيديو");
    } finally { clearTimeout(timer); }
    const headers = {};
    for (const name of ["User-Agent", "Referer", "Origin"]) {
      const val = extracted.headers?.[name] || extracted.headers?.[name.toLowerCase()];
      if (val) headers[name] = val;
    }
    const upstream = await fetch(extracted.url, { headers, redirect: "follow" });
    if (!upstream.ok || !upstream.body) throw new Error("فشل تحميل الفيديو من المصدر");
    const contentType = upstream.headers.get("content-type") || "video/mp4";
    if (!contentType.startsWith("video/") && !contentType.includes("octet-stream")) throw new Error("المصدر لم يرجع ملف فيديو");
    const fileName = `vidkeep_${hostname.replace(/[^a-z0-9_-]/g, "_")}.mp4`;
    const responseHeaders = {"Content-Type": contentType, "Content-Disposition": `attachment; filename="${fileName}"`, "Cache-Control": "no-store"};
    const length = upstream.headers.get("content-length");
    if (length && /^\d+$/.test(length)) responseHeaders["Content-Length"] = length;
    res.writeHead(200, responseHeaders);
    await pipeline(Readable.fromWeb(upstream.body), res);
  } catch (e) {
    if (res.headersSent) res.destroy(e);
    else json(res, 400, { error: e.message || "تعذر تنزيل الفيديو" });
  }
}
