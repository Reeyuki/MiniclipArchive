#!/usr/bin/env python3
"""
Playwright-browser capture downloader for Wayback images.
- Extracts every image URL from index.html + style.css (http, not https)
- Launches a real Chromium browser and lets IT capture each image via page.goto
- Retry mechanism with exponential backoff, validation, skip-existing, manifest
"""
import hashlib
import json
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).parent
HTML_FILE = BASE_DIR / "index.html"
CSS_FILE = BASE_DIR / "style.css"
OUT_DIR = BASE_DIR / "downloaded_images"
MANIFEST = OUT_DIR / "manifest-playwright.json"

MAX_RETRIES = 5
BASE_DELAY = 1.5
PAGE_TIMEOUT = 30000
CHROMIUM_PATH = "/usr/bin/chromium"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")
WAYBACK_RE = re.compile(r"/web/(\d+)([a-z_]*)/(.*)", re.IGNORECASE)


def to_http(u: str) -> str:
    u = u.strip().strip("\"'")
    if u.startswith("/web/"):
        return "http://web.archive.org" + u
    if u.startswith("//"):
        return "http:" + u
    if u.startswith("https://web.archive.org/"):
        return "http://" + u[len("https://"):]
    return u


def is_wayback_image(u: str) -> bool:
    if "web.archive.org" not in u:
        return False
    if "/web/" not in u:
        return False
    if re.search(r"/web/\d+if_/", u):
        return False
    low = u.lower().split("?")[0].split("#")[0]
    return low.endswith(IMAGE_EXTS)


def canonical_key(u: str):
    m = WAYBACK_RE.search(u)
    if not m:
        return ("", u)
    return (m.group(1), m.group(3).lower())


def force_im(u: str) -> str:
    """Force raw-image modifier (im_) so Wayback returns bytes, not HTML wrapper."""
    m = WAYBACK_RE.search(u)
    if not m:
        return u
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    base = u.split("/web/")[0]  # http://web.archive.org
    return f"{base}/web/{ts}im_/{orig}"


def prefer_im(urls):
    best = {}
    for u in urls:
        k = canonical_key(u)
        m = WAYBACK_RE.search(u)
        mod = m.group(2) if m else ""
        score = 1 if "im_" in mod else 0
        if k not in best or score > best[k][1]:
            best[k] = (u, score)
    # force im_ for every fetch URL
    return sorted(force_im(v[0]) for v in best.values())


def extract_urls():
    html = HTML_FILE.read_text(errors="ignore")
    css = CSS_FILE.read_text(errors="ignore") if CSS_FILE.exists() else ""
    img_attrs = re.findall(r'''(?:src|data-src)\s*=\s*["']([^"']+)["']''', html, re.IGNORECASE)
    css_urls = re.findall(r"""url\(\s*["']?([^"'\)]+)["']?\s*\)""", css)
    raw = [to_http(u) for u in img_attrs + css_urls]
    return prefer_im(sorted(set(u for u in raw if is_wayback_image(u))))


def local_path_for(url: str) -> Path:
    m = WAYBACK_RE.search(url)
    if not m:
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        return OUT_DIR / "misc" / f"{h}.bin"
    ts, mod, orig = m.group(1), m.group(2), m.group(3)
    if "://" not in orig:
        orig = "http://" + orig
    parsed = urllib.parse.urlparse(orig)
    host = parsed.netloc or "unknown-host"
    path = parsed.path.lstrip("/") or "index"
    return OUT_DIR / ts / host / path


def valid_image(body: bytes, url: str) -> bool:
    if not body:
        return False
    head = body.lstrip()[:32].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return False
    low = url.lower().split("?")[0]
    if low.endswith(".svg"):
        txt = body.lstrip()[:500].lower()
        return txt.startswith(b"<svg") or txt.startswith(b"<?xml") or b"<svg" in txt[:500]
    if body.startswith(b"\xff\xd8\xff"):
        return True
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return True
    if body.startswith(b"RIFF") and b"WEBP" in body[:16]:
        return True
    if body.startswith(b"\x00\x00\x01\x00"):
        return True
    if len(body) < 100:
        return False
    # accept sizeable non-html blobs (e.g. some png variants)
    return True


def main(limit: int = 0):
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urls = extract_urls()
    if limit:
        urls = urls[:limit]
    print(f"Found {len(urls)} unique wayback image URLs (http)")

    results = []
    ok = skip = fail = 0
    t0 = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROMIUM_PATH,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (playwright-capture)")
        page = context.new_page()

        for i, url in enumerate(urls, 1):
            dest = local_path_for(url)
            # skip-existing
            if dest.exists() and dest.stat().st_size > 0:
                try:
                    if valid_image(dest.read_bytes()[:2048] + b"", str(dest)):
                        # full check on existing file
                        body = dest.read_bytes()
                        if valid_image(body, url):
                            results.append({"url": url, "file": str(dest.relative_to(BASE_DIR)),
                                            "status": "skipped-exists", "bytes": dest.stat().st_size, "attempts": 0})
                            skip += 1
                            if i % 25 == 0 or i == len(urls):
                                print(f"[{i}/{len(urls)}] ok={ok} skip={skip} fail={fail}", flush=True)
                            continue
                except OSError:
                    pass

            last_err = ""
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = page.goto(url, wait_until="commit", timeout=PAGE_TIMEOUT)
                    if resp is None:
                        raise RuntimeError("no response (navigation failed)")
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    body = resp.body()
                    if not valid_image(body, url):
                        ctype = ""
                        try:
                            ctype = resp.headers.get("content-type", "")
                        except Exception:
                            pass
                        raise ValueError(f"invalid image body ({len(body)} bytes, Content-Type={ctype})")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part") if dest.suffix else Path(str(dest) + ".part")
                    tmp.write_bytes(body)
                    tmp.replace(dest)
                    results.append({"url": url, "file": str(dest.relative_to(BASE_DIR)),
                                    "status": "ok", "bytes": len(body), "attempts": attempt})
                    ok += 1
                    success = True
                    break
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    if attempt == MAX_RETRIES:
                        break
                    delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    print(f"  [retry {attempt}/{MAX_RETRIES}] {url.split('/web/')[-1][:90]} ... {last_err} -> sleep {delay:.1f}s", flush=True)
                    time.sleep(delay)
            if not success:
                results.append({"url": url, "file": None, "status": "failed", "error": last_err, "attempts": MAX_RETRIES})
                fail += 1
            if i % 25 == 0 or i == len(urls):
                print(f"[{i}/{len(urls)}] ok={ok} skip={skip} fail={fail}", flush=True)

        browser.close()

    dt = time.time() - t0
    total_bytes = sum(r.get("bytes", 0) for r in results if r.get("bytes"))
    MANIFEST.write_text(json.dumps({"summary": {"total": len(urls), "ok": ok, "skipped": skip,
                                                "failed": fail, "total_bytes": total_bytes,
                                                "seconds": round(dt, 1)}, "results": results}, indent=2))
    failed = [r for r in results if r["status"] == "failed"]
    if failed:
        (OUT_DIR / "failed-playwright.log").write_text("\n".join(r["url"] + " <= " + r.get("error", "") for r in failed))
    print(f"\nDone in {dt:.1f}s. ok={ok} skipped={skip} failed={fail} bytes={total_bytes}")
    print(f"Manifest: {MANIFEST}")
    return 1 if failed else 0


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sys.exit(main(lim))
