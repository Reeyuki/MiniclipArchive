#!/usr/bin/env python3
"""
Local proxy server: browser NEVER talks to web.archive.org directly.
- Serves index.html / style.css with all web.archive.org URLs rewritten to 127.0.0.1:PORT
- /web/... routes are fetched SERVER-SIDE from http://web.archive.org via curl subprocess,
  cached under downloaded_images/, then served to the browser.
- Uses http (not https) for wayback fetches.
"""
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_FILE = BASE_DIR / "index.html"
CSS_FILE = BASE_DIR / "style.css"
OUT_DIR = BASE_DIR / "downloaded_images"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
HOST = "127.0.0.1"
LOCAL_PREFIX = f"http://{HOST}:{PORT}"
WAYBACK_HTTP = "http://web.archive.org"  # http, never https
CURL_BIN = shutil.which("curl") or "curl"

WAYBACK_RE = re.compile(r"/web/(\d+)([a-z_]*)/(.*)", re.IGNORECASE)
# matches https://web.archive.org , http://web.archive.org , //web.archive.org
ARCHIVE_HOST_RE = re.compile(r"(https?:)?//web\.archive\.org", re.IGNORECASE)
# image extensions for uniform-structure normalization
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|bmp)(\?.*)?$", re.IGNORECASE)


def force_im_proxy_path(proxy_path: str) -> str:
    """Normalize /web/<ts>[mod]/<orig> -> /web/<ts>im_/<orig> so all games/images
    follow the same structure and wayback returns raw bytes, not an HTML wrapper."""
    m = WAYBACK_RE.search(proxy_path)
    if not m:
        return proxy_path
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    if mod == "im_":
        return proxy_path
    return f"/web/{ts}im_/{orig}"


def rewrite_to_local(text: str) -> str:
    """Rewrite every archive reference to our local proxy, normalizing images
    to the same im_ structure. Handles plain and backslash-escaped (JS/JSON) forms."""
    # escaped forms first: http:\/\/web.archive.org , https:\/\/web.archive.org , \/\/web.archive.org
    text = re.sub(r"(https?:)?\\/\\/web\.archive\.org", LOCAL_PREFIX.replace("://", r":\/\/"), text,
                  flags=re.IGNORECASE)
    text = ARCHIVE_HOST_RE.sub(LOCAL_PREFIX, text)

    def _repl_host(m):
        return LOCAL_PREFIX

    text = ARCHIVE_HOST_RE.sub(_repl_host, text)

    def _repl_path(m):
        full, ts, mod, orig = m.group(0), m.group(1), m.group(2), m.group(3)
        # only normalize actual images; leave page links (mp_, if_, etc.) alone
        if IMAGE_EXT_RE.search(orig) and mod != "im_":
            return f"/web/{ts}im_/{orig}"
        return full

    return re.sub(r"/web/(\d+)([a-z_]*)/(\S+?)(?=[\"'\)\s<>])", _repl_path, text)


def local_path_for_proxy(proxy_path: str) -> Path:
    """Map /web/<ts>[mod]/<orig> -> downloaded_images/<ts>/<host>/<path>.
    All variants of the same game/page share one cache file (uniform structure)."""
    m = WAYBACK_RE.search(proxy_path)
    if not m:
        h = hashlib.md5(proxy_path.encode()).hexdigest()[:12]
        return OUT_DIR / "misc" / f"{h}.bin"
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    if "://" not in orig:
        orig = "http://" + orig
    # strip fragment, keep path
    orig = orig.split("#")[0]
    parsed = urllib.parse.urlparse(orig)
    host = parsed.netloc or "unknown-host"
    path = parsed.path.lstrip("/") or "index"
    if path.endswith("/"):
        path = path + "index.html"
    if "." not in Path(path).name:
        # extensionless page like /games/foo/en -> store as en.html for uniform structure
        path = path.rstrip("/") + ".html" if "/" in path else path + ".html"
    return OUT_DIR / ts / host / path


def is_image_path(proxy_path: str) -> bool:
    return bool(IMAGE_EXT_RE.search(proxy_path.split("?")[0].split("#")[0]))


def valid_image_bytes(body: bytes) -> bool:
    if not body:
        return False
    s = body.lstrip()[:32].lower()
    if s.startswith((b"<!doctype", b"<html")):
        return False
    if body.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                        b"\x00\x00\x01\x00")):
        return True
    if body.startswith(b"RIFF") and b"WEBP" in body[:16]:
        return True
    t = body.lstrip()[:500].lower()
    if t.startswith((b"<svg", b"<?xml")) or b"<svg" in t[:500]:
        return True
    return len(body) >= 100


