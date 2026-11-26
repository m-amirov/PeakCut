"""Twitch ads are left out of recordings: pieces labelled as ads (by title or by an ad date range) never reach ffmpeg,
the pieces that do are numbered without gaps, a new stretch is marked after an ad, other sites are not touched,
and when the relay can't run the recording reads Twitch directly."""
import sys
sys.path.insert(0, ".")
from studio import adskip   # noqa: E402

fails = []


def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n + (f"  {x}" if x and not ok else "")); ok or fails.append(n)


UP = "https://video-weaver.fra05.hls.ttvnw.net/v1/playlist/abc.m3u8"


def pl(seq, segs, extra=""):
    out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{seq}", extra]
    for i, (name, title, t) in enumerate(segs):
        out += [f"#EXT-X-PROGRAM-DATE-TIME:2026-11-23T20:00:{t:02d}.000Z", f"#EXTINF:2.000,{title}", f"https://x.hls.ttvnw.net/v1/segment/{name}.ts"]
    return "\n".join(x for x in out if x) + "\n"


check("only Twitch streams go through the relay", adskip.url(UP).startswith("http://127.0.0.1:") and adskip.url("https://kick.com/x.m3u8") == "https://kick.com/x.m3u8")

# an ad by its title
out = adskip.relay(UP, pl(100, [("a", "live", 0), ("b", "live", 2), ("ad1", "Amazon|123", 4), ("ad2", "Amazon|124", 6)]))
check("ad pieces (by title) are left out", "ad1" not in out and "ad2" not in out and "/a.ts" in out and "/b.ts" in out, out)
out = adskip.relay(UP, pl(102, [("ad1", "Amazon|123", 4), ("ad2", "Amazon|124", 6), ("c", "live", 8), ("d", "live", 10)]))
segs = [l for l in out.splitlines() if l.startswith("https://")]
check("pieces are numbered with no gap", "#EXT-X-MEDIA-SEQUENCE:0" in out and segs[-2:] == ["https://x.hls.ttvnw.net/v1/segment/c.ts", "https://x.hls.ttvnw.net/v1/segment/d.ts"], out)
lines = out.splitlines()
check("a new stretch starts after an ad", lines[lines.index("https://x.hls.ttvnw.net/v1/segment/c.ts") - 3] == "#EXT-X-DISCONTINUITY", out)
check("the seconds of ads skipped are counted", adskip.skipped[UP] == 4.0, str(adskip.skipped[UP]))
out2 = adskip.relay(UP, pl(104, [("c", "live", 8), ("d", "live", 10), ("e", "live", 12)]))
check("a piece already handed on keeps its number", out2.count("/c.ts") == 1 and "/e.ts" in out2)

# an ad by its date range
UP2 = UP.replace("abc", "def")
dr = '#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-11-23T20:00:04.000Z",DURATION=4.0'
out = adskip.relay(UP2, pl(1, [("p", "live", 2), ("q", "live", 4), ("r", "live", 6), ("s", "live", 8)], dr))
check("ad pieces (by date range) are left out", "/q.ts" not in out and "/r.ts" not in out and "/p.ts" in out and "/s.ts" in out, out)

# the relay can't run: the recording reads Twitch directly
cmd = ["ffmpeg", "-i", adskip.url(UP), "-c", "copy", "x.ts"]
adskip.ensure = lambda: False
check("without the relay, ffmpeg reads Twitch directly", adskip.prepare(cmd)[2] == UP)
check("a command without Twitch is left as it is", adskip.prepare(["ffmpeg", "-i", "a.mp4"]) == ["ffmpeg", "-i", "a.mp4"])


# Fragmented-MP4 Twitch playlists need their initialization segment.  Keep this
# separate from the old MPEG-TS fixtures: the relay must preserve the HLS
# semantics while it filters ads, not manufacture a version-3 playlist.
def fmp4(seq, segs, map_uri="init.mp4", extra=""):
    out = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{seq}",
           f'#EXT-X-MAP:URI="{map_uri}"', extra]
    for name, more in segs:
        out += [*more, "#EXTINF:2.000,live", name]
    return "\n".join(x for x in out if x) + "\n"


