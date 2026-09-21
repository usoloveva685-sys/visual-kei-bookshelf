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


def fetch_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.geturl(), response.read().decode("utf-8", "ignore")


def strip_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def first_match(pattern, text, flags=re.I | re.S):
    m = re.search(pattern, text or "", flags)
    return m.group(1).strip() if m else ""


def extract_jsonld_objects(page):
    objects = []
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page or "", re.I | re.S):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                objects.extend(data)
            else:
                objects.append(data)
        except Exception:
            pass
    return objects


def normalize_author(title, author):
    title_l = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    author = re.sub(r"\s+", " ", str(author or "")).strip()

    # Known metadata mistakes encountered in catalogue searches.
    if title_l in ("четвертое крыло", "четвёртое крыло"):
        return "Ребекка Яррос"
    if title_l == "ртуть":
        return "Нил Стивенсон"
    if author.lower() in ("автор не указан", "не указан", "unknown", "unknown author"):
        return ""
    if author.lower() == "александра ярос":
        return "Ребекка Яррос"
    return author


def parse_generic_product(url, source_name):
    try:
        _, final_url, page = fetch_text(url)
    except Exception:
        return None

    title = ""
    author = ""
    isbn = ""
    publisher = ""
    cover = ""
    gallery = []

    for obj in extract_jsonld_objects(page):
        if not isinstance(obj, dict):
            continue
        typ = str(obj.get("@type", ""))
        if typ and typ.lower() not in ("book", "product", "creativework", "offer"):
            continue
        title = title or str(obj.get("name") or obj.get("headline") or "")
        a = obj.get("author")
        if isinstance(a, dict):
            author = author or str(a.get("name") or "")
        elif isinstance(a, list):
            for item in a:
                if isinstance(item, dict) and item.get("name"):
                    author = author or str(item["name"])
                    break
                if isinstance(item, str):
                    author = author or item
                    break
        elif a:
            author = author or str(a)
        isbn = isbn or str(obj.get("isbn") or "")
        publisher_obj = obj.get("publisher")
        if isinstance(publisher_obj, dict):
            publisher = publisher or str(publisher_obj.get("name") or "")
        elif publisher_obj:
            publisher = publisher or str(publisher_obj)
        image = obj.get("image")
        if isinstance(image, str):
            gallery.append(image)
        elif isinstance(image, list):
            gallery.extend(str(x) for x in image if x)

    title = title or first_match(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page)
    cover = first_match(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)

    isbn = isbn or first_match(r'(?:ISBN(?:-1[03])?)[^0-9Xx]{0,20}([0-9][0-9Xx -]{8,17})', page)
    author = author or first_match(r'<meta[^>]+(?:name|property)=["\']author["\'][^>]+content=["\']([^"\']+)', page)

    for img in re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', page, re.I):
        if img.startswith("//"):
            img = "https:" + img
        if img.startswith("/"):
            img = urllib.parse.urljoin(final_url, img)
        if img.startswith("http") and img not in gallery:
            gallery.append(img)

    if cover and cover not in gallery:
        gallery.insert(0, cover)

    cover = cover or (gallery[0] if gallery else "")
    isbn = clean_isbn(isbn)
    author = normalize_author(title, author)

    if not title:
        return None

    return {
        "title": strip_html(title),
        "author": strip_html(author),
        "isbn": isbn,
        "publisher": strip_html(publisher),
        "cover": cover,
        "gallery": gallery[:12],
        "spine": "",
        "source": source_name,
        "url": final_url,
    }


def chitai_gorod(query, limit=10):
    results = []
    wanted_isbn = clean_isbn(query) if looks_isbn(query) else ""

    search_url = CHITAI_BASE + "/search?" + urllib.parse.urlencode({"q": query})
    try:
        _, final_url, page = fetch_text(search_url)
    except Exception:
        return results

    links = []
    for href in re.findall(r'href=["\']([^"\']*?/product/[^"\']+)["\']', page or "", re.I):
        href = urllib.parse.urljoin(final_url, href)
        href = href.split("#", 1)[0]
        if href not in links:
            links.append(href)

    for url in links[:30]:
        item = parse_generic_product(url, "Читай-город")
        if not item:
            continue
        if wanted_isbn and clean_isbn(item.get("isbn")) != wanted_isbn:
            continue
        results.append(item)
        if len(results) >= limit:
            break

    return results


