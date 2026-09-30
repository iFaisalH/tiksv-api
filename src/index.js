import http from "node:http";
import path from "node:path";
import { readBody, staticFile, json } from "./http.js";
import { handleDownload } from "./handlers.js";

const publicDir = path.join(import.meta.dirname, "..", "public");
const port = Number(process.env.PORT) || 3000;

http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (req.method === "POST" && pathname === "/api/download") {
      await handleDownload(res, await readBody(req));
      return;
    }
    if (req.method === "GET" && !pathname.startsWith("/api") && (await staticFile(res, publicDir, pathname))) return;
    json(res, 404, { error: "not found" });
  } catch (e) {
    json(res, 500, { error: e.message });
  }
}).listen(port, () => console.log(`http://localhost:${port}`));
