"""Website Archive Submitter - Flask web app (dashboard + JSON API)."""
import csv
import io
import os
import sqlite3
import threading
import time
import hmac

from flask import Flask, Response, jsonify, render_template, request

import archivers
import db
import discovery
import worker

app = Flask(__name__)
scan_threads = {}
STARTED = time.time()


@app.before_request
def require_dashboard_auth():
    """Protect the public dashboard and its state-changing API on hosted deployments."""
    if request.endpoint == "health":
        return None
    username = os.environ.get("ARCHIVER_USERNAME")
    password = os.environ.get("ARCHIVER_PASSWORD")
    if os.environ.get("APP_ENV") == "production" and (not username or not password):
        return jsonify(error="Dashboard credentials are not configured"), 503
    if username and password:
        auth = request.authorization
        if (not auth or not hmac.compare_digest(auth.username or "", username)
                or not hmac.compare_digest(auth.password or "", password)):
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="Website Archive Submitter"'})


@app.get("/health")
def health():
    return jsonify(ok=True)


def start_scan(domain_id):
    t = scan_threads.get(domain_id)
    if t and t.is_alive():
        return False
    fresh = request.args.get("fresh") == "1" if request else False
    resume = not fresh
    db.conn().execute("UPDATE domains SET scan_status='scanning', scan_message='Starting scan' WHERE id=?", (domain_id,))
    t = threading.Thread(target=discovery.run_scan, args=(domain_id, resume), daemon=True, name=f"scan-{domain_id}")
    scan_threads[domain_id] = t
    t.start()
    return True


def rows(sql, args=()):
    return [dict(r) for r in db.conn().execute(sql, args).fetchall()]


@app.get("/")
def index():
    return render_template("index.html", services=archivers.LABELS)


# ------------------------------------------------------------------ domains
@app.post("/api/domains")
def add_domain():
    data = request.get_json(force=True)
    try:
        key, start = discovery.parse_domain_input(data.get("url"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        cur = db.conn().execute(
            "INSERT INTO domains(domain, start_url, created_at, max_pages, include_external, use_js) VALUES (?,?,?,?,?,?)",
            (key, start, db.now(), max(1, int(data.get("max_pages") or 500)),
             1 if data.get("include_external") else 0, 1 if data.get("use_js") else 0),
        )
    except sqlite3.IntegrityError:
        return jsonify(error=f"{key} has already been added"), 409
    start_scan(cur.lastrowid)
    return jsonify(id=cur.lastrowid, domain=key)


@app.delete("/api/domains/<int:did>")
def delete_domain(did):
    db.conn().execute("DELETE FROM domains WHERE id=?", (did,))
    return jsonify(ok=True)


@app.post("/api/domains/<int:did>/scan")
def scan(did):
    return jsonify(started=start_scan(did))


@app.post("/api/domains/<int:did>/queue")
def queue(did):
    data = request.get_json(silent=True) or {}
    n = worker.enqueue(did, data.get("services") or ["wayback"], bool(data.get("rearchive")))
    return jsonify(queued=n)


@app.post("/api/queue-all")
def queue_all():
    data = request.get_json(silent=True) or {}
    total = sum(worker.enqueue(d["id"], data.get("services") or ["wayback"], bool(data.get("rearchive")))
                for d in rows("SELECT id FROM domains"))
    return jsonify(queued=total)


@app.post("/api/domains/<int:did>/pause")
def pause(did):
    db.conn().execute("UPDATE domains SET paused=1 WHERE id=?", (did,))
    return jsonify(ok=True)


@app.post("/api/domains/<int:did>/resume")
def resume(did):
    db.conn().execute("UPDATE domains SET paused=0 WHERE id=?", (did,))
    return jsonify(ok=True)


@app.post("/api/retry-failed")
def retry_failed():
    did = (request.get_json(silent=True) or {}).get("domain_id")
    return jsonify(requeued=worker.retry_failed(did))


# ------------------------------------------------------------------ stats / metrics
@app.get("/api/stats")
def stats():
    domains = rows("SELECT * FROM domains ORDER BY id")
    per = {d["id"]: {"total_urls": 0, "broken_urls": 0, "not_queued": 0, "pending": 0, "running": 0,
                     "success": 0, "failed": 0, "services": []} for d in domains}
    for r in rows("SELECT domain_id, COUNT(*) n, SUM(http_status>=400 OR (http_status IS NULL AND error IS NOT NULL)) b "
                  "FROM urls GROUP BY domain_id"):
        per[r["domain_id"]]["total_urls"], per[r["domain_id"]]["broken_urls"] = r["n"], r["b"] or 0
    for r in rows("SELECT domain_id, COUNT(*) n FROM urls u WHERE (http_status IS NULL OR http_status<400) "
                  "AND crawl_state IS NOT 'blocked' AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id=u.id) "
                  "GROUP BY domain_id"):
        per[r["domain_id"]]["not_queued"] = r["n"]
    for r in rows("SELECT u.domain_id, s.service, s.status, COUNT(*) n FROM submissions s "
                  "JOIN urls u ON u.id=s.url_id GROUP BY 1,2,3"):
        p = per[r["domain_id"]]
        p[r["status"]] += r["n"]
        if r["service"] not in p["services"]:
            p["services"].append(r["service"])
    for d in domains:
        d.update(per[d["id"]])
    totals = {k: sum(d[k] for d in domains) for k in
              ("total_urls", "broken_urls", "not_queued", "pending", "running", "success", "failed")}
    totals["domains"] = len(domains)
    return jsonify(domains=domains, totals=totals,
                   workers=dict(sorted(worker.live.items())),
                   cooldowns={s: int(t - time.time()) for s, t in worker.cooldowns.items() if t > time.time()})


@app.get("/api/metrics")
def metrics():
    ten_min_ago = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 600))
    per_service = rows(
        "SELECT service, SUM(status='success') ok, SUM(status='failed') failed, "
        "ROUND(AVG(CASE WHEN duration IS NOT NULL THEN duration END),2) avg_seconds, "
        "SUM(finished_at >= ? AND status='success') last10 FROM submissions GROUP BY service", (ten_min_ago,))
    for p in per_service:
        p["per_minute"] = round((p["last10"] or 0) / 10, 2)
    slow = rows("SELECT u.normalized_url url, s.service, s.duration FROM submissions s JOIN urls u ON u.id=s.url_id "
                "WHERE s.duration IS NOT NULL ORDER BY s.duration DESC LIMIT 5")
    scans = rows("SELECT s.*, d.domain, ROUND(s.pages_crawled*1.0/NULLIF(s.seconds,0),2) pages_per_sec "
                 "FROM scans s JOIN domains d ON d.id=s.domain_id ORDER BY s.id DESC LIMIT 10")
    return jsonify(per_service=per_service, slowest=slow, scans=scans)


