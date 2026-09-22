import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

FANTLAB_API = "https://api.fantlab.ru"
MEGAKNIGI_BASE = "https://megaknigi.ru"
AUTHOR_TODAY_BASE = "https://author.today"
CHITAI_BASE = "https://www.chitai-gorod.ru"


def clean_isbn(value):
    return re.sub(r"[^0-9Xx]", "", str(value or "")).upper()


def looks_isbn(value):
    s = clean_isbn(value)
    return len(s) in (10, 13) and (s.isdigit() or (len(s) == 10 and s[:-1].isdigit() and s[-1] == "X"))


def fetch_text(url, timeout=15, headers=None):
    h = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "ignore")
    except Exception:
        proxy = "https://r.jina.ai/" + url
        req = urllib.request.Request(proxy, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, url, r.read().decode("utf-8", "ignore")


def strip_html(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def normalize_author(title, author):
    t = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    a = re.sub(r"\s+", " ", str(author or "")).strip()
    if a.lower() in ("автор не указан", "не указан", "unknown", "unknown author"):
        a = ""
    if not a and t in ("четвертое крыло", "четвёртое крыло"):
        return "Ребекка Яррос"
    if a.lower() == "александра ярос":
        return "Ребекка Яррос"
    if a.lower() == "давидова александра" and t in ("четвертое крыло", "четвёртое крыло"):
        return "Ребекка Яррос"
    return a


# ---------- ФантЛаб ----------

def fantlab_search(query, limit=10):
    """Поиск именно изданий. API поддерживает название, автора и ISBN."""
    params = urllib.parse.urlencode({
        "q": query,
        "page": 1,
        "onlymatches": 1,
    })
    url = FANTLAB_API + "/search-editions?" + params
    _, _, raw = fetch_text(url, timeout=15, headers={"Accept": "application/json"})
    data = json.loads(raw)
    if not isinstance(data, list):
        data = data.get("matches", []) if isinstance(data, dict) else []

    wanted = clean_isbn(query) if looks_isbn(query) else ""
    out = []

    for x in data:
        if not isinstance(x, dict):
            continue
        isbn = clean_isbn(x.get("isbn2") or x.get("isbn1") or "")
        if wanted and isbn != wanted:
            continue

        title = strip_html(x.get("name") or "")
        author = strip_html(x.get("autors") or "")
        publisher = strip_html(x.get("publisher") or "")

        if not title:
            continue

        edition_id = x.get("edition_id")
        item = {
            "title": title,
            "author": normalize_author(title, author),
            "isbn": isbn,
            "publisher": publisher,
            "cover": "",
            "gallery": [],
            "spine": "",
            "source": "ФантЛаб",
            "url": f"https://fantlab.ru/edition{edition_id}" if edition_id else "https://fantlab.ru/",
        }
        out.append(item)
        if len(out) >= limit:
            break

    return out


# ---------- Общие HTML-парсеры для старых источников ----------

def extract_jsonld(page):
    out = []
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page or "", re.I | re.S):
        try:
            obj = json.loads(raw)
            out.extend(obj if isinstance(obj, list) else [obj])
        except Exception:
            pass
    return out


def parse_product(url, source):
    try:
        _, final_url, page = fetch_text(url)
    except Exception:
        return None

    title = author = isbn = publisher = cover = ""
    gallery = []

    for obj in extract_jsonld(page):
        if not isinstance(obj, dict):
            continue
        title = title or str(obj.get("name") or obj.get("headline") or "")
        a = obj.get("author")
        if isinstance(a, dict):
            author = author or str(a.get("name") or "")
        elif isinstance(a, list):
            names = []
            for z in a:
                if isinstance(z, dict) and z.get("name"):
                    names.append(str(z["name"]))
                elif isinstance(z, str):
                    names.append(z)
            author = author or ", ".join(names)
        elif a:
            author = author or str(a)
        isbn = isbn or str(obj.get("isbn") or "")
        p = obj.get("publisher")
        publisher = publisher or (str(p.get("name") or "") if isinstance(p, dict) else str(p or ""))
        image = obj.get("image")
        if isinstance(image, str):
            gallery.append(image)
        elif isinstance(image, list):
            gallery.extend(str(z) for z in image if z)

    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I)
    title = title or (m.group(1) if m else "")
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
    cover = m.group(1) if m else ""
    m = re.search(r'(?:ISBN(?:-1[03])?)[^0-9Xx]{0,20}([0-9][0-9Xx -]{8,17})', page, re.I)
    isbn = isbn or (m.group(1) if m else "")
    m = re.search(r'<meta[^>]+(?:name|property)=["\']author["\'][^>]+content=["\']([^"\']+)', page, re.I)
    author = author or (m.group(1) if m else "")

    isbn = clean_isbn(isbn)
    author = normalize_author(title, author)
    if not title:
        return None

    if cover and cover not in gallery:
        gallery.insert(0, cover)

    return {
        "title": strip_html(title),
        "author": strip_html(author),
        "isbn": isbn,
        "publisher": strip_html(publisher),
        "cover": cover,
        "gallery": gallery[:12],
        "spine": "",
        "source": source,
        "url": final_url,
    }


