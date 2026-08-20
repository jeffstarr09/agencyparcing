#!/usr/bin/env python3
"""
app.py - The button.

Starts a small web server on your own machine, opens your browser, and gives you
a Start button. Nothing is sent anywhere except to the sites being checked; the
server is bound to 127.0.0.1, so it is not reachable from outside this computer.

    python app.py

Uses only the Python standard library, so there is nothing to install for the UI
itself. Everything it does, the command-line scripts can also do - this is a
front door, not a different program.
"""

import json
import os
import queue
import socket
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import diagnose
import pipeline

HOST = "127.0.0.1"
PORT_RANGE = range(8765, 8790)
SETTINGS_PATH = os.path.join("data", "app_settings.json")
MAX_LOG_LINES = 600


# --------------------------------------------------------------------------
# Run manager - one pipeline run at a time, in a background thread
# --------------------------------------------------------------------------

class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self._stop = threading.Event()
        self.log = []
        self.seq = 0
        self.state = "idle"          # idle | running | stopping | done | error
        self.current = ""
        self.progress = {"batch_index": 0, "batch_total": 0, "step": "", "i": 0, "n": 0}
        self.totals = pipeline.totals_from_disk()
        self.started_at = None
        self.error = ""

    # -- logging --------------------------------------------------------

    def emit(self, kind, message, **data):
        with self.lock:
            self.seq += 1
            self.log.append({
                "seq": self.seq, "kind": kind, "message": message,
                "at": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            })
            del self.log[:-MAX_LOG_LINES]
            if kind == "batch":
                self.current = message
                self.progress["batch_index"] = data.get("batch_index", 0)
                self.progress["batch_total"] = data.get("batch_total", 0)
            if kind == "progress":
                self.progress["step"] = data.get("step", "")
                self.progress["i"] = data.get("i", 0)
                self.progress["n"] = data.get("n", 0)
            if kind == "error":
                self.error = message

    # -- lifecycle ------------------------------------------------------

    def start(self, config):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, "Already running."
        self._stop.clear()
        self.state = "running"
        self.error = ""
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.emit("info", "Starting.")

        def work():
            try:
                totals = pipeline.run(config, self.emit, self._stop.is_set)
                self.totals = totals
                self.state = "stopped" if self._stop.is_set() else "done"
                self.emit("info", "Stopped." if self._stop.is_set() else "Finished.")
            except Exception as e:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"
                self.emit("error", self.error)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True, "Started."

    def start_processing(self, domains, config):
        """
        Check a specific set of agencies, rather than going looking for new ones.
        Used after an import - same downstream code, different starting point.
        """
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, "Already running."
        self._stop.clear()
        self.state = "running"
        self.error = ""
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.emit("info", f"Checking {len(domains)} agency website(s) you added.")

        def work():
            try:
                self.totals = pipeline.process_agencies(
                    domains, self.emit, self._stop.is_set, config)
                self.state = "stopped" if self._stop.is_set() else "done"
                self.emit("info", "Stopped." if self._stop.is_set() else "Finished.")
            except Exception as e:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"
                self.emit("error", self.error)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True, "Started."

    def start_checking(self, domains, config, agency_name=""):
        """Pixel-check a list of brand websites and stop there."""
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, "Already running."
        self._stop.clear()
        self.state = "running"
        self.error = ""
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.emit("info", f"Checking {len(domains)} website(s) for a TikTok pixel.")

        def work():
            try:
                self.totals = pipeline.check_domains_only(
                    domains, self.emit, self._stop.is_set, config, agency_name)
                self.state = "stopped" if self._stop.is_set() else "done"
                self.emit("info", "Stopped." if self._stop.is_set() else "Finished.")
            except Exception as e:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"
                self.emit("error", self.error)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True, "Started."

    def stop(self):
        if self.thread and self.thread.is_alive():
            self._stop.set()
            self.state = "stopping"
            self.emit("info", "Finishing the current site, then stopping...")
            return True
        return False

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def status(self, since=0):
        with self.lock:
            lines = [l for l in self.log if l["seq"] > since]
            return {
                "state": "running" if self.running else self.state,
                "current": self.current,
                "progress": dict(self.progress),
                "totals": self.totals or pipeline.totals_from_disk(),
                "log": lines,
                "seq": self.seq,
                "error": self.error,
                "started_at": self.started_at,
            }


RUNNER = Runner()


# --------------------------------------------------------------------------
# Settings and setup checks
# --------------------------------------------------------------------------

def load_settings():
    cfg = dict(pipeline.DEFAULT_CONFIG)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg


