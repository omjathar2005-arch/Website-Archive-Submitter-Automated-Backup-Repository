"""Archive service integrations.

Rules followed here (per the assignment): no CAPTCHA bypassing, no authentication
bypassing, and rate limits are respected (fixed delay between submissions, backoff on
HTTP 429, and a service-wide cooldown when a service asks us to slow down).
"""
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

UA = "WebsiteArchiveSubmitter/1.0 (student project)"
HEADERS = {"User-Agent": UA}
SNAPSHOT_RE = re.compile(r"/web/(\d{14})/")
ARCHIVE_TODAY_RE = re.compile(
    r"^https?://archive\.[a-z]+/(?!submit|wip|newest|search|online)([A-Za-z0-9]{4,8})/?$"
)


@dataclass
class Result:
    ok: bool
    archive_url: Optional[str] = None
    archive_id: Optional[str] = None
    error: Optional[str] = None
    retry: bool = False        # temporary failure -> worker retries with backoff
    cooldown: int = 0          # seconds the whole service should pause (rate limit / CAPTCHA)


# --------------------------------------------------------------------------- Wayback Machine
def wayback(url):
    """Internet Archive 'Save Page Now'. Uses the authenticated SPN2 API when IA_ACCESS_KEY and
    IA_SECRET_KEY (free from https://archive.org/account/s3.php) are set, otherwise the public
    https://web.archive.org/save/<url> endpoint."""
    access, secret = os.environ.get("IA_ACCESS_KEY"), os.environ.get("IA_SECRET_KEY")
    if access and secret:
        return _wayback_spn2(url, access, secret)
    try:
        r = requests.get("https://web.archive.org/save/" + url, headers=HEADERS, timeout=120, allow_redirects=True)
    except requests.RequestException as e:
        return Result(False, error=f"network error: {type(e).__name__}: {str(e)[:120]}", retry=True)

    if r.status_code == 429:
        return Result(False, error="HTTP 429: rate limited by Wayback Machine", retry=True, cooldown=300)
    match = SNAPSHOT_RE.search(r.url) or SNAPSHOT_RE.search(r.headers.get("Content-Location", ""))
    if r.status_code == 200 and match:
        ts = match.group(1)
        return Result(True, archive_url=f"https://web.archive.org/web/{ts}/{url}", archive_id=ts)
    retry = r.status_code >= 500 or r.status_code == 200  # 200 without snapshot = queued/busy
    return Result(False, error=f"HTTP {r.status_code}: no snapshot link returned", retry=retry)


def _wayback_spn2(url, access, secret):
    h = {"User-Agent": UA, "Accept": "application/json", "Authorization": f"LOW {access}:{secret}"}
    try:
        r = requests.post("https://web.archive.org/save", headers=h, data={"url": url, "capture_all": "1"}, timeout=60)
        if r.status_code == 429:
            return Result(False, error="HTTP 429: SPN2 rate limit", retry=True, cooldown=300)
        job = r.json().get("job_id") if r.status_code == 200 else None
        if not job:
            return Result(False, error=f"SPN2 rejected request (HTTP {r.status_code}): {r.text[:150]}", retry=r.status_code >= 500)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(5)
            s = requests.get(f"https://web.archive.org/save/status/{job}", headers=h, timeout=30).json()
            if s.get("status") == "success":
                ts = s["timestamp"]
                return Result(True, f"https://web.archive.org/web/{ts}/{s.get('original_url', url)}", ts)
            if s.get("status") == "error":
                return Result(False, error=f"SPN2: {s.get('message') or s.get('status_ext')}", retry=True)
        return Result(False, error="SPN2 job timed out", retry=True)
    except (requests.RequestException, ValueError) as e:
        return Result(False, error=f"SPN2 error: {type(e).__name__}: {str(e)[:120]}", retry=True)


# --------------------------------------------------------------------------- archive.today
def archive_today(url):
    """archive.today has no official API and shows a CAPTCHA to most automated clients.
    We try the plain submit form once; if a CAPTCHA / block appears we stop and put the whole
    service on cooldown. We never try to solve or evade it - submit those URLs manually."""
    host = os.environ.get("ARCHIVE_TODAY_HOST", "archive.ph")
    try:
        r = requests.post(f"https://{host}/submit/", data={"url": url, "anyway": "1"},
                          headers=HEADERS, timeout=90, allow_redirects=True)
    except requests.RequestException as e:
        return Result(False, error=f"network error: {type(e).__name__}: {str(e)[:120]}", retry=True)

    if r.status_code == 429:
        return Result(False, error="HTTP 429: rate limited by archive.today", retry=True, cooldown=900)
    body = r.text[:6000].lower()
    if r.status_code == 403 or "captcha" in body or "g-recaptcha" in body:
        return Result(False, error="archive.today requires a CAPTCHA for automated clients - submit manually (not bypassed)",
                      retry=False, cooldown=1800)
    candidates = [r.url, r.headers.get("Refresh", "").split("url=")[-1].strip(), r.headers.get("Location", "")]
    for cand in candidates:
        m = ARCHIVE_TODAY_RE.match(cand or "")
        if m:
            return Result(True, archive_url=cand, archive_id=m.group(1))
    return Result(False, error=f"HTTP {r.status_code}: no archive link found in response", retry=r.status_code >= 500)


# --------------------------------------------------------------------------- mock (testing only)
def mock(url):
    """Simulated service for demos/tests. Produces mock:// links so it can never be mistaken for a real proof."""
    time.sleep(random.uniform(0.2, 0.8))
    roll = random.random()
    if roll < 0.10:
        return Result(False, error="simulated temporary failure", retry=True)
    if roll < 0.14:
        return Result(False, error="simulated permanent failure (HTTP 403)", retry=False)
    aid = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
    return Result(True, archive_url=f"mock://archive/{aid}", archive_id=aid)


SERVICES = {"wayback": wayback, "archive_today": archive_today, "mock": mock}
LABELS = {"wayback": "Wayback Machine", "archive_today": "archive.today", "mock": "Mock (test only)"}
# seconds to wait between two submissions to the same service
DELAYS = {
    "wayback": float(os.environ.get("WAYBACK_DELAY", "6")),
    "archive_today": float(os.environ.get("ARCHIVE_TODAY_DELAY", "15")),
    "mock": 0.1,
}
WORKERS = {"wayback": 1, "archive_today": 1, "mock": 3}
