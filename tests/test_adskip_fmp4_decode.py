"""A local fMP4 HLS playlist remains decodable after the ad relay rewrites it."""
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
from studio import adskip  # noqa: E402


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory(prefix="peakcut-hls-") as tmp:
    root = Path(tmp)
    source = root / "source.m3u8"
    made = run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", "4", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_segment_type", "fmp4",
                "-hls_flags", "independent_segments", "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", "part%d.m4s", "source.m3u8"], root)
    if made.returncode:
        print(made.stderr)
        raise SystemExit(1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=str(root), **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adskip._streams.clear()
        upstream = f"http://127.0.0.1:{server.server_port}/source.m3u8"
        relayed = adskip.relay(upstream, source.read_text(encoding="utf-8"))
        playlist = root / "relayed.m3u8"
        playlist.write_text(relayed, encoding="utf-8")
        decoded = run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file,http,tcp,crypto,data", "-i", str(playlist), "-f", "null", "-"])
        if decoded.returncode:
            print(decoded.stderr)
            raise SystemExit(1)
    finally:
        server.shutdown()
        server.server_close()
    print("PASS fMP4 relay fixture is fully decodable by FFmpeg")
