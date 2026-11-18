#!/usr/bin/env python
"""Record Tripwire Live to demo/tripwire_live.webm (+ .mp4 when an ffmpeg binary is available).

  python demo/record_live.py                       plain rogue, seed 3, 1x speed
  python demo/record_live.py --rogue sneaky --seed 0 --variant ours_bait

Starts server.py (or reuses one already answering on the port), records the page in headless
Edge at 1280x720, saves two screenshots, and checks the on-screen scorecards against sim.play().
"""
import argparse, importlib, json, queue, re, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VIEW = {"width": 1280, "height": 720}
OUT_WEBM, OUT_MP4 = HERE / "tripwire_live.webm", HERE / "tripwire_live.mp4"
SHOT_MID, SHOT_END = HERE / "shot_live_mid.png", HERE / "shot_live_end.png"
HOLD_MS = 4000
PAINT_SLACK = 0.3  # seconds after the DOM is ready before the first painted frame is trusted

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, str(ROOT))
import sim  # noqa: E402


def tripwire_up(port):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/config", timeout=2) as r:
            return "monitors" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server(port):
    """Return (port, process or None). Reuses a running Tripwire server; otherwise starts server.py and uses
    the port it prints, since server.py moves on to the next free port when the one asked for is taken."""
    if tripwire_up(port):
        print(f"reusing server.py on port {port}")
        return port, None
    while port_busy(port):
        print(f"port {port} is held by another program, trying {port + 1}")
        port += 1
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py"), "--port", str(port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            creationflags=flags)
    printed = queue.Queue()

    def drain():
        # server.py logs every request, so the pipe has to be read for as long as the server runs.
        for line in proc.stdout:
            m = re.search(r"Tripwire Live: http://localhost:(\d+)/", line)
            if m:
                printed.put(int(m.group(1)))

    threading.Thread(target=drain, daemon=True).start()
    try:
        port = printed.get(timeout=15)
    except queue.Empty:
        proc.kill()
        sys.exit("server.py did not print its URL")
    for _ in range(60):
        if tripwire_up(port):
            print(f"started server.py on port {port} (pid {proc.pid})")
            return port, proc
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    proc.kill()
    sys.exit("server.py did not start")


def ffmpeg():
    """imageio-ffmpeg ships its own ffmpeg binary; install it on first use. Returns (module, exe) or (None, None)."""
    try:
        import imageio_ffmpeg
    except ImportError:
        print("installing imageio-ffmpeg for MP4 conversion ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"], check=False)
        importlib.invalidate_caches()
        try:
            import imageio_ffmpeg
        except ImportError:
            return None, None
    try:
        return imageio_ffmpeg, imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        print(f"no ffmpeg binary: {exc}")
        return None, None


def record(url, mid_mark):
    from playwright.sync_api import sync_playwright

    problems = []
    tmp = Path(tempfile.mkdtemp(prefix="tripwire_video_"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            ctx = browser.new_context(viewport=VIEW, record_video_dir=str(tmp), record_video_size=VIEW)
            page = ctx.new_page()
            t0 = time.time()
            page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#officeB .desk') !== null", timeout=60_000)
            lead = time.time() - t0 + PAINT_SLACK
            page.wait_for_function("window.__liveMarks.length > 0", timeout=60_000)
            print(f"page painted after {lead - PAINT_SLACK:.1f}s, streaming after {time.time() - t0:.1f}s")
            page.wait_for_function(f"window.__liveMarks.includes({json.dumps(mid_mark)}) || window.__liveDone === true",
                                   timeout=180_000)
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOT_MID))
            print(f"mid screenshot at {time.time() - t0:.1f}s ({mid_mark})")
            page.wait_for_function("window.__liveDone === true", timeout=240_000)
            game_secs = time.time() - t0
            page.wait_for_timeout(HOLD_MS)
            page.screenshot(path=str(SHOT_END))
            state = page.evaluate("""() => ({
                summaries: window.__liveSummaries, rounds: window.__liveRounds, error: window.__liveError,
                cards: Object.fromEntries(['A', 'B'].map(k => [k, Object.fromEntries(
                    [...document.querySelectorAll(`#office${k} .score .st[data-k]`)].map(e => [e.dataset.k, e.dataset.v]))])),
            })""")
            video = page.video
            page.close()
            video.save_as(str(OUT_WEBM))
            ctx.close()
            browser.close()
            wall = time.time() - t0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"games finished after {game_secs:.1f}s; raw recording {wall:.1f}s")
    return state, problems, wall, lead