def links_from_page(page, base, pattern):
    links = []
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', page or "", re.I):
        href = urllib.parse.urljoin(base, href).split("#")[0]
        if re.search(pattern, href, re.I) and href not in links:
            links.append(href)
    for href in re.findall(r'\]\((https?://[^)\s]+)\)', page or "", re.I):
        if re.search(pattern, href, re.I) and href not in links:
            links.append(href)
    return links


def external_search(query, domain, limit=25):
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": f"site:{domain} {query}"})
    try:
        _, final, page = fetch_text(url)
    except Exception:
        return []
    out = []
    for href in re.findall(r'href=["\'](https?://[^"\']+)["\']', page or "", re.I):
        if domain in href and href not in out:
            out.append(href)
    return out[:limit]


def generic_site_search(query, base, pattern, source, limit=10):
    candidates = [
        base + "/search?q=" + urllib.parse.quote(query),
        base + "/search?query=" + urllib.parse.quote(query),
    ]
    for url in candidates:
        try:
            _, final, page = fetch_text(url)
        except Exception:
            continue
        links = links_from_page(page, final, pattern)
        result = [parse_product(x, source) for x in links[:40]]
        result = [x for x in result if x]
        if result:
            return result[:limit]
    links = external_search(query, urllib.parse.urlparse(base).netloc, 30)
    result = [parse_product(x, source) for x in links]
    return [x for x in result if x][:limit]


def megaknigi(query, limit=10):
    return generic_site_search(query, MEGAKNIGI_BASE, r"/(?:read|book|knig|proizved)", "MegaKnigi", limit)


def authortoday(query, limit=10):
    return generic_site_search(query, AUTHOR_TODAY_BASE, r"/work/", "Author.Today", limit)


# ---------- Читай-город: API + безопасный fallback ----------

