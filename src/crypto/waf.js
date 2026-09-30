import { createHash } from "crypto";

function sha256(parts) {
  const h = createHash("sha256");
  for (const p of parts) h.update(typeof p === "string" ? p : p);
  return h.digest("hex");
}

export function solveChallenge(csB64) {
  const c = JSON.parse(Buffer.from(csB64, "base64").toString("utf8"));
  const prefix = Buffer.from(c.v.a, "base64");
  const expect = Buffer.from(c.v.c, "base64").toString("hex");
  for (let i = 0; i <= 1_000_000; i++) {
    if (sha256([prefix, String(i)]) === expect) {
      c.d = Buffer.from(String(i)).toString("base64");
      return Buffer.from(JSON.stringify(c)).toString("base64");
    }
  }
  throw new Error("waf unsolved");
}

export function readChallenge(html) {
  const cs = html.match(/id="cs"\s+class="([^"]+)"/)?.[1];
  const wci = html.match(/id="wci"\s+class="([^"]+)"/)?.[1];
  if (!cs || !wci) return null;
  return {
    cs,
    wci,
    rci: html.match(/id="rci"\s+class="([^"]+)"/)?.[1],
    rs: html.match(/id="rs"\s+class="([^"]*)"/)?.[1],
    rsId: html.match(/id="rs_id"\s+class="([^"]*)"/)?.[1],
  };
}

export function challengeCookies(fields, token) {
  const out = [`${fields.wci}=${token}`];
  if (fields.rs) out.push(`${fields.rci}=${fields.rs}`);
  if (fields.rsId) out.push(`waforigin_id=${fields.rsId}`);
  return out.join("; ");
}
