# Website Archive Submitter & Automated Backup Repository

A Python/Flask tool that discovers the public URLs of one or more websites, submits them to web-archiving
services through a persistent queue, and keeps a searchable history of everything it did.

## Setup (5 minutes)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                     # dashboard at http://127.0.0.1:8000
```

Optional:
- **Better Wayback reliability:** create free keys at https://archive.org/account/s3.php and set
  `IA_ACCESS_KEY` and `IA_SECRET_KEY` before `python app.py`. The tool then uses the official Save Page Now 2 API.
- **JavaScript rendering bonus:** `pip install playwright` then `playwright install chromium`, and tick
  "Render JavaScript pages" when adding a site.
- Settings (environment variables): `PORT`, `ARCHIVER_DB`, `CRAWL_DELAY` (0.2 s), `WAYBACK_DELAY` (6 s),
  `ARCHIVE_TODAY_DELAY` (15 s), `MAX_ATTEMPTS` (4).

## Demo script (covers the "Mandatory Demonstration" list)

Use the bundled demo site so the demo is fast and repeatable, then show one real small site.

1. Terminal A: `python demo_site.py` (serves http://127.0.0.1:9000, 40 posts, sitemap, robots.txt, a broken link, a redirect).
2. Terminal B: `python app.py`, open http://127.0.0.1:8000.
3. **Add one domain:** enter `http://127.0.0.1:9000` -> discovery starts automatically.
4. **Show the inventory:** "Websites" row shows total URLs; the Repository table lists each URL, HTTP status, and the
   discovery source (sitemap, html_link, canonical, pagination, feed). Filter "Broken URL" to show the 404.
5. **Create the queue and submit:** tick **Mock (test only)** (the demo site is on localhost, which real archives cannot
   reach), press **Queue**. Watch Queued / Success / Failed and the worker panel update live. Some mock submissions fail
   on purpose, so you can show retries with backoff and the permanent failures (use **Retry failed**).
6. **Show archive URLs:** Repository table -> "Archive link"; **History** shows every attempt for a URL.
7. **Interrupt and resume:** while the queue is running press Ctrl+C in Terminal B, run `python app.py` again.
   Pending items are still there; items that were mid-flight are put back in the queue automatically.
   (A scan can be interrupted the same way: the site shows "interrupted" and **Resume scan** continues from the saved frontier.)
8. **Second scan:** open http://127.0.0.1:9000/grow (adds 20 posts), press **Scan** again.
   The "Recent scans" table shows the new-URL count; press **Queue** and only the new URLs are submitted.
   Tick "Re-archive..." and Queue to create additional snapshots (history keeps all of them).
9. **Multi-domain:** add a second site (e.g. a small real site, or `http://localhost:9000` which counts as a different host).
   Each has its own statistics, pause/resume, and queue; the workers serve all domains from one combined queue,
   rotating between domains.
10. **Real archiving:** add a small site you own or have permission to archive, tick **Wayback Machine**, press **Queue**.
    Wayback is slow by design (one request every ~6 s), so keep the demo site small.

## Honest limitations

- **archive.today** has no official API and shows a CAPTCHA to most scripts. The tool tries the public submit form; when it gets a
  CAPTCHA/429 it records the reason, pauses that service, and never tries to bypass it (as the assignment requires).
- **Wayback anonymous mode** can be rate limited (HTTP 429). The tool backs off and pauses the service automatically.
- **Mock** snapshots use `mock://` links and are for testing only; they are not real proof of archive.
- JavaScript rendering is optional and only used for "thin" pages (fewer than 3 links in the raw HTML).
  The Instagram bonus is not implemented: Instagram requires login for most content and its terms forbid scraping.
- Single machine, SQLite, in-process workers (fine for thousands to ~100k URLs; see ARCHITECTURE.md for scaling).

## Deploy to Render

This app uses continuous background worker threads and a local SQLite database, so deploy it as a persistent web service rather than a serverless function. The included `render.yaml` configures a Render web service with a 1 GB persistent disk at `/var/data`, one Gunicorn worker, and HTTP Basic Authentication.

1. Push this project to a GitHub repository. Do not commit `venv/`, `.env`, or `archiver.db`; `.gitignore` excludes them.
2. In Render, create a Blueprint from that repository and review the service plan and disk cost before confirming deployment. A persistent disk requires a paid Render web service.
3. Set `ARCHIVER_USERNAME` and `ARCHIVER_PASSWORD` when Render prompts for the two secret environment variables. The production app returns an error if those credentials are missing.
4. Open the service URL and enter those credentials in the browser's authentication prompt.

The SQLite database and queue survive service restarts on the mounted disk. Keep this as a single instance: SQLite plus in-process worker threads is not configured for horizontal scaling. The dashboard can crawl websites and submit URLs, so keep its credentials private. Wayback API credentials can also be added as `IA_ACCESS_KEY` and `IA_SECRET_KEY` in Render's environment settings.