def chitai_api_search(query, limit=10):
    api = "https://web-agr.chitai-gorod.ru/web/api/v2/search/product"
    params = {
        "customerCityId": os.environ.get("CHITAI_GOROD_CITY_ID", "39"),
        "products[page]": 1,
        "products[per-page]": max(10, min(limit * 3, 60)),
        "phrase": query,
    }
    url = api + "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json",
        "Origin": CHITAI_BASE,
        "Referer": CHITAI_BASE + "/",
        "User-Agent": "Mozilla/5.0",
    }
    token = os.environ.get("CHITAI_GOROD_BEARER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token

    try:
        _, _, raw = fetch_text(url, timeout=18, headers=headers)
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(str(e))

    blocks = []
    for key in ("included", "data"):
        block = data.get(key, []) if isinstance(data, dict) else []
        if isinstance(block, list):
            blocks.extend(block)

    wanted = clean_isbn(query) if looks_isbn(query) else ""
    out = []

    for p in blocks:
        if not isinstance(p, dict) or str(p.get("type", "")).lower() != "product":
            continue
        a = p.get("attributes") or {}
        title = str(a.get("title") or a.get("name") or "").strip()
        if not title:
            continue
        author = str(a.get("author") or "")
        m = re.match(r"^(.*?)\s*\(([^()]+)\)", title)
        if m:
            title = m.group(1).strip()
            author = author or m.group(2).strip()

        isbn = clean_isbn(a.get("isbn") or a.get("ean") or "")
        if wanted and isbn != wanted:
            continue

        cover = str(a.get("picture") or a.get("image") or a.get("cover") or "")
        if cover and cover.startswith("/"):
            cover = urllib.parse.urljoin(CHITAI_BASE, cover)

        pid = p.get("id")
        out.append({
            "title": title,
            "author": normalize_author(title, author),
            "isbn": isbn,
            "publisher": str(a.get("publisher") or ""),
            "cover": cover,
            "gallery": [cover] if cover else [],
            "spine": "",
            "source": "Читай-город",
            "url": urllib.parse.urljoin(CHITAI_BASE, str(a.get("url") or f"/product/{pid or ''}")),
        })
        if len(out) >= limit:
            break
    return out


def chitai_gorod(query, limit=10):
    # 401/403 у внутреннего API больше не ломает весь источник.
    try:
        found = chitai_api_search(query, limit)
        if found:
            return found
    except Exception:
        pass

    return generic_site_search(
        query,
        CHITAI_BASE,
        r"/product/",
        "Читай-город",
        limit,
    )


# ---------- Общий поиск ----------

def score(query, item):
    q = query.lower().strip()
    title = str(item.get("title", "")).lower()
    author = str(item.get("author", "")).lower()
    if looks_isbn(q):
        return 1000 if clean_isbn(q) == clean_isbn(item.get("isbn")) else -1000
    if title == q:
        return 500
    if q in title:
        return 250
    if q in author:
        return 120
    return 0


def dedup(items):
    seen = set()
    out = []
    for x in items:
        key = clean_isbn(x.get("isbn"))
        if not key:
            key = (
                re.sub(r"\W+", " ", str(x.get("title", "")).lower()).strip(),
                re.sub(r"\W+", " ", str(x.get("author", "")).lower()).strip(),
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def search_books(q):
    q = str(q or "").strip()
    if not q:
        return [], {}

    adapters = [
        ("ФантЛаб", fantlab_search),
        ("MegaKnigi", megaknigi),
        ("Author.Today", authortoday),
        ("Читай-город", chitai_gorod),
    ]

    results = []
    sources = {}

    def run(name, fn):
        try:
            return name, fn(q, 10), None
        except Exception as e:
            return name, [], str(e)[:180]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, n, f) for n, f in adapters]
        for future in as_completed(futures):
            name, found, error = future.result()
            results.extend(found)
            sources[name] = (
                {"status": "error", "count": 0, "error": error}
                if error else
                {"status": "ok", "count": len(found)}
            )

    if looks_isbn(q):
        wanted = clean_isbn(q)
        results = [x for x in results if clean_isbn(x.get("isbn")) == wanted]

    for x in results:
        x["_score"] = score(q, x)
    results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    for x in results:
        x.pop("_score", None)

    return dedup(results)[:30], sources


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path in ("/", ""):
            return str(ROOT / "index.html") if (ROOT / "index.html").exists() else str(STATIC / "index.html")
        root_file = ROOT / path.lstrip("/")
        if root_file.exists():
            return str(root_file)
        return str(STATIC / path.lstrip("/"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            results, sources = search_books(q)
            body = json.dumps({"results": results, "sources": sources}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


def main():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Книжный шкаф: поиск подключен, сервер запущен на порту {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
