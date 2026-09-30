function streamUrl(gear) {
  const list = gear?.PlayAddr?.UrlList;
  if (!list?.length) return null;
  return list.find((u) => /^https:\/\/v\d+-/.test(u)) ?? list[0];
}

function bestUrl(streams) {
  return streams.sort((a, b) => b.bitrate - a.bitrate)[0]?.url ?? null;
}

export function pickStreamUrl(item) {
  const video = item?.video;
  if (!video) return null;

  const streams = (video.bitrateInfo ?? [])
    .map((gear) => {
      const url = streamUrl(gear);
      return url ? { url, bitrate: gear.Bitrate ?? 0, codec: gear.CodecType ?? "" } : null;
    })
    .filter(Boolean);

  if (video.playAddr) streams.push({ url: video.playAddr, bitrate: video.bitrate ?? 0, codec: video.codecType ?? "h264" });

  return bestUrl(streams.filter((s) => /h264/i.test(s.codec))) ?? bestUrl(streams);
}
