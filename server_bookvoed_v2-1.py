#!/usr/bin/env python3
import json, re, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8")) if (ROOT / "sources.json").exists() else {"opds": []}
UA = "VisualKeiBookshelf/0.3 (+human-facing book discovery)"


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/atom+xml, application/xml, text/xml, */*", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()


def clean_isbn(value):
    s = re.sub(r"[^0-9Xx]", "", value or "").upper()
    return s if len(s) in (10, 13) else ""


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).strip()


def tokens(s):
    return [x for x in re.findall(r"[\w-]+", norm(s), flags=re.UNICODE) if len(x) > 1]


def looks_isbn(q):
    return clean_isbn(q) != ""


def google_query(q):
    url = "https://www.googleapis.com/books/v1/volumes?maxResults=40&printType=books&q=" + urllib.parse.quote(q)
    data, _ = http_get(url)
    j = json.loads(data)
    out = []
    for v in j.get("items", []):
        x = v.get("volumeInfo", {})
        ids = x.get("industryIdentifiers", []) or []
        isbn = next((z.get("identifier", "") for z in ids if z.get("identifier")), "")
        links = x.get("imageLinks") or {}
        cover = links.get("thumbnail") or links.get("smallThumbnail") or ""
        cover = cover.replace("http:", "https:")
        out.append({
            "title": x.get("title", ""),
            "author": ", ".join(x.get("authors", [])),
            "isbn": clean_isbn(isbn),
            "cover": cover,
            "source": "Google Books",
            "language": x.get("language", ""),
            "published": x.get("publishedDate", ""),
            "publisher": x.get("publisher", ""),
            "id": v.get("id", ""),
        })
    return out


def google(q):
    queries = []
    isbn = clean_isbn(q)
    if isbn:
        queries.append("isbn:" + isbn)
    else:
        # Exact-ish title search first, then ordinary search. Google Books supports
        # fielded queries such as intitle/inauthor; we intentionally do not force
        # langRestrict because it can hide Russian records that are indexed poorly.
        queries.extend([
            'intitle:"' + q.replace('"', ' ') + '"',
            q,
        ])
    out, seen = [], set()
    for query in queries:
        try:
            for x in google_query(query):
                key = (x.get("id") or "", norm(x.get("title")), norm(x.get("author")), x.get("isbn", ""))
                if key in seen:
                    continue
                seen.add(key); out.append(x)
        except Exception:
            continue
    return out


def openlibrary_query(params):
    params = {**params, "limit": 50, "fields": "title,author_name,author_key,cover_i,isbn,language,edition_key,first_publish_year,publisher,subject"}
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params, doseq=True)
    data, _ = http_get(url)
    return json.loads(data)


def openlibrary(q):
    isbn = clean_isbn(q)
    variants = []
    if isbn:
        variants.append({"isbn": isbn})
    else:
        variants.append({"title": q, "lang": "ru"})
        variants.append({"q": 'title:"' + q.replace('"', ' ') + '"', "lang": "ru"})
        variants.append({"q": q, "lang": "ru"})
        # Do not exclude other languages; lang=ru is only a preference according to
        # Open Library's API documentation.
        variants.append({"q": q})
    out, seen = [], set()
    for params in variants:
        try:
            j = openlibrary_query(params)
            for x in j.get("docs", []):
                authors = ", ".join(x.get("author_name", []) or [])
                isbns = x.get("isbn", []) or []
                isbn1 = clean_isbn(isbns[0]) if isbns else ""
                cover = f"https://covers.openlibrary.org/b/id/{x['cover_i']}-M.jpg" if x.get("cover_i") else ""
                languages = x.get("language", []) or []
                subjects = x.get("subject", []) or []
                key = (norm(x.get("title")), norm(authors), isbn1)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "title": x.get("title", ""),
                    "author": authors,
                    "isbn": isbn1,
                    "cover": cover,
                    "source": "Open Library",
                    "language": languages,
                    "published": str(x.get("first_publish_year", "") or ""),
                    "publisher": ", ".join(x.get("publisher", [])[:3] or []),
                    "subjects": subjects[:20],
                    "edition_key": (x.get("edition_key") or [""])[0],
                })
        except Exception:
            continue
    return out


ATOM = "http://www.w3.org/2005/Atom"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"


def opds_search(root_url, q, source_name):
    raw, _ = http_get(root_url)
    root = ET.fromstring(raw)
    search_url = None
    for link in root.findall(".//{%s}link" % ATOM):
        if link.attrib.get("rel") == "search" and "opensearchdescription+xml" in link.attrib.get("type", ""):
            search_url = urllib.parse.urljoin(root_url, link.attrib.get("href", "")); break
    if not search_url:
        return []
    raw, _ = http_get(search_url)
    desc = ET.fromstring(raw)
    template = None
    for u in desc.findall("{%s}Url" % OPENSEARCH):
        t = u.attrib.get("template", "")
        if "{searchTerms}" in t:
            template = t; break
    if not template:
        return []
    url = template.replace("{searchTerms}", urllib.parse.quote(q))
    raw, _ = http_get(urllib.parse.urljoin(root_url, url))
    feed = ET.fromstring(raw)
    out = []
    for e in feed.findall("{%s}entry" % ATOM):
        title = (e.findtext("{%s}title" % ATOM) or "").strip()
        author = (e.findtext("{%s}author/{%s}name" % (ATOM, ATOM)) or "").strip()
        ident = (e.findtext("{%s}identifier" % "http://purl.org/dc/elements/1.1/") or "").strip()
        cover = ""
        for l in e.findall("{%s}link" % ATOM):
            rel = l.attrib.get("rel", "")
            typ = l.attrib.get("type", "")
            if "image" in rel or typ.startswith("image/"):
                cover = urllib.parse.urljoin(root_url, l.attrib.get("href", "")); break
        if title:
            out.append({"title": title, "author": author, "isbn": clean_isbn(ident), "cover": cover, "source": source_name + " (OPDS)"})
    return out


def opds(q):
    out = []
    for item in SOURCES.get("opds", []):
        if isinstance(item, list) and len(item) >= 2:
            name, url = item[0], item[1]
        elif isinstance(item, dict):
            name, url = item.get("name", "OPDS"), item.get("url", "")
        else:
            continue
        if not url:
            continue
        try: out.extend(opds_search(url, q, name))
        except Exception: continue
    return out




def labirint_ids(q, limit=5):
    """Find Labirint product ids for a title/author/ISBN query.
    Labirint exposes book pages with stable numeric ids; the product gallery
    contains a front cover and often an angled photo (ph_001.jpg) showing the
    real spine. We use the gallery URL as an image source rather than drawing
    a synthetic spine.
    """
    q = (q or "").strip()
    if not q:
        return []
    url = "https://www.labirint.ru/search/" + urllib.parse.quote(q, safe="") + "/"
    try:
        raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=12)
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    ids = []
    for m in re.finditer(r'href=[\"\']/books/(\d+)/[\"\']', text):
        ident = m.group(1)
        if ident not in ids:
            ids.append(ident)
            if len(ids) >= limit:
                break
    return ids


def labirint_spine_for(q, isbn="", title="", author=""):
    queries = []
    if isbn:
        queries.append(isbn)
    if title and author:
        queries.append(f"{title} {author}")
    if title:
        queries.append(title)
    if q and q not in queries:
        queries.append(q)
    for query in queries:
        for ident in labirint_ids(query, limit=5):
            base = f"https://imo10.labirint.ru/books/{ident}/"
            return {
                "labirint_id": ident,
                "labirint_cover": base + "cover.jpg/242-0",
                "spine": base + "ph_001.jpg/242-0",
                "labirint_url": f"https://www.labirint.ru/books/{ident}/",
            }
    return {}


def enrich_labirint(results, original_q):
    # Only enrich the best few results. This keeps the search responsive and
    # avoids hammering the bookstore when a broad query returns many records.
    ranked = sorted(results, key=lambda z: z.get("_score", 0), reverse=True)[:8]
    for x in ranked:
        try:
            hit = labirint_spine_for(original_q, x.get("isbn", ""), x.get("title", ""), x.get("author", ""))
            if hit:
                x.update(hit)
        except Exception:
            continue
    return results


def bookvoed_search(q, limit=8):
    """Find actual Bookvoed book pages.

    Bookvoed's public /search page is a mixed shop search (books + stationery +
    gifts), and its query parameters are not stable. Therefore we first try
    the public search, but keep only candidates that look like the requested
    book. If that search is unhelpful, use a normal web search restricted to
    bookvoed.ru/product pages, then fetch those product pages for metadata.
    """
    q = (q or "").strip()
    if not q:
        return []

    def relevant(title, author, isbn=""):
        qn = norm(q)
        tn = norm(title)
        an = norm(author)
        if not tn and not an:
            return False
        if looks_isbn(q):
            return clean_isbn(q) == clean_isbn(isbn)
        qt = set(tokens(q))
        if not qt:
            return False
        title_tokens = set(tokens(title))
        author_tokens = set(tokens(author))
        # Exact phrase / all title words is strongest.
        if qn and qn in tn:
            return True
        if qt <= title_tokens:
            return True
        # Author search: e.g. "Чехов" should match books whose author is Чехов.
        if qt & author_tokens:
            return len(qt & author_tokens) >= max(1, len(qt) // 2)
        overlap = len(qt & (title_tokens | author_tokens)) / max(1, len(qt))
        return overlap >= 0.75

    def parse_product(url):
        title = author = isbn = cover = ""
        page = ""
        try:
            raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=7)
            page = raw.decode("utf-8", errors="ignore")
            for jm in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, flags=re.I | re.S):
                try:
                    data = json.loads(jm.strip())
                    objs = data if isinstance(data, list) else [data]
                    for obj in objs:
                        if not isinstance(obj, dict):
                            continue
                        typ = str(obj.get("@type", ""))
                        if not title and obj.get("name"):
                            title = str(obj["name"]).strip()
                        if not author:
                            a = obj.get("author")
                            if isinstance(a, dict): author = str(a.get("name", "")).strip()
                            elif isinstance(a, list): author = ", ".join(str(v.get("name", "")) for v in a if isinstance(v, dict)).strip(", ")
                            elif isinstance(a, str): author = a.strip()
                        image = obj.get("image")
                        if not cover and image:
                            cover = image[0] if isinstance(image, list) else str(image)
                except Exception:
                    continue
            mi = re.search(r'ISBN[^0-9Xx]{0,60}([0-9Xx][0-9Xx\- ]{9,20})', page, re.I)
            if mi:
                isbn = clean_isbn(mi.group(1))
            if not author:
                ma = re.search(r'Автор[^<]{0,80}</[^>]+>\s*([^<]{2,120})', page, re.I)
                if ma:
                    author = re.sub(r'\s+', ' ', ma.group(1)).strip()
        except Exception:
            return None

        if not title:
            return None
        return {"title": title, "author": author, "isbn": isbn, "cover": cover, "bookvoed_url": url, "source": "Буквоед"}

    def collect_paths(html, max_paths=20):
        paths, seen = [], set()
        for m in re.finditer(r'href=["\'](/product/[^"\']+)["\']', html):
            path = m.group(1).split("#", 1)[0]
            if path not in seen:
                seen.add(path); paths.append(path)
                if len(paths) >= max_paths: break
        return paths

    # 1) Try Bookvoed's own search. It is deliberately filtered because the
    # shop also returns stationery and gifts for ordinary text searches.
    variants = [
        "https://www.bookvoed.ru/search?search=" + urllib.parse.quote(q),
        "https://www.bookvoed.ru/search?q=" + urllib.parse.quote(q),
        "https://www.bookvoed.ru/search?query=" + urllib.parse.quote(q),
    ]
    paths = []
    for url in variants:
        try:
            raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://www.bookvoed.ru/search"}, timeout=7)
            html = raw.decode("utf-8", errors="ignore")
            paths = collect_paths(html, 20)
            if paths:
                break
        except Exception:
            continue

    out, seen_urls = [], set()
    for path in paths:
        url = urllib.parse.urljoin("https://www.bookvoed.ru", path)
        item = parse_product(url)
        if not item or not relevant(item.get("title", ""), item.get("author", ""), item.get("isbn", "")):
            continue
        if url in seen_urls: continue
        seen_urls.add(url); out.append(item)
        if len(out) >= limit: return out

    # 2) Fallback: search-engine discovery restricted to Bookvoed product
    # pages. This is useful because the shop's own search is a mixed catalog.
    engine_queries = [
        'site:bookvoed.ru/product "' + q.replace('"', ' ') + '"',
        'site:bookvoed.ru/product ' + q,
    ]
    for eq in engine_queries:
        for engine_url in [
            "https://www.google.com/search?num=10&q=" + urllib.parse.quote(eq),
            "https://www.bing.com/search?count=10&q=" + urllib.parse.quote(eq),
        ]:
            try:
                raw, _ = http_get(engine_url, headers={"Accept": "text/html", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}, timeout=8)
                html = raw.decode("utf-8", errors="ignore")
                found = re.findall(r'https?://www\.bookvoed\.ru/product/[^"<>\s&]+', html, flags=re.I)
                for u in found:
                    u = u.replace("\\u0026", "&")
                    # Strip search-engine tracking fragments/parameters.
                    u = u.split("&", 1)[0]
                    if u in seen_urls: continue
                    item = parse_product(u)
                    if not item or not relevant(item.get("title", ""), item.get("author", ""), item.get("isbn", "")):
                        continue
                    seen_urls.add(u); out.append(item)
                    if len(out) >= limit: return out
            except Exception:
                continue
    return out

def language_score(x):
    lang = x.get("language", "")
    if isinstance(lang, list):
        lang = " ".join(lang)
    lang = str(lang).lower()
    if "rus" in lang or lang.strip() == "ru": return 25
    if "eng" in lang or lang.strip() == "en": return -8
    return 0


def score(q, x):
    qn, tn, an = norm(q), norm(x.get("title")), norm(x.get("author"))
    if not qn: return 0
    s = 0
    if qn == tn: s += 160
    elif qn in tn: s += 120
    qt, tt = set(tokens(q)), set(tokens(x.get("title")))
    if qt:
        overlap = len(qt & tt) / len(qt)
        s += round(overlap * 70)
        if qt <= tt: s += 35
    if qn in an: s += 35
    s += language_score(x)
    src = x.get("source", "")
    if "Open Library" in src: s += 2
    if any(w in (tn + " " + " ".join(x.get("subjects", []) or [])).lower() for w in ("fanfiction", "fan fiction", "фанфик", "фанфикшн")):
        s -= 80
    if not x.get("cover"): s -= 3
    return s


def dedup(results):
    seen = set(); out = []
    for x in sorted(results, key=lambda z: z["_score"], reverse=True):
        isbn = x.get("isbn", "")
        key = (isbn,) if isbn else (norm(x.get("title")), norm(x.get("author")))
        if key in seen: continue
        seen.add(key); x.pop("_score", None); out.append(x)
    return out[:40]


def search_diagnostic(q):
    """Search each catalog independently; one failure cannot stop the others."""
    q = (q or "").strip()
    if not q:
        return [], []

    allr, diagnostics = [], []

    def run_source(name, fn):
        try:
            rows = fn(q) or []
            for x in rows:
                x["_score"] = score(q, x)
            allr.extend(rows)
            diagnostics.append({"source": name, "count": len(rows), "status": "ok"})
        except Exception as e:
            diagnostics.append({
                "source": name,
                "count": 0,
                "status": "error",
                "error": str(e)[:300]
            })

    run_source("Google Books", google)
    run_source("Open Library", openlibrary)
    run_source("OPDS", opds)
    run_source("Лабиринт", lambda query: labirint_search(query, limit=8))
    run_source("Буквоед", lambda query: bookvoed_search(query, limit=8))
    return dedup(allr), diagnostics


def search(q):
    results, _ = search_diagnostic(q)
    return results


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/api/search":
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            try:
                body = json.dumps({"query": q, "results": search(q)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"query": q, "results": [], "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body)
            return
        if u.path == "/api/health":
            body = json.dumps({"ok": True, "sources": {"google": "enabled", "openlibrary": "enabled", "opds": len(SOURCES.get("opds", [])), "labirint": "enabled"}}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body); return
        return super().do_GET()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8765"))
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