def megaknigi(query, limit=10):
    # Kept as a safe adapter. If the current site changes its HTML,
    # it simply returns no results rather than breaking the whole search.
    results = []
    try:
        url = MEGAKNIGI_BASE + "/search/" + urllib.parse.quote(query)
        _, final_url, page = fetch_text(url)
    except Exception:
        return results

    for href in re.findall(r'href=["\']([^"\']+)["\']', page or "", re.I):
        if "/read/" not in href:
            continue
        u = urllib.parse.urljoin(final_url, href)
        item = parse_generic_product(u, "MegaKnigi")
        if item:
            results.append(item)
        if len(results) >= limit:
            break
    return results


def authortoday(query, limit=10):
    results = []
    try:
        url = AUTHOR_TODAY_BASE + "/search?" + urllib.parse.urlencode({"q": query})
        _, final_url, page = fetch_text(url)
    except Exception:
        return results

    links = []
    for href in re.findall(r'href=["\']([^"\']*/work/[^"\']+)["\']', page or "", re.I):
        u = urllib.parse.urljoin(final_url, href).split("#", 1)[0]
        if u not in links:
            links.append(u)

    for u in links[:30]:
        item = parse_generic_product(u, "Author.Today")
        if item:
            results.append(item)
        if len(results) >= limit:
            break
    return results


def score(query, item):
    q = re.sub(r"\s+", " ", query.lower()).strip()
    title = re.sub(r"\s+", " ", str(item.get("title", "")).lower()).strip()
    author = re.sub(r"\s+", " ", str(item.get("author", "")).lower()).strip()
    isbn = clean_isbn(item.get("isbn", ""))

    if looks_isbn(q):
        return 1000 if clean_isbn(q) == isbn else -1000

    value = 0
    if title == q:
        value += 500
    elif q and q in title:
        value += 250
    if q and q in author:
        value += 120
    return value


def dedup(items):
    seen = set()
    out = []
    for item in items:
        key = clean_isbn(item.get("isbn", ""))
        if not key:
            key = (
                re.sub(r"\W+", " ", str(item.get("title", "")).lower()).strip(),
                re.sub(r"\W+", " ", str(item.get("author", "")).lower()).strip(),
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def search_books(q):
    q = str(q or "").strip()
    if not q:
        return [], "Введите название, автора или ISBN."

    adapters = [
        ("MegaKnigi", megaknigi),
        ("Author.Today", authortoday),
        ("Читай-город", chitai_gorod),
    ]

    all_results = []
    status = []

    for name, adapter in adapters:
        try:
            found = adapter(q, 10)
            all_results.extend(found)
            status.append(name)
        except Exception:
            status.append(name + ": ошибка")

    # ISBN is an exact identifier: never return unrelated books.
    if looks_isbn(q):
        wanted = clean_isbn(q)
        all_results = [
            x for x in all_results
            if clean_isbn(x.get("isbn", "")) == wanted
        ]

    for item in all_results:
        item["_score"] = score(q, item)

    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return dedup(all_results)[:30], "Проверены: " + ", ".join(status)


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path == "/" or path == "":
            # index.html находится в корне репозитория.
            if (ROOT / "index.html").exists():
                return str(ROOT / "index.html")
            return str(STATIC / "index.html")
        if path.startswith("/api/"):
            return super().translate_path(path)
        # Сначала ищем файл в корне проекта, затем в static.
        root_file = ROOT / path.lstrip("/")
        if root_file.exists():
            return str(root_file)
        return str(STATIC / path.lstrip("/"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            results, status = search_books(query)
            body = json.dumps(
                {"results": results, "status": status},
                ensure_ascii=False
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        return super().do_GET()


def main():
    port = 10000
    try:
        port = int(__import__("os").environ.get("PORT", port))
    except Exception:
        pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Книжный шкаф запущен на порту {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
