"""Persistent submission queue + background workers.

The queue IS the `submissions` table (status = pending). Nothing is held only in memory, so a
crash/restart loses nothing: `recover()` puts interrupted rows back to pending."""
import os
import random
import threading
import time

import archivers
import db

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))
stop_event = threading.Event()
cooldowns = {}      # service -> unix time until which the service is paused
live = {}           # service -> human readable worker state (for the dashboard)

CLAIM_SQL = """
SELECT s.id, s.attempts, u.normalized_url, u.domain_id
FROM submissions s
JOIN urls u    ON u.id = s.url_id
JOIN domains d ON d.id = u.domain_id
WHERE s.status = 'pending' AND s.service = ? AND d.paused = 0
  AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= ?)
ORDER BY COALESCE(d.last_submit_at, ''), s.id   -- rotate between domains = combined fair queue
LIMIT 1
"""


def backoff(attempts):
    return min(30 * 2 ** (attempts - 1), 900) + random.randint(0, 5)


def recover():
    """Run once at start-up: anything that was mid-flight when the process died goes back in the queue."""
    c = db.conn()
    c.execute("UPDATE submissions SET status='pending', error='recovered after restart' WHERE status='running'")
    c.execute("UPDATE scans SET status='interrupted' WHERE status='running'")
    c.execute("UPDATE domains SET scan_status='interrupted', scan_message='Scan was interrupted - press Resume scan' "
              "WHERE scan_status='scanning'")


def enqueue(domain_id, services, rearchive=False):
    """Create pending submissions. Default: only URLs this tool never submitted to that service.
    rearchive=True: also URLs that already have a successful snapshot (history is kept)."""
    c = db.conn()
    total = 0
    for service in services:
        if service not in archivers.SERVICES:
            continue
        if rearchive:
            guard = "NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id=u.id AND s.service=? AND s.status IN ('pending','running'))"
        else:
            guard = "NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id=u.id AND s.service=?)"
        cur = c.execute(
            f"INSERT INTO submissions(url_id, service, status, queued_at) "
            f"SELECT u.id, ?, 'pending', ? FROM urls u WHERE u.domain_id=? "
            f"AND (u.http_status IS NULL OR u.http_status < 400) AND u.crawl_state IS NOT 'blocked' AND {guard}",
            (service, db.now(), domain_id, service),
        )
        total += cur.rowcount
    return total


def retry_failed(domain_id=None):
    c = db.conn()
    sql = ("UPDATE submissions SET status='pending', attempts=0, next_attempt_at=NULL "
           "WHERE status='failed'")
    args = []
    if domain_id:
        sql += " AND url_id IN (SELECT id FROM urls WHERE domain_id=?)"
        args.append(domain_id)
    return c.execute(sql, args).rowcount


def _finish(c, sub, res, duration):
    now = db.now()
    attempts = sub["attempts"] + 1
    if res.ok:
        c.execute(
            "UPDATE submissions SET status='success', finished_at=?, attempts=?, archive_url=?, archive_id=?, "
            "error=NULL, duration=?, next_attempt_at=NULL WHERE id=?",
            (now, attempts, res.archive_url, res.archive_id, duration, sub["id"]),
        )
    elif res.retry and attempts < MAX_ATTEMPTS:
        wait = backoff(attempts)
        nxt = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + wait))
        c.execute(
            "UPDATE submissions SET status='pending', attempts=?, error=?, duration=?, next_attempt_at=? WHERE id=?",
            (attempts, f"{res.error} (retry {attempts}/{MAX_ATTEMPTS - 1} in {wait}s)", duration, nxt, sub["id"]),
        )
    else:
        c.execute(
            "UPDATE submissions SET status='failed', finished_at=?, attempts=?, error=?, duration=? WHERE id=?",
            (now, attempts, res.error, duration, sub["id"]),
        )


def service_loop(service, idx):
    name = f"{service}#{idx}"
    fn, delay = archivers.SERVICES[service], archivers.DELAYS[service]
    c = db.conn()
    while not stop_event.is_set():
        wait = cooldowns.get(service, 0) - time.time()
        if wait > 0:
            live[name] = f"cooling down {int(wait)}s (service asked us to slow down)"
            stop_event.wait(2)
            continue
        sub = c.execute(CLAIM_SQL, (service, db.now())).fetchone()
        if not sub:
            live[name] = "idle"
            stop_event.wait(2)
            continue
        claimed = c.execute(
            "UPDATE submissions SET status='running', started_at=?, last_attempt_at=? WHERE id=? AND status='pending'",
            (db.now(), db.now(), sub["id"]),
        ).rowcount
        if not claimed:
            continue
        c.execute("UPDATE domains SET last_submit_at=? WHERE id=?", (db.now(), sub["domain_id"]))
        live[name] = f"submitting {sub['normalized_url'][:70]}"
        t0 = time.time()
        try:
            res = fn(sub["normalized_url"])
        except Exception as e:
            res = archivers.Result(False, error=f"unexpected error: {type(e).__name__}: {str(e)[:150]}", retry=True)
        _finish(c, sub, res, round(time.time() - t0, 2))
        if res.cooldown:
            cooldowns[service] = time.time() + res.cooldown
        stop_event.wait(delay)


def start_workers():
    recover()
    for service in archivers.SERVICES:
        for i in range(archivers.WORKERS.get(service, 1)):
            threading.Thread(target=service_loop, args=(service, i), daemon=True, name=f"worker-{service}-{i}").start()
