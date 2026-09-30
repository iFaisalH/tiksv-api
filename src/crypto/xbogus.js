import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";

const sdk = fs.readFileSync(new URL("../vendor/webmssdk.js", import.meta.url), "utf8");
let acrawler;

function getAcrawler() {
  if (acrawler) return acrawler;
  const noop = () => {};
  const store = () => ({ getItem: () => null, setItem: noop, removeItem: noop });
  const env = {
    console,
    Uint8Array,
    Uint16Array,
    Int32Array,
    ArrayBuffer,
    DataView,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    performance: { now: () => Date.now() },
    navigator: {
      userAgent:
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      platform: "MacIntel",
      cookieEnabled: true,
    },
    screen: { width: 1920, height: 1080 },
    location: { href: "https://www.tiktok.com/", hostname: "www.tiktok.com" },
    localStorage: store(),
    sessionStorage: store(),
    document: {
      cookie: "",
      readyState: "complete",
      addEventListener: noop,
      removeEventListener: noop,
      dispatchEvent: noop,
      createElement: () => ({ style: {}, appendChild: noop, setAttribute: noop, getContext: () => null }),
    },
    Event: class {
      constructor(type) {
        this.type = type;
      }
    },
    XMLHttpRequest: class {
      open() {}
      send() {}
      setRequestHeader() {}
    },
    fetch: async () => ({ ok: true, json: async () => ({}), text: async () => "" }),
    Request: class {},
    Headers: class {},
    Symbol,
    Object,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Date,
    Math,
    JSON,
    Promise,
    Error,
    encodeURIComponent,
    decodeURIComponent,
    _mssdk: { _enablePathListRegex: [/.*/], cacheOpts: { 1988: { apiHost: "https://www.tiktok.com" } } },
  };
  env.self = env;
  env.window = env;
  env.globalThis = env;
  vm.runInNewContext(sdk, env, { timeout: 20000 });
  acrawler = env.byted_acrawler;
  return acrawler;
}

export function signUrl(url) {
  const bogus = getAcrawler().frontierSign({ url })["X-Bogus"];
  return `${url}${url.includes("?") ? "&" : "?"}X-Bogus=${encodeURIComponent(bogus)}`;
}
