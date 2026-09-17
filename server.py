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


def http_probe(url, headers=None, timeout=12):
    """Low-level probe: expose the real HTTP status/error instead of turning it into zero results."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, application/xhtml+xml, text/html, */*", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return {
                "status_code": getattr(r, "status", 200),
                "content_type": r.headers.get_content_type(),
                "bytes": len(data),
                "final_url": r.geturl(),
                "sample": data[:180].decode("utf-8", errors="ignore").replace("\n", " ")[:180],
            }
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(500)
        except Exception:
            pass
        return {
            "status_code": e.code,
            "error_type": "HTTPError",
            "error": str(e)[:300],
            "sample": body.decode("utf-8", errors="ignore").replace("\n", " ")[:180],
        }
    except Exception as e:
        return {"status_code": None, "error_type": type(e).__name__, "error": str(e)[:300]}


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




def parse_jsonld_product(page):
    title = author = isbn = cover = ""
    for jm in re.findall(r'<script[^>]+type=["\']application/ld\\+json["\'][^>]*>(.*?)</script>', page, flags=re.I | re.S):
        try:
            data = json.loads(jm.strip())
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict): continue
                if not title and obj.get("name"): title = str(obj["name"]).strip()
                a = obj.get("author")
                if not author:
                    if isinstance(a, dict): author = str(a.get("name", "")).strip()
                    elif isinstance(a, list): author = ", ".join(str(v.get("name", "")) for v in a if isinstance(v, dict)).strip(", ")
                    elif isinstance(a, str): author = a.strip()
                if not isbn and obj.get("isbn"): isbn = clean_isbn(str(obj.get("isbn")))
                image = obj.get("image")
                if not cover and image: cover = image[0] if isinstance(image, list) else str(image)
        except Exception:
            continue
    return title, author, isbn, cover


def relevant_for_query(q, title, author="", isbn=""):
    if looks_isbn(q): return clean_isbn(q) == clean_isbn(isbn)
    qt=set(tokens(q)); tt=set(tokens(title)); at=set(tokens(author))
    if not qt: return False
    if norm(q) in norm(title): return True
    if qt <= tt: return True
    if qt <= at: return True
    return len(qt & (tt|at)) / len(qt) >= 0.75


def labirint_search(q, limit=8):
    q=(q or "").strip()
    if not q: return []
    urls=["https://www.labirint.ru/search/"+urllib.parse.quote(q, safe="")+"/"]
    paths=[]
    for url in urls:
        try:
            raw,_=http_get(url, headers={"Accept":"text/html,application/xhtml+xml","Accept-Language":"ru-RU,ru;q=0.9"}, timeout=10)
            html=raw.decode("utf-8",errors="ignore")
            for m in re.finditer(r'href=["\'](/books/\d+/)["\']',html):
                path=m.group(1)
                if path not in paths: paths.append(path)
                if len(paths)>=limit*2: break
            if paths: break
        except Exception: continue
    out=[]; seen=set()
    for path in paths:
        url=urllib.parse.urljoin("https://www.labirint.ru",path)
        try:
            raw,_=http_get(url,headers={"Accept":"text/html,application/xhtml+xml"},timeout=8)
            page=raw.decode("utf-8",errors="ignore")
            title,author,isbn,cover=parse_jsonld_product(page)
            if not title:
                m=re.search(r'<h1[^>]*>(.*?)</h1>',page,re.I|re.S)
                if m: title=re.sub(r'<[^>]+>',' ',m.group(1)); title=re.sub(r'\\s+',' ',title).strip()
            if not relevant_for_query(q,title,author,isbn): continue
            if not cover:
                ident=re.search(r'/books/(\\d+)/',path)
                if ident: cover=f"https://imo10.labirint.ru/books/{ident.group(1)}/cover.jpg/242-0"
            key=(clean_isbn(isbn),norm(title),norm(author))
            if key in seen: continue
            seen.add(key)
            out.append({"title":title,"author":author,"isbn":isbn,"cover":cover,"source":"Лабиринт","labirint_url":url})
            if len(out)>=limit: break
        except Exception: continue
    return out


def chitai_search(q, limit=8):
    q=(q or "").strip()
    if not q: return []
    url="https://www.chitai-gorod.ru/search?phrase="+urllib.parse.quote(q)
    try:
        raw,_=http_get(url,headers={"Accept":"text/html,application/xhtml+xml","Accept-Language":"ru-RU,ru;q=0.9"},timeout=12)
        html=raw.decode("utf-8",errors="ignore")
    except Exception:
        return []
    paths=[]
    for m in re.finditer(r'href=["\'](/product/[^"\']+)["\']',html):
        path=m.group(1).split("?",1)[0]
        if path not in paths: paths.append(path)
        if len(paths)>=limit*3: break
    out=[]; seen=set()
    for path in paths:
        url2=urllib.parse.urljoin("https://www.chitai-gorod.ru",path)
        try:
            raw,_=http_get(url2,headers={"Accept":"text/html,application/xhtml+xml"},timeout=8)
            page=raw.decode("utf-8",errors="ignore")
            title,author,isbn,cover=parse_jsonld_product(page)
            if not title:
                m=re.search(r'<h1[^>]*>(.*?)</h1>',page,re.I|re.S)
                if m: title=re.sub(r'<[^>]+>',' ',m.group(1)); title=re.sub(r'\\s+',' ',title).strip()
            if not relevant_for_query(q,title,author,isbn): continue
            key=(clean_isbn(isbn),norm(title),norm(author))
            if key in seen: continue
            seen.add(key)
            out.append({"title":title,"author":author,"isbn":isbn,"cover":cover,"source":"Читай-город","chitai_url":url2})
            if len(out)>=limit: break
        except Exception: continue
    return out


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
    """Search catalogs and expose low-level diagnostics for every source."""
    q = (q or "").strip()
    if not q:
        return [], []

    allr, diagnostics = [], []

    def run_source(name, fn, probe_url=None, headers=None):
        diag = {"source": name}
        if probe_url:
            diag["probe"] = http_probe(probe_url, headers=headers)
        try:
            rows = fn(q) or []
            for x in rows:
                x["_score"] = score(q, x)
            allr.extend(rows)
            diag["count"] = len(rows)
            diag["status"] = "ok" if rows else "empty"
        except Exception as e:
            diag["count"] = 0
            diag["status"] = "error"
            diag["error"] = str(e)[:300]
        diagnostics.append(diag)

    google_probe = "https://www.googleapis.com/books/v1/volumes?maxResults=3&printType=books&q=" + urllib.parse.quote(q)
    run_source("Google Books", google, google_probe)

    run_source("OPDS", opds)

    lab_probe = "https://www.labirint.ru/search/" + urllib.parse.quote(q, safe="") + "/"
    run_source("Лабиринт", lambda query: labirint_search(query, limit=8), lab_probe,
               {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ru-RU,ru;q=0.9"})

    chitai_probe = "https://www.chitai-gorod.ru/search?phrase=" + urllib.parse.quote(q)
    run_source("Читай-город", lambda query: chitai_search(query, limit=8), chitai_probe,
               {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ru-RU,ru;q=0.9"})

    return dedup(allr), diagnostics


def search(q):
    results, _ = search_diagnostic(q)
    return results


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/api/search", "/api/diagnostic"):
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            try:
                results, diagnostics = search_diagnostic(q)
                payload = {"query": q, "results": results, "diagnostics": diagnostics}
                if u.path == "/api/diagnostic":
                    payload["note"] = "Диагностика источников. Open Library намеренно отключён. Probe показывает реальный HTTP-ответ."
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"query": q, "results": [], "diagnostics": [], "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body)
            return
        if u.path == "/api/health":
            body = json.dumps({"ok": True, "sources": {"google": "enabled", "openlibrary": "disabled",
"chitai_gorod": "enabled", "opds": len(SOURCES.get("opds", [])), "labirint": "enabled"}}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body); return
        return super().do_GET()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8765"))
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
