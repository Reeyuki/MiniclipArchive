#!/usr/bin/env python3
"""
Reliable Wayback image downloader with retry mechanism (curl-subprocess based).
- Extracts every image URL from index.html (<img src/data-src>) and style.css (url(...))
  that points to web.archive.org
- Downloads by spawning `curl` subprocess per attempt, with exponential-backoff
  retries, timeout, validation, resume/skip-existing
- Concurrent workers, manifest + failure log
"""
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_FILE = BASE_DIR / "index.html"
CSS_FILE = BASE_DIR / "style.css"
OUT_DIR = BASE_DIR / "downloaded_images"
MANIFEST = OUT_DIR / "manifest.json"

MAX_RETRIES = 5
TIMEOUT = 30
WORKERS = 8
BASE_DELAY = 1.5  # seconds, exponential
CURL_BIN = shutil.which("curl") or "curl"
USER_AGENT = "Mozilla/5.0 (wayback-image-archiver; curl-retry-enabled)"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")

# Wayback wrapper pattern: /web/<timestamp>[mod_]/<original-url>
WAYBACK_RE = re.compile(r"/web/(\d+)([a-z_]*)/(.*)", re.IGNORECASE)


def to_http(u: str) -> str:
    u = u.strip().strip("\"'")
    if u.startswith("/web/"):
        return "http://web.archive.org" + u
    if u.startswith("//"):
        return "http:" + u
    # force http (not https) for wayback - port 443 is blocked, port 80 works
    if u.startswith("https://web.archive.org/"):
        return "http://" + u[len("https://"):]
    return u


def is_wayback_image(u: str) -> bool:
    if "web.archive.org" not in u:
        return False
    if "/web/" not in u:
        return False
    # exclude iframe embeds
    if "/web/" in u and re.search(r"/web/\d+if_/", u):
        return False
    low = u.lower().split("?")[0].split("#")[0]
    return low.endswith(IMAGE_EXTS)


def canonical_key(u: str):
    """Dedupe key: (timestamp, original-url-lower). Prefers im_ variant."""
    m = WAYBACK_RE.search(u)
    if not m:
        return ("", u)
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    # strip scheme differences for key
    return (ts, orig.lower())


def force_im(u: str) -> str:
    """Force raw-image modifier (im_) so Wayback returns bytes, not HTML wrapper."""
    m = WAYBACK_RE.search(u)
    if not m:
        return u
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    base = u.split("/web/")[0]
    return f"{base}/web/{ts}im_/{orig}"


def prefer_im(urls):
    """For same (timestamp, original), keep the im_ variant if present."""
    best = {}
    for u in urls:
        k = canonical_key(u)
        m = WAYBACK_RE.search(u)
        mod = m.group(2) if m else ""
        score = 1 if "im_" in mod else 0
        if k not in best or score > best[k][1]:
            best[k] = (u, score)
    return sorted(force_im(v[0]) for v in best.values())


def extract_urls():
    html = HTML_FILE.read_text(errors="ignore")
    css = CSS_FILE.read_text(errors="ignore") if CSS_FILE.exists() else ""
    img_attrs = re.findall(r'''(?:src|data-src)\s*=\s*["']([^"']+)["']''', html, re.IGNORECASE)
    css_urls = re.findall(r"""url\(\s*["']?([^"'\)]+)["']?\s*\)""", css)
    raw = [to_http(u) for u in img_attrs + css_urls]
    imgs = sorted(set(u for u in raw if is_wayback_image(u)))
    # dedupe im_ vs plain duplicates
    imgs = prefer_im(imgs)
    return imgs


def local_path_for(url: str) -> Path:
    """Map wayback URL -> downloaded_images/<timestamp>/<host>/<path>"""
    m = WAYBACK_RE.search(url)
    if not m:
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        return OUT_DIR / "misc" / f"{h}.bin"
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    # orig like http://static.miniclipcdn.com/... or https://...
    if "://" not in orig:
        orig = "http://" + orig
    parsed = urllib.parse.urlparse(orig)
    host = parsed.netloc or "unknown-host"
    path = parsed.path.lstrip("/") or "index"
    # keep query in filename if it affects variant (?v9 etc for fonts already excluded)
    # sanitize
    full = OUT_DIR / ts / host / path
    # ensure parent has no trailing dot issues
    return full


MAGIC = [
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp?"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"<svg", "svg"),
    (b"<?xml", "svg?"),
]


def looks_like_image(head: bytes, url: str, content_type: str) -> bool:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("image/"):
            return True
        if "text/html" in ct:
            return False
    low = url.lower().split("?")[0]
    if low.endswith(".svg"):
        # svg is text
        txt = head[:500].lstrip().lower()
        return txt.startswith(b"<svg") or txt.startswith(b"<?xml") or b"<svg" in txt[:500]
    for magic, _ in MAGIC:
        if head.startswith(magic):
            return True
    # RIFF....WEBP
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return True
    return False


