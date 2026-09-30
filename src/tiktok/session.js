import { readChallenge, solveChallenge, challengeCookies } from "../crypto/waf.js";
import { signUrl } from "../crypto/xbogus.js";

export const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const HOME = "https://www.tiktok.com/";
const MSSDK = "https://mssdk-sg.tiktok.com/web/common";

export function mergeCookies(jar, setCookie = []) {
  const map = new Map();
  for (const part of jar.split(";").map((s) => s.trim()).filter(Boolean)) {
    const i = part.indexOf("=");
    if (i > 0) map.set(part.slice(0, i), part.slice(i + 1));
  }
  for (const raw of setCookie) {
    const part = raw.split(";")[0];
    const i = part.indexOf("=");
    if (i > 0) map.set(part.slice(0, i), part.slice(i + 1));
  }
  return [...map].map(([k, v]) => `${k}=${v}`).join("; ");
}

export function htmlHeaders(session) {
  return { "User-Agent": USER_AGENT, Accept: "text/html", Cookie: session.cookie };
}

export async function createSession() {
  let cookie = "";
  const home = await fetch(HOME, { headers: { "User-Agent": USER_AGENT, Accept: "text/html" } });
  cookie = mergeCookies(cookie, home.headers.getSetCookie?.() ?? []);

  const challenge = readChallenge(await home.text());
  if (challenge) {
    cookie = mergeCookies(cookie, [challengeCookies(challenge, solveChallenge(challenge.cs))]);
    const pass = await fetch(HOME, { headers: htmlHeaders({ cookie }) });
    cookie = mergeCookies(cookie, pass.headers.getSetCookie?.() ?? []);
  }

  const ms = await fetch(signUrl(MSSDK), {
    method: "POST",
    headers: { "User-Agent": USER_AGENT, "Content-Type": "application/json", Cookie: cookie },
    body: "{}",
  });
  return { cookie: mergeCookies(cookie, ms.headers.getSetCookie?.() ?? []) };
}
