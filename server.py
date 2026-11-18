#!/usr/bin/env python
"""Tripwire Live: serves the project folder and streams sim.py games as Server-Sent Events.

  python server.py               then open http://localhost:8765/live.html
  python server.py --port 8766   start looking for a free port at 8766
  python server.py --allow-real  enable Claude-agent games (spends API credits; needs ANTHROPIC_API_KEY)

  GET /api/config   monitors, rogue styles, featured seeds, whether real (Claude) mode is available
  GET /api/stream?monitor=ours_full&rogue=plain&seed=3&delay=2.5&mode=mock
                    events: start, one round per round, end (game summary without rounds)
"""
import argparse, importlib.util, json, os, sys, threading, time
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import sim

ROOT = Path(__file__).resolve().parent
AGENT_NAMES = {"agent0": "Ava", "agent1": "Ben", "agent2": "Cleo", "agent3": "Dev", "agent4": "Eli"}
ROGUE_STYLES = {
    "plain": "Reports every line 15% high",
    "sneaky": "Honest for 4 rounds, then inflates only lines over $6,500",
}
LIVE_MONITORS = ["baseline", "ours_full", "ours_bait"]
MAX_DELAY = 30.0
MAX_STREAMS = 6
DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
STATIC_EXTENSIONS = {".html", ".js", ".json", ".png", ".mp4", ".webm", ".svg", ".ico", ".css"}
STATIC_BLOCKED = {"data/real_cache.json"}

_real_lock = threading.Lock()
_streams = threading.BoundedSemaphore(MAX_STREAMS)
ALLOW_REAL = False


class ClientGone(Exception):
    pass


class LiveClaudePlayer(sim.ClaudePlayer):
    """Two offices stream at once; serialise turns so they never write the shared cache file together."""

    def answer(self, item, rnd, rng):
        with _real_lock:
            return super().answer(item, rnd, rng)


def real_status():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "Set ANTHROPIC_API_KEY and restart server.py"
    if importlib.util.find_spec("anthropic") is None:
        return False, "pip install anthropic, then restart server.py"
    if not ALLOW_REAL:
        return False, "Real mode spends API credits. Restart with: python server.py --allow-real"
    return True, ""


def setup_real():
    with _real_lock:
        if sim.ClaudePlayer.client is not None:
            return
        import anthropic
        sim.DATA.mkdir(exist_ok=True)
        sim.ClaudePlayer.client = anthropic.Anthropic()
        sim.ClaudePlayer.model = os.environ.get("TRIPWIRE_MODEL", "claude-haiku-4-5")
        sim.ClaudePlayer.cache_path = sim.DATA / "real_cache.json"
        if sim.ClaudePlayer.cache_path.exists():
            sim.ClaudePlayer.cache = json.loads(sim.ClaudePlayer.cache_path.read_text())


@lru_cache(maxsize=1)
def featured_seeds():
    try:
        featured = json.loads((sim.DATA / "mock_results.json").read_text()).get("featured", {})
    except (OSError, ValueError):
        featured = {}
    plain, sneaky = featured.get("plain"), featured.get("sneaky_escape")
    return {"plain": 3 if plain is None else plain, "sneaky": 0 if sneaky is None else sneaky}