def check(state, problems, rogue, seed, variant):
    ok = True
    for key, monitor in (("A", "baseline"), ("B", variant)):
        want = sim.play(seed, monitor, rogue)
        got, card = state["summaries"].get(key, {}), state["cards"].get(key, {})
        fields = ("rogue", "caught_round", "false_firings", "final_error", "final_ledger", "model_calls")
        diffs = [f for f in fields if got.get(f) != want[f]]
        card_diffs = [k for k, ok_ in (
            ("caught", card.get("caught") == str(want["caught_round"] or "")),
            ("false", card.get("false") == str(want["false_firings"])),
            ("error", card.get("error") not in (None, "") and abs(float(card["error"]) - want["final_error"]) < 0.005),
            ("calls", card.get("calls") == str(want["model_calls"])),
        ) if not ok_]
        animated = state["rounds"].get(key)
        line_ok = not diffs and not card_diffs and animated == sim.ROUNDS
        ok &= line_ok
        print(f"  Office {key} ({monitor}): rounds animated {animated}/{sim.ROUNDS}, caught {want['caught_round']}, "
              f"honest fired {want['false_firings']}, error {want['final_error']:+,.2f}, calls {want['model_calls']} "
              f"-> {'matches sim.play' if line_ok else f'MISMATCH stream={diffs} scorecard={card_diffs}'}")
    if state.get("error"):
        ok = False
        print(f"  stream error: {state['error']}")
    print(f"  console errors: {len(problems)}")
    for p in problems:
        print(f"    {p}")
    return ok and not problems


def run_ffmpeg(exe, *args):
    try:
        r = subprocess.run([exe, "-nostdin", "-y", "-loglevel", "error", *args], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("ffmpeg timed out")
        return False
    if r.returncode:
        print(f"ffmpeg failed: {r.stderr.strip()[:300]}")
    return r.returncode == 0


def postprocess(lead):
    """Trim the blank lead-in before the page painted, rewrite the WebM with a duration header, make the MP4."""
    mod, exe = ffmpeg()
    if not exe:
        print("no ffmpeg: keeping the raw WebM (untrimmed), no MP4")
        return None
    raw = OUT_WEBM.with_name("tripwire_live.raw.webm")
    OUT_WEBM.replace(raw)
    trim = ["-ss", f"{lead:.2f}"] if lead > 0.05 else []
    ok = run_ffmpeg(exe, *trim, "-i", str(raw), "-c:v", "libvpx", "-b:v", "1200k", "-deadline", "good",
                    "-cpu-used", "4", "-an", str(OUT_WEBM))
    if not ok:
        OUT_WEBM.unlink(missing_ok=True)
        raw.replace(OUT_WEBM)
        trim = []
        print("kept the untrimmed WebM")
    made_mp4 = run_ffmpeg(exe, *trim, "-i", str(raw if raw.exists() else OUT_WEBM), "-c:v", "libx264",
                          "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium", "-r", "25",
                          "-movflags", "+faststart", "-an", str(OUT_MP4))
    raw.unlink(missing_ok=True)
    if not made_mp4:
        OUT_MP4.unlink(missing_ok=True)
        print("MP4 conversion failed; WebM only")
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rogue", choices=["plain", "sneaky"], default="plain")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--variant", choices=["ours_full", "ours_bait"], default="ours_full")
    ap.add_argument("--speed", choices=["0.5", "1", "2"], default="1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mid-mark", default="B:r6:strike", help="page marker to wait for before the mid-game screenshot")
    args = ap.parse_args()

    port, proc = ensure_server(args.port)
    try:
        url = (f"http://localhost:{port}/live.html?autostart=1&reveal=1&rogue={args.rogue}&seed={args.seed}"
               f"&speed={args.speed}&variant={args.variant}")
        print(f"recording {url}")
        state, problems, wall, lead = record(url, args.mid_mark)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
            print("stopped server.py")

    print("checks:")
    passed = check(state, problems, args.rogue, args.seed, args.variant)
    OUT_MP4.unlink(missing_ok=True)
    mod = postprocess(lead)
    for path in (OUT_WEBM, OUT_MP4):
        if path.exists():
            secs = mod.count_frames_and_secs(str(path))[1] if mod else wall
            print(f"wrote {path.relative_to(ROOT)}  {path.stat().st_size / 1e6:.1f} MB  {secs:.1f}s")
    print(f"wrote {SHOT_MID.relative_to(ROOT)} and {SHOT_END.relative_to(ROOT)}")
    print("all checks passed" if passed else "SOME CHECKS FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