def fetch_via_curl(wayback_url: str, dest: Path, retries: int = 8, expect_image: bool = True,
                   budget: float = 60.0):
    """Server-side fetch using curl subprocess over http (never https).
    Retries for up to `budget` seconds (~60s) with exponential backoff + jitter.
    Tries im_ -> id_ -> plain fallbacks for images. Returns (ok, content_type, err)."""
    import random

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part") if dest.suffix else Path(str(dest) + ".part")

    # candidate URLs: normalized im_ first, then id_, then as-requested
    candidates = [wayback_url]
    m = WAYBACK_RE.search(wayback_url)
    if m and expect_image and m.group(2) != "im_":
        ts, mod, orig = m.group(1), m.group(2), m.group(3)
        base = wayback_url.split("/web/")[0]
        candidates = [f"{base}/web/{ts}im_/{orig}",
                      f"{base}/web/{ts}id_/{orig}",
                      wayback_url]

    last_err = ""
    t0 = time.monotonic()
    attempt = 0
    for cand in candidates:
        while time.monotonic() - t0 < budget:
            attempt += 1
            # generous timeouts: wayback page rewrites can be slow
            cmd = [CURL_BIN, "-sS", "-L", "--fail",
                   "--connect-timeout", "30", "--max-time", "90",
                   "--retry", "0", "-A", "Mozilla/5.0 (local-proxy)",
                   "-o", str(tmp), "-w", "%{http_code} %{content_type}",
                   "--", cand]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=105)
                meta = (p.stdout or "").strip()
                code, _, ctype = meta.partition(" ")
                if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    body = tmp.read_bytes()
                    if expect_image and not valid_image_bytes(body):
                        last_err = f"wayback returned HTML/text for image ({len(body)}b, {ctype})"
                    else:
                        os.replace(tmp, dest)
                        return True, ctype.strip(), ""
                else:
                    err = (p.stderr or "").strip().splitlines()
                    last_err = err[-1] if err else f"curl exit {p.returncode} http={code}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            elapsed = time.monotonic() - t0
            if elapsed >= budget:
                break
            # exponential backoff capped, + jitter; keep total within budget
            delay = min(2.0 ** min(attempt, 5), 12.0) + random.uniform(0, 1.0)
            delay = min(delay, max(0.0, budget - elapsed))
            sys.stderr.write(f"[proxy] retry {attempt} {cand.split('/web/')[-1][:80]} ... {last_err} -> sleep {delay:.1f}s ({elapsed:.0f}/{budget:.0f}s)\n")
            sys.stderr.flush()
            if delay > 0:
                time.sleep(delay)
        # next candidate gets remaining budget
    return False, "", last_err or "retry budget exhausted"


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniclipLocalProxy/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.address_string()}] {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. index
        if path in ("/", "/index.html"):
            body = rewrite_to_local(HTML_FILE.read_text(errors="ignore"))
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return

        # 2. css (rewritten too; relative /web/... already points here)
        if path == "/style.css":
            body = rewrite_to_local(CSS_FILE.read_text(errors="ignore"))
            self._send(200, body.encode("utf-8"), "text/css; charset=utf-8")
            return

        # 3. wayback proxy: /web/<ts>... (query preserved for wayback fetch)
        if path.startswith("/web/"):
            # normalize images to the same im_ structure before cache lookup/fetch
            norm_path = force_im_proxy_path(parsed.path) if is_image_path(parsed.path) else parsed.path
            norm_qs = ("?" + parsed.query) if parsed.query else ""
            wayback_url = WAYBACK_HTTP + norm_path + norm_qs  # http, never https
            dest = local_path_for_proxy(norm_path)
            expect_image = is_image_path(norm_path)

            def _serve_file(raw: bytes):
                # HTML pages: rewrite archive refs to localhost so the browser
                # stays local (never talks to wayback directly)
                head = raw.lstrip()[:64].lower()
                if not expect_image and (head.startswith(b"<!doctype") or head.startswith(b"<html")):
                    try:
                        return rewrite_to_local(raw.decode("utf-8", errors="ignore")).encode("utf-8"), \
                            "text/html; charset=utf-8"
                    except Exception:
                        pass
                ctype, _ = mimetypes.guess_type(str(dest))
                if expect_image and dest.suffix.lower() == ".svg":
                    ctype = "image/svg+xml"
                if not expect_image and not ctype:
                    ctype = "text/html; charset=utf-8"
                return raw, ctype or "application/octet-stream"

            if dest.exists() and dest.stat().st_size > 0:
                try:
                    raw = dest.read_bytes()
                except OSError:
                    raw = b""
                if raw and (not expect_image or valid_image_bytes(raw[:4096] + b"x" * 100)):
                    body, ctype = _serve_file(raw)
                    self._send(200, body, ctype)
                    return
                # corrupt cache entry: refetch below
                try:
                    dest.unlink()
                except OSError:
                    pass
            # cache miss: fetch server-side with ~60s retry budget + im_/id_ fallback
            ok, ctype, err = fetch_via_curl(wayback_url, dest, expect_image=expect_image, budget=60.0)
            if ok:
                body, ctype2 = _serve_file(dest.read_bytes())
                self._send(200, body, ctype2)
                return
            self._send(502, f"Bad Gateway: wayback fetch failed: {err}".encode(), "text/plain")
            return

        # 4. health
        if path == "/healthz":
            self._send(200, b"ok", "text/plain")
            return

        self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving {BASE_DIR} at {LOCAL_PREFIX}/ (proxying wayback via {WAYBACK_HTTP}, curl={CURL_BIN})")
    print("Browser only talks to localhost; wayback is contacted server-side only.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
