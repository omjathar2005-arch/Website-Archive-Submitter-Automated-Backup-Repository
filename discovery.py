"""URL discovery: robots.txt, sitemaps (+indexes, gzip), HTML links, canonical,
pagination, feeds, optional JavaScript rendering. All state lives in SQLite so a
scan can be resumed after a crash."""
import gzip
import os
import re
import time
import urllib.parse as up
import urllib.robotparser as robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

import db

UA = "WebsiteArchiveSubmitter/1.0 (student project; polite crawler)"
HEADERS = {"User-Agent": UA}
CRAWL_DELAY = float(os.environ.get("CRAWL_DELAY", "0.2"))
MAX_HTML_BYTES = 2_000_000
SKIP_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".json",
    ".zip", ".gz", ".rar", ".7z", ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2",
    ".ttf", ".eot", ".exe", ".dmg", ".pdf", ".xml", ".rss",
}
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid"}


# --------------------------------------------------------------------------- URL helpers
def normalize(url, base=None):
    """Lower-case host, drop fragment/default port/tracking params, sort query. None if unusable."""
    try:
        if base:
            url = up.urljoin(base, url)
        p = up.urlsplit(url.strip())
        if p.scheme.lower() not in ("http", "https") or not p.hostname:
            return None
        host = p.hostname.lower()
        port = p.port
        default = (p.scheme.lower() == "http" and port == 80) or (p.scheme.lower() == "https" and port == 443)
        netloc = host if port is None or default else f"{host}:{port}"
        query = [
            (k, v) for k, v in up.parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS
        ]
        query.sort()
        return up.urlunsplit((p.scheme.lower(), netloc, p.path or "/", up.urlencode(query), ""))
    except ValueError:
        return None


def site_key(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url, key):
    return site_key(up.urlsplit(url).hostname) == key


def skip_ext(url):
    path = up.urlsplit(url).path.lower()
    return any(path.endswith(e) for e in SKIP_EXT)