def config():
    available, reason = real_status()
    return {
        "monitors": [{"id": m, "label": sim.MONITORS[m]["label"], "short": sim.MONITORS[m]["short"]}
                     for m in LIVE_MONITORS],
        "rogue_styles": [{"id": k, "label": v} for k, v in ROGUE_STYLES.items()],
        "featured": featured_seeds(),
        "agents": AGENT_NAMES,
        "rounds": sim.ROUNDS,
        "real_available": available,
        "real_reason": reason,
        "model": os.environ.get("TRIPWIRE_MODEL", "claude-haiku-4-5"),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/stream"):
            super().log_message(fmt, *args)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def handle(self):
        try:
            super().handle()
        except DISCONNECTS:
            pass

    def host_ok(self):
        # Rejecting unknown Host headers blocks DNS-rebinding pages from reaching this server.
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def same_site(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc.lower() in self.server.allowed_hosts

    def static_ok(self, path):
        rel = unquote(path).lstrip("/").replace("\\", "/")
        parts = [p for p in rel.split("/") if p]
        return (bool(parts) and not any(p.startswith(".") for p in parts)
                and rel not in STATIC_BLOCKED and Path(rel).suffix.lower() in STATIC_EXTENSIONS)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def do_HEAD(self):
        if not self.host_ok() or not self.static_ok(urlparse(self.path).path):
            return self.send_error(404)
        super().do_HEAD()

    def do_GET(self):
        url = urlparse(self.path)
        if not self.host_ok():
            return self.send_error(403, "Unknown Host header")
        if url.path.startswith("/api/") and not self.same_site():
            return self.send_json({"error": "cross-site request refused"}, 403)
        if url.path == "/":
            self.send_response(302)
            self.send_header("Location", "/live.html")
            self.end_headers()
        elif url.path == "/api/config":
            self.send_json(config())
        elif url.path == "/api/stream":
            if not _streams.acquire(blocking=False):
                return self.send_json({"error": f"too many games running (limit {MAX_STREAMS})"}, 429)
            try:
                self.stream({k: v[0] for k, v in parse_qs(url.query).items()})
            finally:
                _streams.release()
        elif self.static_ok(url.path):
            super().do_GET()
        else:
            self.send_error(404)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_event(self, name, data):
        try:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        except DISCONNECTS as exc:
            raise ClientGone from exc

    def stream(self, q):
        monitor, rogue, mode = q.get("monitor", "ours_full"), q.get("rogue", "plain"), q.get("mode", "mock")
        try:
            seed, delay = int(q.get("seed", 3)), float(q.get("delay", 2.5))
        except ValueError:
            return self.send_json({"error": "seed must be an integer and delay a number"}, 400)
        if monitor not in sim.MONITORS:
            return self.send_json({"error": f"unknown monitor {monitor!r}"}, 400)
        if rogue not in ROGUE_STYLES:
            return self.send_json({"error": f"unknown rogue style {rogue!r}"}, 400)
        if mode not in ("mock", "real"):
            return self.send_json({"error": "mode must be mock or real"}, 400)
        delay = max(0.0, min(delay, MAX_DELAY))
        player = sim.MockPlayer
        if mode == "real":
            available, reason = real_status()
            if not available:
                return self.send_json({"error": reason}, 400)
            setup_real()
            player = LiveClaudePlayer

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def on_round(rec):
            self.send_event("round", rec)
            if mode == "mock" and delay and rec["round"] < sim.ROUNDS:
                time.sleep(delay)

        try:
            # The rogue depends only on the seed, so a scripted dry run names it without any API calls.
            rogue_name = sim.play(seed, monitor, rogue)["rogue"]
            self.send_event("start", {
                "seed": seed, "monitor": monitor, "monitor_label": sim.MONITORS[monitor]["label"],
                "rogue_style": rogue, "rogue": rogue_name, "agents": AGENT_NAMES,
                "rounds": sim.ROUNDS, "mode": mode, "delay": delay,
            })
            game = sim.play(seed, monitor, rogue, player, on_round)
            game.pop("rounds", None)
            game["mode"] = mode
            self.send_event("end", game)
        except ClientGone:
            pass
        except Exception as exc:
            try:
                self.send_event("fail", {"error": f"{type(exc).__name__}: {exc}"})
            except ClientGone:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRIPWIRE_PORT", 8765)))
    ap.add_argument("--allow-real", action="store_true", help="enable Claude-agent games (spends API credits)")
    args = ap.parse_args()
    global ALLOW_REAL
    ALLOW_REAL = args.allow_real
    httpd = None
    for port in range(args.port, args.port + 10):
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            print(f"  port {port} is busy, trying {port + 1}", flush=True)
    if httpd is None:
        sys.exit(f"Ports {args.port}-{args.port + 9} are all busy. Run: python server.py --port <free port>")
    httpd.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    available, reason = real_status()
    print(f"Tripwire Live: http://localhost:{port}/live.html", flush=True)
    print(f"  real mode: {'available' if available else 'off (' + reason + ')'}   Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