def curl_download(url: str, tmp: Path):
    """Spawn curl subprocess for a single attempt. Returns (http_code, content_type).

    Raises RuntimeError on curl failure / HTTP error.
    Uses --fail so HTTP >=400 exits non-zero.
    """
    cmd = [
        CURL_BIN,
        "-sS",           # silent but show errors
        "-L",            # follow redirects (wayback redirects a lot)
        "--fail",        # non-zero exit on HTTP >= 400
        "-A", USER_AGENT,
        "--connect-timeout", "15",
        "--max-time", str(max(TIMEOUT, 60)),
        "--retry", "0",  # we do our own retry/backoff
        "-o", str(tmp),
        "-w", "%{http_code} %{content_type}",
        "--", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(TIMEOUT, 60) + 10)
    meta = (proc.stdout or "").strip()  # e.g. "200 image/jpeg"
    http_code, _, content_type = meta.partition(" ")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        err = err[-1] if err else f"curl exit {proc.returncode}"
        raise RuntimeError(f"curl exit {proc.returncode} http={http_code or '?'}: {err}")
    return http_code.strip(), content_type.strip()


def download_one(url: str):
    dest = local_path_for(url)
    tmp = dest.with_suffix(dest.suffix + ".part") if dest.suffix else Path(str(dest) + ".part")
    # resume/skip: if dest exists and looks valid, skip
    if dest.exists() and dest.stat().st_size > 0:
        # quick magic check
        try:
            with open(dest, "rb") as f:
                head = f.read(32)
            # accept if non-empty and not obviously html
            if not head.lstrip().lower().startswith(b"<!doctype") and not head.lstrip().lower().startswith(b"<html"):
                return {"url": url, "file": str(dest.relative_to(BASE_DIR)), "status": "skipped-exists", "bytes": dest.stat().st_size, "attempts": 0}
        except OSError:
            pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            http_code, ctype = curl_download(url, tmp)
            if http_code not in ("200",):
                raise RuntimeError(f"unexpected HTTP {http_code} (Content-Type={ctype})")
            total = tmp.stat().st_size if tmp.exists() else 0
            if total == 0:
                raise ValueError("empty body (0 bytes)")
            with open(tmp, "rb") as f:
                head = f.read(512)
            if head.lstrip().lower().startswith(b"<!doctype") or head.lstrip().lower().startswith(b"<html"):
                raise ValueError(f"got HTML page instead of image (Content-Type={ctype})")
            if not looks_like_image(head, url, ctype):
                # still accept if sizeable and content-type missing? be strict for small files
                if total < 100:
                    raise ValueError(f"failed magic validation (Content-Type={ctype}, {total} bytes)")
            os.replace(tmp, dest)
            return {"url": url, "file": str(dest.relative_to(BASE_DIR)), "status": "ok", "bytes": total, "attempts": attempt, "content_type": ctype}
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            # cleanup partial
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if attempt == MAX_RETRIES:
                break
            # exponential backoff with jitter
            delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  [retry {attempt}/{MAX_RETRIES}] {url.split('/web/')[-1][:90]} ... {last_err} -> sleep {delay:.1f}s", flush=True)
            time.sleep(delay)
    return {"url": url, "file": None, "status": "failed", "error": last_err, "attempts": MAX_RETRIES}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urls = extract_urls()
    print(f"Found {len(urls)} unique wayback image URLs (from index.html + style.css)")
    results = []
    ok = fail = skip = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(download_one, u): u for u in urls}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if r["status"] == "ok":
                ok += 1
            elif r["status"].startswith("skipped"):
                skip += 1
            else:
                fail += 1
            if i % 25 == 0 or i == len(urls):
                print(f"[{i}/{len(urls)}] ok={ok} skip={skip} fail={fail}", flush=True)
    dt = time.time() - t0
    total_bytes = sum(r.get("bytes", 0) for r in results if r.get("bytes"))
    summary = {"total": len(urls), "ok": ok, "skipped": skip, "failed": fail,
               "total_bytes": total_bytes, "seconds": round(dt, 1)}
    MANIFEST.write_text(json.dumps({"summary": summary, "results": sorted(results, key=lambda r: r["url"])}, indent=2))
    failed = [r for r in results if r["status"] == "failed"]
    if failed:
        (OUT_DIR / "failed.log").write_text("\n".join(r["url"] + "  <=  " + r.get("error", "") for r in failed))
    print(f"\nDone in {dt:.1f}s. ok={ok} skipped={skip} failed={fail} bytes={total_bytes}")
    print(f"Manifest: {MANIFEST}")
    if failed:
        print(f"Failed list: {OUT_DIR / 'failed.log'} ({len(failed)} URLs)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