def parse_domain_input(text):
    """'example.com' or 'https://www.example.com/blog' -> (site_key, normalized start url)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter a domain or URL")
    if "://" not in text:
        text = "https://" + text
    start = normalize(text)
    if not start:
        raise ValueError("That does not look like a valid website address")
    return site_key(up.urlsplit(start).hostname), start


# --------------------------------------------------------------------------- DB helpers
def add_url(domain_id, original, normalized, source, scan_id, crawl=False):
    """Insert a URL once per domain. Returns True if it is new to the repository."""
    c = db.conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO urls(domain_id, original_url, normalized_url, source, discovered_at,"
        " first_scan_id, last_seen_scan_id) VALUES (?,?,?,?,?,?,?)",
        (domain_id, original, normalized, source, db.now(), scan_id, scan_id),
    )
    is_new = cur.rowcount == 1
    if not is_new:
        c.execute(
            "UPDATE urls SET last_seen_scan_id=? WHERE domain_id=? AND normalized_url=?",
            (scan_id, domain_id, normalized),
        )
    if crawl:
        c.execute(
            "UPDATE urls SET crawl_state='todo' WHERE domain_id=? AND normalized_url=? AND crawl_state IS NULL",
            (domain_id, normalized),
        )
    return is_new


def _set_msg(domain_id, status, message):
    db.conn().execute("UPDATE domains SET scan_status=?, scan_message=? WHERE id=?", (status, message, domain_id))


# --------------------------------------------------------------------------- sitemaps / robots
def _loc(tag):
    return tag.split("}")[-1].lower() == "loc"


def _fetch_xml(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return None
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_sitemap(data):
    root = ET.fromstring(data)
    is_index = root.tag.split("}")[-1].lower() == "sitemapindex"
    locs = [e.text.strip() for e in root.iter() if _loc(e.tag) and e.text]
    return is_index, locs


def load_robots(start_url):
    """Returns (RobotFileParser or None, [sitemap urls listed in robots.txt])."""
    p = up.urlsplit(start_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = requests.get(robots_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None, []
        lines = r.text.splitlines()
        rp = robotparser.RobotFileParser()
        rp.parse(lines)
        sitemaps = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
        return rp, sitemaps
    except requests.RequestException:
        return None, []


def discover_sitemaps(domain_id, key, start_url, robots_sitemaps, scan_id):
    """Walk sitemap indexes recursively. Sitemap URLs are recorded, not downloaded as pages."""
    p = up.urlsplit(start_url)
    base = f"{p.scheme}://{p.netloc}"
    queue = [(u, "robots_sitemap") for u in robots_sitemaps]
    queue += [(base + "/sitemap.xml", "sitemap"), (base + "/sitemap_index.xml", "sitemap")]
    seen, found = set(), 0
    c = db.conn()
    while queue and len(seen) < 200:
        sm_url, src = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)
        try:
            data = _fetch_xml(sm_url)
            if not data:
                continue
            is_index, locs = parse_sitemap(data)
        except (requests.RequestException, ET.ParseError, OSError, EOFError):
            continue
        if is_index:
            queue += [(u, src) for u in locs]
            continue
        c.execute("BEGIN")
        try:
            for loc in locs:
                n = normalize(loc)
                if n and same_site(n, key) and not skip_ext(n):
                    add_url(domain_id, loc, n, src, scan_id)
                    found += 1
        finally:
            c.execute("COMMIT")
    return found


# --------------------------------------------------------------------------- HTML + JS rendering
def extract_links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel") or []).lower()
        out.append((a["href"], "pagination" if ("next" in rel or "prev" in rel) else "html_link"))
    for l in soup.find_all("link", href=True):
        rel = " ".join(l.get("rel") or []).lower()
        typ = (l.get("type") or "").lower()
        if "canonical" in rel:
            out.append((l["href"], "canonical"))
        elif "next" in rel or "prev" in rel:
            out.append((l["href"], "pagination"))
        elif "alternate" in rel and ("rss" in typ or "atom" in typ):
            out.append((l["href"], "feed"))
    return out


class Renderer:
    """Optional headless-browser renderer (Playwright) for JavaScript-built pages."""

    def __init__(self):
        self.ok, self.error, self.pw, self.browser = False, None, None, None
        try:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch()
            self.ok = True
        except Exception as e:  # not installed / browsers missing
            self.error = str(e)[:200]

    def render(self, url):
        page = self.browser.new_page(user_agent=UA)
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        finally:
            page.close()

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- status check
def _probe(row):
    url = row["normalized_url"]
    try:
        r = requests.head(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code in (403, 405, 501):
            r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True, stream=True)
            r.close()
        final = r.url if normalize(r.url) != url else None
        return row["id"], r.status_code, final, None
    except requests.RequestException as e:
        return row["id"], None, None, f"{type(e).__name__}: {str(e)[:150]}"


def check_statuses(domain_id, scan_id, scan_started):
    """HEAD every URL seen in this scan that has not been checked yet (records status + redirects)."""
    c = db.conn()
    while True:
        rows = c.execute(
            "SELECT id, normalized_url FROM urls WHERE domain_id=? AND last_seen_scan_id=? "
            "AND (last_checked IS NULL OR last_checked < ?) AND crawl_state IS NOT 'blocked' LIMIT 200",
            (domain_id, scan_id, scan_started),
        ).fetchall()
        if not rows:
            return
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(_probe, rows))
        c.execute("BEGIN")
        for uid, status, final, err in results:
            c.execute(
                "UPDATE urls SET http_status=?, final_url=?, error=?, last_checked=? WHERE id=?",
                (status, final, err, db.now(), uid),
            )
        c.execute("COMMIT")


# --------------------------------------------------------------------------- main scan
def run_scan(domain_id, resume=False):
    c = db.conn()
    d = c.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if not d:
        return
    key = site_key(up.urlsplit(d["start_url"]).hostname)
    t0 = time.time()
    scan = None
    if resume:
        scan = c.execute(
            "SELECT * FROM scans WHERE domain_id=? AND status='interrupted' ORDER BY id DESC LIMIT 1", (domain_id,)
        ).fetchone()
    if scan:
        scan_id, scan_started, crawled = scan["id"], scan["started_at"], scan["pages_crawled"]
        c.execute("UPDATE scans SET status='running' WHERE id=?", (scan_id,))
    else:
        c.execute("UPDATE scans SET status='abandoned' WHERE domain_id=? AND status='interrupted'", (domain_id,))
        scan_started = db.now()
        scan_id = c.execute(
            "INSERT INTO scans(domain_id, started_at, status) VALUES (?,?, 'running')", (domain_id, scan_started)
        ).lastrowid
        c.execute("UPDATE urls SET crawl_state=NULL WHERE domain_id=?", (domain_id,))
        crawled = 0

    renderer = None
    try:
        _set_msg(domain_id, "scanning", "Reading robots.txt and sitemaps")
        robots, robots_sitemaps = load_robots(d["start_url"])
        discover_sitemaps(domain_id, key, d["start_url"], robots_sitemaps, scan_id)
        add_url(domain_id, d["start_url"], d["start_url"], "seed", scan_id, crawl=True)

        if d["use_js"]:
            renderer = Renderer()
            if not renderer.ok:
                _set_msg(domain_id, "scanning", f"JS rendering unavailable ({renderer.error}); continuing without it")

        session = requests.Session()
        session.headers.update(HEADERS)
        while crawled < d["max_pages"]:
            row = c.execute(
                "SELECT id, normalized_url FROM urls WHERE domain_id=? AND crawl_state='todo' ORDER BY id LIMIT 1",
                (domain_id,),
            ).fetchone()
            if not row:
                break
            url = row["normalized_url"]
            if not same_site(url, key) or skip_ext(url):
                c.execute("UPDATE urls SET crawl_state='done' WHERE id=?", (row["id"],))
                continue
            if robots and not robots.can_fetch(UA, url):
                c.execute(
                    "UPDATE urls SET crawl_state='blocked', error='blocked by robots.txt', last_checked=? WHERE id=?",
                    (db.now(), row["id"]),
                )
                continue

            _set_msg(domain_id, "scanning", f"Crawling ({crawled + 1}/{d['max_pages']}): {url[:80]}")
            status, final, err, links, base = None, None, None, [], url
            try:
                r = session.get(url, timeout=20, allow_redirects=True)
                status = r.status_code
                base = r.url
                if normalize(r.url) != url:
                    final = r.url
                if status < 400 and "html" in r.headers.get("Content-Type", "").lower():
                    html = r.text[:MAX_HTML_BYTES]
                    links = extract_links(html, base)
                    if renderer and renderer.ok and len(links) < 3:  # thin page => probably JS-built
                        try:
                            links += [(h, "js_rendered") for h, _ in extract_links(renderer.render(url), base)]
                        except Exception as e:
                            err = f"render failed: {str(e)[:100]}"
                    if final:
                        n = normalize(final)
                        if n and same_site(n, key):
                            add_url(domain_id, final, n, "html_link", scan_id)
            except requests.RequestException as e:
                err = f"{type(e).__name__}: {str(e)[:150]}"

            c.execute("BEGIN")
            for href, src in links:
                n = normalize(href, base)
                if not n:
                    continue
                if same_site(n, key):
                    add_url(domain_id, href, n, src, scan_id, crawl=(src != "feed"))
                elif d["include_external"]:
                    add_url(domain_id, href, n, "external_link", scan_id)
            c.execute(
                "UPDATE urls SET crawl_state='done', http_status=?, final_url=?, error=?, last_checked=? WHERE id=?",
                (status, final, err, db.now(), row["id"]),
            )
            c.execute("COMMIT")
            crawled += 1
            c.execute("UPDATE scans SET pages_crawled=? WHERE id=?", (crawled, scan_id))
            time.sleep(CRAWL_DELAY)

        _set_msg(domain_id, "scanning", "Checking HTTP status of discovered URLs")
        check_statuses(domain_id, scan_id, scan_started)

        found = c.execute("SELECT COUNT(*) FROM urls WHERE domain_id=? AND last_seen_scan_id=?", (domain_id, scan_id)).fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM urls WHERE domain_id=? AND first_scan_id=?", (domain_id, scan_id)).fetchone()[0]
        c.execute(
            "UPDATE scans SET status='completed', finished_at=?, pages_crawled=?, urls_found=?, new_urls=?, seconds=? WHERE id=?",
            (db.now(), crawled, found, new, round(time.time() - t0, 2), scan_id),
        )
        c.execute("UPDATE domains SET last_scan_at=?, scan_status='idle', scan_message=? WHERE id=?",
                  (db.now(), f"Last scan: {found} URLs seen, {new} new", domain_id))
    except Exception as e:  # never let a scan thread die silently
        c.execute("UPDATE scans SET status='failed', message=?, finished_at=? WHERE id=?", (str(e)[:300], db.now(), scan_id))
        _set_msg(domain_id, "failed", f"Scan failed: {str(e)[:200]}")
    finally:
        if renderer:
            renderer.close()
