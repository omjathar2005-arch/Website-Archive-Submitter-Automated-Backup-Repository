"""SQLite persistence layer. Every thread gets its own connection (autocommit mode, WAL)."""
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "ARCHIVER_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "archiver.db"),
)
_local = threading.local()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    domain           TEXT NOT NULL UNIQUE,
    start_url        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    max_pages        INTEGER NOT NULL DEFAULT 500,   -- crawl budget per scan
    include_external INTEGER NOT NULL DEFAULT 0,     -- record (never crawl) off-site links
    use_js           INTEGER NOT NULL DEFAULT 0,     -- render thin pages with Playwright
    paused           INTEGER NOT NULL DEFAULT 0,     -- paused domains are skipped by workers
    scan_status      TEXT NOT NULL DEFAULT 'idle',   -- idle|scanning|interrupted|failed
    scan_message     TEXT,
    last_scan_at     TEXT,
    last_submit_at   TEXT
);

CREATE TABLE IF NOT EXISTS urls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id        INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    original_url     TEXT NOT NULL,                  -- exactly as first discovered
    normalized_url   TEXT NOT NULL,
    source           TEXT NOT NULL,                  -- sitemap|robots_sitemap|html_link|canonical|pagination|feed|seed|js_rendered|external_link
    discovered_at    TEXT NOT NULL,
    first_scan_id    INTEGER,
    last_seen_scan_id INTEGER,
    http_status      INTEGER,
    final_url        TEXT,                           -- set when the URL redirected somewhere else
    error            TEXT,
    last_checked     TEXT,
    crawl_state      TEXT,                           -- NULL|todo|done  (persistent crawl frontier)
    UNIQUE(domain_id, normalized_url)
);
CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain_id);
CREATE INDEX IF NOT EXISTS idx_urls_crawl  ON urls(domain_id, crawl_state);

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id           INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    service          TEXT NOT NULL,                  -- wayback|archive_today|mock
    status           TEXT NOT NULL DEFAULT 'pending',-- pending|running|success|failed
    queued_at        TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    last_attempt_at  TEXT,
    next_attempt_at  TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    archive_url      TEXT,
    archive_id       TEXT,
    error            TEXT,
    duration         REAL
);
CREATE INDEX IF NOT EXISTS idx_sub_queue ON submissions(service, status);
CREATE INDEX IF NOT EXISTS idx_sub_url   ON submissions(url_id);

CREATE TABLE IF NOT EXISTS scans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id        INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running', -- running|completed|interrupted|failed|abandoned
    pages_crawled    INTEGER NOT NULL DEFAULT 0,
    urls_found       INTEGER NOT NULL DEFAULT 0,
    new_urls         INTEGER NOT NULL DEFAULT 0,
    seconds          REAL,
    message          TEXT
);
"""


def init_db():
    c = conn()
    c.executescript(SCHEMA)
