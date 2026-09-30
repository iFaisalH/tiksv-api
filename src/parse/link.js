const ID = /(\d{15,25})/;
const HOST = /^(?:https?:\/\/)?(?:(?:www|m|vm|vt|v)\.)?(?:tiktok\.com|[\w-]+\.tiktok\.com)(?:\/|$)/i;
const VIDEO = /\/(?:@[\w.-]+\/)?video\/(\d{15,25})/i;
const PHOTO = /\/(?:@[\w.-]+\/)?photo\/(\d{15,25})/i;

export function parseLink(raw) {
  const input = raw.trim();
  if (!input) throw new Error("empty input");
  if (/^\d{15,25}$/.test(input)) return { videoId: input, pageUrl: null, needsRedirect: false };

  const url = /^https?:\/\//i.test(input) ? input : `https://${input}`;
  if (!HOST.test(url)) {
    const id = input.match(ID)?.[1];
    if (id) return { videoId: id, pageUrl: null, needsRedirect: false };
    throw new Error("not a TikTok link or video id");
  }

  const photo = url.match(PHOTO);
  if (photo) return { videoId: photo[1], pageUrl: url, needsRedirect: false };
  const video = url.match(VIDEO);
  if (video) return { videoId: video[1], pageUrl: url, needsRedirect: false };
  const bare = url.match(/\/(\d{15,25})(?:\?|$|\/)/);
  if (bare) return { videoId: bare[1], pageUrl: url, needsRedirect: false };
  return { videoId: null, pageUrl: url, needsRedirect: true };
}

export function videoIdFromUrl(url) {
  return url.match(PHOTO)?.[1] ?? url.match(VIDEO)?.[1] ?? url.match(/\/(\d{15,25})/)?.[1] ?? null;
}