# ------------------------------------------------------------------ repository search
def _url_query(args, select):
    where, params, join_args = ["1=1"], [], []
    service = args.get("service") or ""
    latest = "(SELECT MAX(id) FROM submissions WHERE url_id=u.id" + (" AND service=?)" if service else ")")
    if service:
        join_args.append(service)
    if args.get("domain_id"):
        where.append("u.domain_id=?")
        params.append(int(args["domain_id"]))
    if args.get("q"):
        where.append("(u.normalized_url LIKE ? OR d.domain LIKE ?)")
        params += [f"%{args['q']}%"] * 2
    status = args.get("status") or ""
    if status == "never":
        where.append("s.id IS NULL")
    elif status == "broken":
        where.append("(u.http_status>=400 OR (u.http_status IS NULL AND u.error IS NOT NULL))")
    elif status:
        where.append("s.status=?")
        params.append(status)
    sql = (f"{select} FROM urls u JOIN domains d ON d.id=u.domain_id "
           f"LEFT JOIN submissions s ON s.id={latest} WHERE {' AND '.join(where)}")
    return sql, join_args + params


@app.get("/api/urls")
def urls():
    page, per = max(1, int(request.args.get("page", 1))), 50
    sql, args = _url_query(request.args, "SELECT COUNT(*)")
    total = db.conn().execute(sql, args).fetchone()[0]
    sql, args = _url_query(
        request.args,
        "SELECT u.id, u.normalized_url, u.original_url, u.source, u.discovered_at, u.http_status, u.final_url, u.error, "
        "d.domain, s.service, s.status sub_status, s.archive_url, s.finished_at, s.error sub_error, "
        "(SELECT COUNT(*) FROM submissions WHERE url_id=u.id) n_subs")
    items = rows(sql + " ORDER BY u.id LIMIT ? OFFSET ?", args + [per, (page - 1) * per])
    return jsonify(items=items, total=total, page=page, pages=max(1, -(-total // per)))


@app.get("/api/urls/<int:uid>/history")
def history(uid):
    return jsonify(url=rows("SELECT u.*, d.domain FROM urls u JOIN domains d ON d.id=u.domain_id WHERE u.id=?", (uid,))[0],
                   submissions=rows("SELECT * FROM submissions WHERE url_id=? ORDER BY id DESC", (uid,)))


@app.post("/api/urls/<int:uid>/rearchive")
def rearchive_one(uid):
    service = (request.get_json(silent=True) or {}).get("service") or "wayback"
    if service not in archivers.SERVICES:
        return jsonify(error="unknown service"), 400
    db.conn().execute("INSERT INTO submissions(url_id, service, status, queued_at) VALUES (?,?,'pending',?)",
                      (uid, service, db.now()))
    return jsonify(ok=True)


@app.get("/api/export")
def export():
    sql = ("SELECT d.domain, u.original_url, u.normalized_url, u.source, u.discovered_at, u.http_status, u.final_url, "
           "s.service, s.started_at, s.finished_at submission_time, s.status, s.archive_url, s.archive_id, "
           "COALESCE(s.error, u.error) error, s.last_attempt_at FROM urls u JOIN domains d ON d.id=u.domain_id "
           "LEFT JOIN submissions s ON s.url_id=u.id")
    args = []
    if request.args.get("domain_id"):
        sql += " WHERE u.domain_id=?"
        args.append(int(request.args["domain_id"]))
    data = rows(sql + " ORDER BY u.id, s.id", args)
    if request.args.get("format") == "json":
        return jsonify(data)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(data[0].keys()) if data else ["domain"])
    w.writeheader()
    w.writerows(data)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=archive_inventory.csv"})


db.init_db()
worker.start_workers()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Open http://127.0.0.1:{port}")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, threaded=True, use_reloader=False)