UP3 = "https://video-weaver.fra05.hls.ttvnw.net/v1/playlist/fmp4.m3u8"
adskip._streams.clear()
out = adskip.relay(UP3, fmp4(1, [("one.m4s", [])]))
check("fMP4 map and compatible version reach the relay", '#EXT-X-VERSION:6' in out and
      '#EXT-X-MAP:URI="https://video-weaver.fra05.hls.ttvnw.net/v1/playlist/init.mp4"' in out and
      out.index("#EXT-X-MAP") < out.index("https://video-weaver.fra05.hls.ttvnw.net/v1/playlist/one.m4s"), out)

UPMAP = UP3.replace("fmp4", "map-range")
adskip._streams.clear()
out = adskip.relay(UPMAP, "\n".join(["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:1",
                                      '#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"', "#EXTINF:2.000,live", "one.m4s", ""]))
check("an initialization map keeps its byte range", 'BYTERANGE="720@0"' in out, out)

adskip._streams.clear()
adskip.relay(UP3, fmp4(1, [("one.m4s", [])]))
out = adskip.relay(UP3, "\n".join(["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:2",
                                    "#EXTINF:2.000,live", "two.m4s", ""]))
check("rolling fMP4 playlist keeps its active initialization map", "two.m4s" in out and out.count("#EXT-X-MAP") == 1, out)

UP4 = UP3.replace("fmp4", "map-change")
adskip._streams.clear()
out = adskip.relay(UP4, fmp4(1, [("one.m4s", []), ("two.m4s", [])], "init-a.mp4") +
                   fmp4(3, [("three.m4s", [])], "init-b.mp4"))
check("a changed fMP4 initialization map stays with its later segments", out.count("#EXT-X-MAP") == 2 and
      out.index("init-a.mp4") < out.index("one.m4s") < out.index("init-b.mp4") < out.index("three.m4s"), out)

UP5 = UP3.replace("fmp4", "ranges")
adskip._streams.clear()
out = adskip.relay(UP5, fmp4(1, [("chunk.m4s", ["#EXT-X-BYTERANGE:100@0"]),
                                 ("chunk.m4s", ["#EXT-X-BYTERANGE:100@100"])]))
check("byte ranges are preserved and distinguish otherwise identical URIs", out.count("chunk.m4s") == 2 and
      "#EXT-X-BYTERANGE:100@0" in out and "#EXT-X-BYTERANGE:100@100" in out, out)

UP5B = UP3.replace("fmp4", "implicit-ranges")
adskip._streams.clear()
out = adskip.relay(UP5B, fmp4(1, [("chunk.m4s", ["#EXT-X-BYTERANGE:100@0"]),
                                  ("chunk.m4s", ["#EXT-X-BYTERANGE:100"]),
                                  ("chunk.m4s", ["#EXT-X-BYTERANGE:100"])]))
check("implicit byte ranges use the previous offset for deduplication", out.count("chunk.m4s") == 3 and
      "#EXT-X-BYTERANGE:100\n" in out, out)

UP6 = UP3.replace("fmp4", "discontinuity")
adskip._streams.clear()
out = adskip.relay(UP6, fmp4(1, [("one.m4s", []), ("two.m4s", ["#EXT-X-DISCONTINUITY"])]))
check("source discontinuities are preserved", out.count("#EXT-X-DISCONTINUITY") == 1, out)

try:
    adskip.relay(UP3.replace("fmp4", "iframes"), "#EXTM3U\n#EXT-X-I-FRAMES-ONLY\n#EXT-X-VERSION:4\n")
    check("unsupported I-frame-only playlist is refused", False)
except ValueError as e:
    check("unsupported I-frame-only playlist is refused", "I-frame-only" in str(e), str(e))

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
