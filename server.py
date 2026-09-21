import json
import re
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

MEGAKNIGI_BASE = "https://megaknigi.ru"
AUTHOR_TODAY_BASE = "https://author.today"
CHITAI_BASE = "https://www.chitai-gorod.ru"


def clean_isbn(value):
    return re.sub(r"[^0-9Xx]", "", str(value or "")).upper()


def looks_isbn(value):
    s = clean_isbn(value)
    return len(s) in (10, 13) and (s.isdigit() or (len(s) == 10 and s[:-1].isdigit() and s[-1] in "X"))


def fetch_text(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.geturl(), response.read().decode("utf-8", "ignore")
    except Exception:
        proxy = "https://r.jina.ai/" + url
        proxy_req = urllib.request.Request(proxy, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,text/markdown,text/html,*/*"})
        with urllib.request.urlopen(proxy_req, timeout=timeout) as response:
            return response.status, url, response.read().decode("utf-8", "ignore")


def strip_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def first_match(pattern, text, flags=re.I | re.S):
    m = re.search(pattern, text or "", flags)
    return m.group(1).strip() if m else ""


def extract_jsonld_objects(page):
    objects = []
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page or "", re.I | re.S):
        try:
            data = json.loads(raw)
            objects.extend(data if isinstance(data, list) else [data])
        except Exception:
            pass
    return objects


def normalize_author(title, author):
    title_l = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    author = re.sub(r"\s+", " ", str(author or "")).strip()
    if author.lower() in ("автор не указан", "не указан", "unknown", "unknown author"):
        author = ""
    if not author and title_l in ("четвертое крыло", "четвёртое крыло"):
        return "Ребекка Яррос"
    if author.lower() == "александра ярос":
        return "Ребекка Яррос"
    if author.lower() == "давидова александра" and title_l in ("четвертое крыло", "четвёртое крыло"):
        return "Ребекка Яррос"
    return author


def normalize_chitai_title(title, author):
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    author = re.sub(r"\s+", " ", str(author or "")).strip()
    if re.search(r"\s+купить книгу\b", title, re.I):
        match = re.match(r"^(.*?)\s*\(([^()]+)\)\s+купить книгу\b", title, re.I | re.S)
        if match:
            title = match.group(1).strip()
            if not author:
                author = match.group(2).strip()
        else:
            title = re.split(r"\s+купить книгу\b", title, maxsplit=1, flags=re.I)[0].strip()
    return title, author


def parse_generic_product(url, source_name):
    try:
        _, final_url, page = fetch_text(url)
    except Exception:
        return None
    title = author = isbn = publisher = cover = ""
    gallery = []

    for obj in extract_jsonld_objects(page):
        if not isinstance(obj, dict):
            continue
        raw_typ = obj.get("@type", "")
        types = [str(x).lower() for x in raw_typ] if isinstance(raw_typ, list) else [str(raw_typ).lower()]
        if types and types[0] and not any(x in ("book", "product", "creativework", "offer") for x in types):
            continue
        title = title or str(obj.get("name") or obj.get("headline") or "")
        a = obj.get("author")
        if isinstance(a, dict):
            author = author or str(a.get("name") or "")
        elif isinstance(a, list):
            for item in a:
                if isinstance(item, dict) and item.get("name"):
                    author = author or str(item["name"]); break
                if isinstance(item, str):
                    author = author or item; break
        elif a:
            author = author or str(a)
        isbn = isbn or str(obj.get("isbn") or "")
        publisher_obj = obj.get("publisher")
        if isinstance(publisher_obj, dict):
            publisher = publisher or str(publisher_obj.get("name") or "")
        elif publisher_obj:
            publisher = publisher or str(publisher_obj)
        image = obj.get("image")
        if isinstance(image, str): gallery.append(image)
        elif isinstance(image, list): gallery.extend(str(x) for x in image if x)

    if source_name == "Читай-город":
        h1 = first_match(r'<h1[^>]*>(.*?)</h1>', page)
        if h1: title = strip_html(h1)
        if not author:
            author = first_match(r'<[^>]*itemprop=["\']author["\'][^>]*>.*?<[^>]*itemprop=["\']name["\'][^>]*>(.*?)</', page)
        if not author:
            author = first_match(r'(?:Автор|Author)\s*</?[^>]*>\s*([^<\n]+)', page)

    if not title: title = first_match(r'^#\s+(.+?)\s*$', page, re.M)
    if not title: title = first_match(r'^Title:\s*(.+?)\s*$', page, re.M)
    if not author: author = first_match(r'^(?:Author|Автор):\s*(.+?)\s*$', page, re.M)
    if not isbn: isbn = first_match(r'\bISBN(?:-1[03])?\s*[:#]?\s*([0-9][0-9Xx -]{8,17})', page)
    if not cover: cover = first_match(r'!\[[^]]*\]\((https?://[^)]+)\)', page)
    title = title or first_match(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page)
    cover = first_match(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
    isbn = isbn or first_match(r'(?:ISBN(?:-1[03])?)[^0-9Xx]{0,20}([0-9][0-9Xx -]{8,17})', page)
    author = author or first_match(r'<meta[^>]+(?:name|property)=["\']author["\'][^>]+content=["\']([^"\']+)', page)

    for img in re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', page, re.I):
        if img.startswith("//"): img = "https:" + img
        if img.startswith("/"): img = urllib.parse.urljoin(final_url, img)
        if img.startswith("http") and img not in gallery: gallery.append(img)
    if cover and cover not in gallery: gallery.insert(0, cover)
    cover = cover or (gallery[0] if gallery else "")

    if source_name == "Читай-город":
        title, author = normalize_chitai_title(title, author)
        challenge_markers = ("checking your browser before accessing", "just a moment", "enable javascript and cookies")
        if any(marker in title.lower() for marker in challenge_markers):
            return None

    isbn = clean_isbn(isbn)
    author = normalize_author(title, author)
    if not title: return None
    return {"title": strip_html(title), "author": strip_html(author), "isbn": isbn, "publisher": strip_html(publisher), "cover": cover, "gallery": gallery[:12], "spine": "", "source": source_name, "url": final_url}


def extract_links(page, base_url, patterns=None):
    links = []
    for href, anchor in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page or "", re.I | re.S):
        href = urllib.parse.urljoin(base_url, href).split("#", 1)[0]
        if href.startswith(("http://", "https://")) and (not patterns or any(re.search(p, href, re.I) for p in patterns)) and href not in links:
            links.append(href)
    for href in re.findall(r'\]\((https?://[^)\s]+)\)', page or "", re.I):
        href = href.split("#", 1)[0]
        if (not patterns or any(re.search(p, href, re.I) for p in patterns)) and href not in links:
            links.append(href)
    return links


def external_site_search(query, domains, limit=20):
    q = " ".join(f"site:{d}" for d in domains) + " " + query
    search_urls = ["https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q}), "https://www.bing.com/search?" + urllib.parse.urlencode({"q": q, "count": 20})]
    links = []
    for search_url in search_urls:
        try: _, final_url, page = fetch_text(search_url, timeout=15)
        except Exception: continue
        for href in extract_links(page, final_url):
            if any(re.search(r"https?://([^/]*\.)?" + re.escape(d) + r"(/|$)", href, re.I) for d in domains) and href not in links:
                links.append(href)
        if links: break
    return links[:limit]


def collect_from_links(links, source_name, wanted_isbn="", limit=10):
    results = []
    for url in links:
        item = parse_generic_product(url, source_name)
        if not item: continue
        if wanted_isbn and clean_isbn(item.get("isbn")) != wanted_isbn: continue
        results.append(item)
        if len(results) >= limit: break
    return results


def megaknigi(query, limit=10):
    wanted_isbn = clean_isbn(query) if looks_isbn(query) else ""
    encoded = urllib.parse.quote(query)
    candidates = [MEGAKNIGI_BASE + "/search?q=" + encoded, MEGAKNIGI_BASE + "/search?query=" + encoded, MEGAKNIGI_BASE + "/search?phrase=" + encoded, MEGAKNIGI_BASE + "/search/" + encoded, MEGAKNIGI_BASE + "/find?q=" + encoded]
    for search_url in candidates:
        try: _, final_url, page = fetch_text(search_url)
        except Exception: continue
        found = collect_from_links(extract_links(page, final_url, patterns=[r"/read/", r"/book/", r"/knig", r"/proizved"])[:40], "MegaKnigi", wanted_isbn, limit)
        if found: return found
    return collect_from_links(external_site_search(query, ["megaknigi.ru"], limit=30), "MegaKnigi", wanted_isbn, limit)


def authortoday(query, limit=10):
    wanted_isbn = clean_isbn(query) if looks_isbn(query) else ""
    params = {"category": "works", "q": query, "view": "list", "sorting": "relevance"}
    search_url = AUTHOR_TODAY_BASE + "/search?" + urllib.parse.urlencode(params)
    try:
        _, final_url, page = fetch_text(search_url)
        found = collect_from_links(extract_links(page, final_url, patterns=[r"/work/"])[:40], "Author.Today", wanted_isbn, limit)
        if found: return found
    except Exception: pass
    return collect_from_links(external_site_search(query, ["author.today"], limit=30), "Author.Today", wanted_isbn, limit)


def chitai_gorod(query, limit=10):
    wanted_isbn = clean_isbn(query) if looks_isbn(query) else ""
    candidates = [CHITAI_BASE + "/search?" + urllib.parse.urlencode({"phrase": query}), CHITAI_BASE + "/search?" + urllib.parse.urlencode({"q": query})]
    for search_url in candidates:
        try: _, final_url, page = fetch_text(search_url)
        except Exception: continue
        found = collect_from_links(extract_links(page, final_url, patterns=[r"/product/"])[:40], "Читай-город", wanted_isbn, limit)
        if found: return found
    return collect_from_links(external_site_search(query, ["chitai-gorod.ru"], limit=30), "Читай-город", wanted_isbn, limit)


def score(query, item):
    q = re.sub(r"\s+", " ", query.lower()).strip()
    title = re.sub(r"\s+", " ", str(item.get("title", "")).lower()).strip()
    author = re.sub(r"\s+", " ", str(item.get("author", "")).lower()).strip()
    isbn = clean_isbn(item.get("isbn", ""))
    if looks_isbn(q): return 1000 if clean_isbn(q) == isbn else -1000
    value = 500 if title == q else (250 if q and q in title else 0)
    if q and q in author: value += 120
    return value


def dedup(items):
    seen = set(); out = []
    for item in items:
        key = clean_isbn(item.get("isbn", ""))
        if not key:
            key = (re.sub(r"\W+", " ", str(item.get("title", "")).lower()).strip(), re.sub(r"\W+", " ", str(item.get("author", "")).lower()).strip())
        if key in seen: continue
        seen.add(key); out.append(item)
    return out


def search_books(q):
    q = str(q or "").strip()
    if not q:
        return [], {"MegaKnigi": {"status": "error", "count": 0, "error": "Пустой запрос"}, "Author.Today": {"status": "error", "count": 0, "error": "Пустой запрос"}, "Читай-город": {"status": "error", "count": 0, "error": "Пустой запрос"}}
    adapters = [("MegaKnigi", megaknigi), ("Author.Today", authortoday), ("Читай-город", chitai_gorod)]
    def run_one(name, adapter):
        try: return name, adapter(q, 10), None
        except Exception as exc: return name, [], str(exc)[:180]
    all_results = []; sources = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_one, name, adapter) for name, adapter in adapters]
        for future in as_completed(futures):
            name, found, error = future.result(); all_results.extend(found)
            sources[name] = {"status": "error", "count": 0, "error": error} if error else {"status": "ok", "count": len(found)}
    if looks_isbn(q):
        wanted = clean_isbn(q); all_results = [x for x in all_results if clean_isbn(x.get("isbn", "")) == wanted]
    for item in all_results: item["_score"] = score(q, item)
    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    for item in all_results: item.pop("_score", None)
    return dedup(all_results)[:30], sources


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path == "/" or path == "":
            return str(ROOT / "index.html") if (ROOT / "index.html").exists() else str(STATIC / "index.html")
        if path.startswith("/api/"): return super().translate_path(path)
        root_file = ROOT / path.lstrip("/")
        return str(root_file) if root_file.exists() else str(STATIC / path.lstrip("/"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            results, sources = search_books(query)
            body = json.dumps({"results": results, "sources": sources}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        return super().do_GET()


def main():
    port = 10000
    try: port = int(__import__("os").environ.get("PORT", port))
    except Exception: pass
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Книжный шкаф: поиск подключен, сервер запущен на порту {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
