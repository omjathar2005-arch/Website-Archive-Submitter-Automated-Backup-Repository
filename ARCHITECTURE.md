# Technical Architecture

```
 Browser dashboard (templates/index.html, polls JSON every 2 s)
        |  REST/JSON
 Flask app (app.py) ----------------------------------------------+
   |  starts scan threads            |  enqueue / retry / export    |
   v                                 v                              |
 discovery.py                    worker.py  (1 thread per service slot)
   robots.txt, sitemaps,           claims 'pending' rows, calls archivers.py,
   HTML crawl, status check        retries with backoff, cooldown on 429/CAPTCHA
        \                              /
         +----------  SQLite (db.py, WAL)  ----------+
              domains | urls | submissions | scans
```

## Data model (db.py)
- **domains**: one project per website; options (crawl budget, external links, JS), pause flag, scan status, timestamps.
- **urls**: `UNIQUE(domain_id, normalized_url)`. Keeps `original_url`, `normalized_url`, discovery `source`,
  `discovered_at`, `first_scan_id` / `last_seen_scan_id` (new-vs-known detection), `http_status`, `final_url` (redirects),
  `error`, and `crawl_state` (the persistent crawl frontier).
- **submissions**: one row per archive attempt chain: service, status (pending/running/success/failed), queued/started/finished/
  last-attempt times, attempts, `next_attempt_at`, `archive_url`, `archive_id`, `error`, `duration`. Many rows per URL = history.
- **scans**: per scan statistics (pages crawled, URLs seen, new URLs, seconds) -> crawl speed and incremental-scan reporting.

## Discovery (discovery.py)
1. robots.txt: read `Sitemap:` lines and use the rules to skip disallowed pages.
2. Sitemaps: `/sitemap.xml`, `/sitemap_index.xml` and robots-listed sitemaps; indexes are followed recursively; `.gz` supported.
   Sitemap URLs are recorded without downloading each page.
3. HTML crawl from the start URL: `<a href>`, `rel=canonical`, `rel=next/prev` pagination, RSS/Atom feed links.
   Same-site only (www and non-www are the same site); external links are only recorded when the option is on and are never crawled.
4. Normalisation: lower-case host, remove fragments and default ports, drop `utm_*`/`fbclid`/`gclid`, sort query parameters.
5. Status pass: HEAD (GET fallback) with 8 threads records HTTP status and redirect targets. 4xx/5xx URLs are not queued.
6. Optional Playwright rendering for thin pages (JS-built sites).
Each page is fetched at most once per scan. The frontier lives in the database, so a crashed scan resumes where it stopped.

## Queue and workers (worker.py)
- The queue is the `submissions` table. Workers claim the oldest pending row of the domain served least recently
  (fair combined queue across domains), skipping paused domains and rows waiting for `next_attempt_at`.
- Temporary failures -> back to pending with exponential backoff (30 s, 60 s, 120 s ... max 15 min). After `MAX_ATTEMPTS` -> failed,
  and the reason stays visible. A failure never stops other URLs.
- Rate limits: fixed delay per service, and a service-wide cooldown after HTTP 429 or a CAPTCHA response.
- Crash recovery: on start-up `recover()` returns 'running' submissions to pending and marks running scans as interrupted.

## Incremental behaviour
- Queue only creates submissions for URLs with no submission for that service, so repeated clicks do not duplicate work.
- A re-scan updates `last_seen_scan_id`; URLs whose `first_scan_id` equals the new scan are the newly discovered ones.
- "Re-archive" adds new submissions (history is never overwritten).

## Scaling notes
SQLite + indexes, batched inserts (transactions), no in-memory crawl state, workers per service. For hundreds of thousands of URLs
swap SQLite for PostgreSQL and run workers as separate processes; the claim query is the only coupling point.