def save_settings(cfg):
    os.makedirs(os.path.dirname(os.path.abspath(SETTINGS_PATH)), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def setup_status():
    """What is ready and what isn't. Only Python is actually required to start."""
    checks = []

    creds = os.path.exists("service_account.json") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS")
    email = ""
    if os.path.exists("service_account.json"):
        try:
            with open("service_account.json", encoding="utf-8") as f:
                email = json.load(f).get("client_email", "")
        except (OSError, ValueError):
            email = ""
    checks.append({
        "id": "sheets",
        "label": "Google Sheets",
        "ok": bool(creds),
        "required": False,
        "detail": (f"Ready. Make sure the Sheet is shared with {email} as an Editor."
                   if email else
                   "Optional. Without it, results are saved as spreadsheet files "
                   "in this folder, which you can open in Excel or Numbers."),
    })

    try:
        partners = pipeline.tiktok_partners.domain_set(required=False)
    except SystemExit:
        partners = set()
    checks.append({
        "id": "suppression",
        "label": "TikTok partner list",
        "ok": bool(partners),
        "required": False,
        "detail": (f"{len(partners)} badged partners will be skipped."
                   if partners else
                   "Not loaded. This only skips agencies TikTok has officially "
                   "badged — roughly 500 worldwide, almost none of them small. "
                   "You can safely start without it."),
    })

    try:
        import requests  # noqa: F401
        deps_ok, deps_detail = True, "Ready."
    except ImportError:
        deps_ok, deps_detail = False, ("Missing. Run:  pip install -r requirements.txt")
    checks.append({"id": "deps", "label": "Python packages", "ok": deps_ok,
                   "required": True, "detail": deps_detail})

    return {
        "checks": checks,
        "can_start": all(c["ok"] for c in checks if c["required"]),
        "results_folder": os.path.abspath("."),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TikTokGap"

    def log_message(self, *args):
        pass                      # the UI is the log; don't clutter the console

    # -- helpers --------------------------------------------------------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # This server only ever talks to the page it serves.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length or length > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}

    def _guard(self):
        """
        Only accept requests from a browser on this machine.

        The server is already bound to 127.0.0.1, so this is belt and braces
        against a page on another site scripting requests at it.
        """
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = urlparse(origin).hostname
            if oh not in ("127.0.0.1", "localhost"):
                self._json({"error": "bad origin"}, 403)
                return False
        return True

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, _load_page().encode("utf-8"),
                              "text/html; charset=utf-8")
        if path == "/api/setup":
            return self._json(setup_status())
        if path == "/api/settings":
            return self._json(load_settings())
        if path == "/api/status":
            try:
                since = int(urlparse(self.path).query.split("since=")[-1] or 0)
            except (ValueError, IndexError):
                since = 0
            return self._json(RUNNER.status(since))
        if path == "/api/results":
            return self._json({"agencies": pipeline.top_agencies(60),
                               "totals": pipeline.totals_from_disk()})
        if path == "/api/review":
            return self._json({"clients": pipeline.clients_for_review(150)})
        if path == "/api/problems":
            return self._json(problems())
        if path == "/api/search-links":
            q = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&")
                     if "=" in p)
            from urllib.parse import unquote_plus
            return self._json({"links": pipeline.search_links(
                unquote_plus(q.get("vertical", "home_services")),
                unquote_plus(q.get("geo", "")),
                unquote_plus(q.get("engine", "google")))})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/start":
            cfg = load_settings()
            cfg.update({k: v for k, v in (body or {}).items() if k in cfg})
            save_settings(cfg)
            ok, msg = RUNNER.start(cfg)
            return self._json({"ok": ok, "message": msg})

        if path == "/api/stop":
            return self._json({"ok": RUNNER.stop()})

        if path == "/api/settings":
            cfg = load_settings()
            cfg.update({k: v for k, v in (body or {}).items() if k in cfg})
            save_settings(cfg)
            return self._json(cfg)

        if path == "/api/verdict":
            key = (body.get("entity_key") or "").strip()
            agency = (body.get("agency_domain") or "").strip()
            verdict = (body.get("verdict") or "").strip()
            if not key or not verdict:
                return self._json({"error": "need entity_key and verdict"}, 400)
            if verdict not in pipeline.feedback.VERDICTS:
                return self._json({"error": f"unknown verdict {verdict}"}, 400)
            try:
                report = pipeline.record_verdict(
                    key, agency, verdict,
                    (body.get("correct_value") or "").strip(),
                    (body.get("method") or "").strip())
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"ok": True, "applied": report.get("applied", 0),
                               "skipped": report.get("skipped", [])})

        if path == "/api/diagnose":
            if RUNNER.running:
                return self._json({"error": "Stop the run first, then diagnose."}, 409)
            try:
                return self._json(diagnose.run(delay=0.5))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        if path == "/api/import-preview":
            found = pipeline.extract_agencies(
                body.get("text", ""), body.get("vertical", ""), body.get("geo", ""))
            return self._json({"found": [f["agency_domain"] for f in found]})

        if path == "/api/import":
            domains = body.get("domains") or []
            if not domains:
                found = pipeline.extract_agencies(body.get("text", ""))
                domains = [f["agency_domain"] for f in found]
            if not domains:
                return self._json({"error": "No websites found in that."}, 400)

            if body.get("kind") == "brands":
                # A list of brands: pixel-check them and stop. No agency record,
                # no client parsing - that would invent a relationship nobody
                # told us about.
                if RUNNER.running:
                    return self._json({"error": "Stop the current run first."}, 409)
                RUNNER.start_checking(domains, load_settings(),
                                      (body.get("agency_name") or "").strip())
                return self._json({"ok": True, "checking": len(domains),
                                   "kind": "brands"})

            added, known, partner = pipeline.add_agencies(
                domains, body.get("vertical", ""), body.get("geo", ""))
            if body.get("check_now") and not RUNNER.running:
                RUNNER.start_processing(domains, load_settings())
            return self._json({"ok": True, "added": added,
                               "already_known": known, "tiktok_partners": partner,
                               "kind": "agencies"})

        if path == "/api/reset":
            pipeline.reset_state()
            return self._json({"ok": True})

        if path == "/api/open-folder":
            _open_folder(os.path.abspath("."))
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def problems():
    """The honest column: what we could not read, and why."""
    out = {"blocked_sources": [], "needs_review": [], "unreachable": [], "notes": []}
    try:
        out["blocked_sources"] = pipeline.feedback.health_report()
    except Exception:
        pass
    import csv as _csv
    if os.path.exists(pipeline.AGENCIES_CSV):
        try:
            with open(pipeline.AGENCIES_CSV, newline="", encoding="utf-8-sig") as f:
                for r in _csv.DictReader(f):
                    if r.get("status") == "needs_manual_review":
                        out["needs_review"].append({
                            "agency_domain": r.get("agency_domain", ""),
                            "page": r.get("client_page_url", ""),
                        })
                    elif r.get("status") == "unreachable":
                        out["unreachable"].append({
                            "agency_domain": r.get("agency_domain", ""),
                        })
        except (OSError, ValueError):
            pass
    if os.path.exists(pipeline.FAILURES_CSV):
        try:
            with open(pipeline.FAILURES_CSV, newline="", encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
            counts = {}
            for r in rows:
                counts[r.get("reason", "?")] = counts.get(r.get("reason", "?"), 0) + 1
            out["notes"] = [f"{n} x {reason}" for reason, n
                            in sorted(counts.items(), key=lambda kv: -kv[1])][:8]
        except (OSError, ValueError):
            pass
    out["needs_review"] = out["needs_review"][:40]
    out["unreachable"] = out["unreachable"][:40]
    return out


def _open_folder(path):
    try:
        if sys.platform == "darwin":
            os.system(f'open "{path}"')
        elif os.name == "nt":
            os.startfile(path)          # noqa: S606
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
    except Exception:
        pass


def find_port():
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return None


PAGE = ""       # the UI, read from ui.html
_PAGE_MTIME = 0


def _load_page():
    """
    Read ui.html, and re-read it whenever the file changes.

    Re-reading means editing the interface does not need the app restarted,
    which matters because the app is the thing people leave running for hours.
    """
    global PAGE, _PAGE_MTIME
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "ui.html")
    if not os.path.exists(path):
        sys.exit(f"Missing {path}. The app needs ui.html next to app.py.")
    mtime = os.path.getmtime(path)
    if mtime != _PAGE_MTIME or not PAGE:
        with open(path, encoding="utf-8") as f:
            PAGE = f.read()
        _PAGE_MTIME = mtime
    return PAGE


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    _load_page()

    port = find_port()
    if not port:
        sys.exit(f"Could not find a free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}. "
                 f"Close any other copy of this app and try again.")

    url = f"http://{HOST}:{port}/"
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True

    print("\n  TikTok Gap")
    print(f"  Open this in your browser if it didn't open by itself:\n\n      {url}\n")
    print("  Leave this window open while it runs. Close it to quit.\n")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        RUNNER.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
