import fs from "node:fs/promises";
import path from "node:path";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

export function readBody(req, limit = 1_048_576) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

export function send(res, status, headers, body) {
  res.writeHead(status, headers);
  res.end(body);
}

export function json(res, status, data) {
  send(res, status, { "Content-Type": "application/json; charset=utf-8" }, JSON.stringify(data));
}

export async function staticFile(res, root, urlPath) {
  const rel = urlPath === "/" ? "/index.html" : urlPath;
  const file = path.resolve(root, rel.slice(1));
  if (!file.startsWith(path.resolve(root) + path.sep)) {
    send(res, 403, { "Content-Type": "text/plain" }, "Forbidden");
    return true;
  }
  try {
    const data = await fs.readFile(file);
    send(res, 200, { "Content-Type": TYPES[path.extname(file).toLowerCase()] ?? "application/octet-stream" }, data);
    return true;
  } catch (e) {
    if (e.code === "ENOENT") return false;
    throw e;
  }
}
