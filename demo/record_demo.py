"""Record the Tripwire dashboard tour (dashboard.html?demo=1) to a video.

Writes, next to this script:
  tripwire_demo.webm   Playwright's recording (trimmed to start on a rendered page when ffmpeg is available)
  tripwire_demo.mp4    H.264 / yuv420p / +faststart copy, via imageio-ffmpeg (installed on demand)
  shot_scoreboard.png, shot_replay.png, shot_end.png

Usage:  python demo/record_demo.py
Needs:  pip install playwright   (uses the installed Microsoft Edge, so no browser download)
"""
import pathlib
import re
import shutil
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

DEMO = pathlib.Path(__file__).resolve().parent
ROOT = DEMO.parent
PAGE = (ROOT / "dashboard.html").as_uri()
SIZE = {"width": 1280, "height": 720}
WEBM = DEMO / "tripwire_demo.webm"
MP4 = DEMO / "tripwire_demo.mp4"
SHOTS = [("shot_scoreboard.png", "scoreboard"), ("shot_replay.png", "replay"), ("shot_end.png", "end")]
# data/real_results.js is optional and loaded with a plain <script> tag, so the browser logs a 404 when it is absent.
OPTIONAL = "real_results.js"


def record():
    tmp = DEMO / "_video_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport=SIZE, record_video_dir=str(tmp), record_video_size=SIZE)
        t0 = time.monotonic()
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append((m.text, (m.location or {}).get("url", ""))))
        page.on("pageerror", lambda e: errors.append((f"pageerror: {e}", "")))
        page.goto(PAGE + "?demo=1")
        page.wait_for_function("window.__demoStarted === true", timeout=30_000)
        lead = time.monotonic() - t0
        t1 = time.monotonic()
        page.wait_for_function("window.__demoDone === true", timeout=240_000, polling=250)
        tour = time.monotonic() - t1
        demo_error = page.evaluate("window.__demoError || null")
        video = page.video
        ctx.close()
        src = pathlib.Path(video.path())
        browser.close()
    WEBM.unlink(missing_ok=True)
    shutil.move(str(src), WEBM)
    shutil.rmtree(tmp, ignore_errors=True)
    return lead, tour, errors, demo_error


def screenshots():
    """Second, unrecorded pass at 0.35x timings; captions hidden so the stills are clean."""
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport=SIZE)
        page.goto(PAGE + "?demo=1&fast=1")
        page.add_style_tag(content=".caption{display:none !important}")
        for name, marker in SHOTS:
            page.wait_for_function(f"window.__shot === '{marker}'", timeout=120_000, polling=100)
            if marker == "scoreboard":
                page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(1000 if marker == "end" else 600)
            page.screenshot(path=str(DEMO / name))
        page.wait_for_function("window.__demoDone === true", timeout=120_000)
        browser.close()


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"], check=True)
        import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(ff, path):
    out = subprocess.run([ff, "-i", str(path), "-f", "null", "-"], capture_output=True, text=True).stderr
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", out)
    if not times:
        return None
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def encode(ff, start):
    """Trim the blank lead-in, write the MP4, and replace the raw WebM with a trimmed VP8 copy."""
    ss = ["-ss", f"{start:.2f}"] if start > 0 else []
    subprocess.run([ff, "-y", *ss, "-i", str(WEBM), "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(MP4)],
                   check=True, capture_output=True)
    trimmed = DEMO / "_trimmed.webm"
    try:
        subprocess.run([ff, "-y", *ss, "-i", str(WEBM), "-an", "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
                        "-b:v", "3000k", "-r", "25", str(trimmed)], check=True, capture_output=True)
        trimmed.replace(WEBM)
        return True
    except subprocess.CalledProcessError:
        trimmed.unlink(missing_ok=True)
        return False


def main():
    print("Recording the tour (about a minute)...")
    lead, tour, errors, demo_error = record()
    unexpected = [text for text, url in errors if OPTIONAL not in url]
    print(f"  raw video: {WEBM}  (tour ran {tour:.1f} s after a {lead:.1f} s load)")
    if demo_error:
        print(f"  demo script error: {demo_error}")
    print(f"  console errors: {unexpected or 'none'}"
          + ("  (plus the expected 404 for the optional data/real_results.js)" if len(unexpected) < len(errors) else ""))

    print("Taking screenshots...")
    screenshots()
    for name, _ in SHOTS:
        print(f"  {DEMO / name}")

    print("Converting to MP4...")
    try:
        ff = ffmpeg_exe()
        webm_trimmed = encode(ff, max(0.0, lead - 0.6))
        print(f"  mp4:  {MP4}  ({duration(ff, MP4):.1f} s)")
        print(f"  webm: {WEBM}  ({duration(ff, WEBM):.1f} s{', trimmed' if webm_trimmed else ', untrimmed'})")
    except Exception as exc:  # keep the WebM if anything about ffmpeg fails
        print(f"  MP4 conversion failed ({exc}); keeping {WEBM}")


if __name__ == "__main__":
    main()
