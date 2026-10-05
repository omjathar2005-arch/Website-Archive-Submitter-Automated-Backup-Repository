"""Tiny local website for demoing/testing the archiver without touching any real site.
Run:  python demo_site.py            (serves http://127.0.0.1:9000)
Visit http://127.0.0.1:9000/grow to add 20 new pages (used to demo the 'second scan finds only new URLs' step)."""
from flask import Flask, Response, redirect

app = Flask(__name__)
COUNT = {"n": 40}
PER_PAGE = 10


def shell(title, body, head=""):
    return f"<!doctype html><html><head><title>{title}</title>{head}</head><body><h1>{title}</h1>{body}</body></html>"


@app.get("/")
def home():
    return list_page(1)


@app.get("/list/<int:p>")
def list_page(p):
    start, end = (p - 1) * PER_PAGE + 1, min(p * PER_PAGE, COUNT["n"])
    links = "".join(f'<li><a href="/post/{i}">Post {i}</a></li>' for i in range(start, end + 1))
    pages = -(-COUNT["n"] // PER_PAGE)
    nav = (f'<a rel="prev" href="/list/{p-1}">prev</a> ' if p > 1 else "") + (f'<a rel="next" href="/list/{p+1}">next</a>' if p < pages else "")
    extra = '<a href="/missing-page">broken link</a> <a href="/old">redirecting link</a> <a href="/about#team">About</a> <a href="/about?utm_source=x">About (tracked)</a>'
    head = f'<link rel="canonical" href="http://127.0.0.1:9000/list/{p}"><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    return shell(f"Posts page {p}", f"<ul>{links}</ul>{nav}<p>{extra}</p>", head)


@app.get("/post/<int:i>")
def post(i):
    if i > COUNT["n"]:
        return "not found", 404
    return shell(f"Post {i}", '<a href="/">home</a>')


@app.get("/about")
def about():
    return shell("About", '<a href="/">home</a>')


@app.get("/old")
def old():
    return redirect("/about", 301)


@app.get("/feed.xml")
def feed():
    return Response("<rss version='2.0'><channel><title>demo</title></channel></rss>", mimetype="application/rss+xml")


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: http://127.0.0.1:9000/sitemap.xml\n", mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>http://127.0.0.1:9000/post/{i}</loc></url>" for i in range(1, COUNT["n"] + 1))
    return Response(f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', mimetype="application/xml")


@app.get("/grow")
def grow():
    COUNT["n"] += 20
    return f"Site now has {COUNT['n']} posts. Run a new scan in the dashboard."


if __name__ == "__main__":
    app.run(port=9000, threaded=True)
