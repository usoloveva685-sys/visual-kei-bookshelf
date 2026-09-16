#!/usr/bin/env python3
# Поисковый сервер книжного шкафа.
# Python 3.10+; только стандартная библиотека.
import json, re, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
UA = "VisualKeiBookshelf/0.2"


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE).strip()


def tokens(s):
    return [x for x in re.findall(r"[\w]+", norm(s), flags=re.UNICODE) if len(x) > 1]


def clean_isbn(s):
    return re.sub(r"[^0-9Xx]", "", s or "").upper()


def is_isbn(s):
    x = clean_isbn(s)
    return bool(re.fullmatch(r"(?:97[89]\d{10}|\d{9}[\dX])", x))


def lang_bonus(lang):
    l = (lang or "").lower()
    if l.startswith("ru"):
        return 25
    if l.startswith("uk") or l.startswith("be"):
        return 10
    return 0


def google_request(q, lang=None):
    params = {"maxResults": "40", "printType": "books", "q": q}
    if lang:
        params["langRestrict"] = lang
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    data, _ = http_get(url)
    return json.loads(data)


def google(q):
    # Несколько независимых запросов: один не должен определять всю выдачу.
    queries = []
    raw = q.strip()
    if is_isbn(raw):
        queries = [("isbn:" + clean_isbn(raw), None)]
    else:
        # Точное название и обычный поиск. Русский фильтр пробуем, но не доверяем ему полностью.
        queries = [
            ('intitle:"' + raw.replace('"', ' ') + '"', "ru"),
            ('intitle:"' + raw.replace('"', ' ') + '"', None),
            (raw, "ru"),
            (raw, None),
        ]

    out, seen = [], set()
    for qq, lang in queries:
        try:
            j = google_request(qq, lang)
        except Exception:
            continue
        for v in j.get("items", []):
            x = v.get("volumeInfo", {}) or {}
            ids = x.get("industryIdentifiers", []) or []
            isbn13 = next((z.get("identifier", "") for z in ids if z.get("type") == "ISBN_13"), "")
            isbn10 = next((z.get("identifier", "") for z in ids if z.get("type") == "ISBN_10"), "")
            isbn = isbn13 or isbn10
            cover = (x.get("imageLinks") or {}).get("thumbnail", "").replace("http:", "https:")
            item = {
                "title": x.get("title", ""),
                "author": ", ".join(x.get("authors", [])),
                "isbn": isbn,
                "cover": cover,
                "source": "Google Books",
                "language": x.get("language", ""),
                "published": x.get("publishedDate", ""),
                "description": x.get("description", ""),
            }
            key = (norm(item["title"]), norm(item["author"]), clean_isbn(item["isbn"]))
            if key not in seen and item["title"]:
                seen.add(key)
                out.append(item)
    return out


def openlibrary(q):
    params_list = []
    raw = q.strip()
    if is_isbn(raw):
        params_list = [{"isbn": clean_isbn(raw), "limit": "40"}]
    else:
        # title-параметр даёт существенно более чистую выдачу для русских названий.
        params_list = [
            {"title": raw, "limit": "40"},
            {"q": raw, "limit": "40"},
        ]

    out, seen = [], set()
    for params in params_list:
        try:
            url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
            data, _ = http_get(url)
            j = json.loads(data)
        except Exception:
            continue
        for x in j.get("docs", []):
            title = x.get("title", "") or ""
            author = ", ".join(x.get("author_name", []) or [])
            covers = x.get("cover_i")
            cover = f"https://covers.openlibrary.org/b/id/{covers}-M.jpg" if covers else ""
            isbns = x.get("isbn") or []
            isbn = next((z for z in isbns if len(clean_isbn(z)) == 13), (isbns[0] if isbns else ""))
            languages = x.get("language") or []
            item = {
                "title": title,
                "author": author,
                "isbn": isbn,
                "cover": cover,
                "source": "Open Library",
                "language": languages[0] if languages else "",
                "published": str(x.get("first_publish_year", "") or ""),
                "description": "",
                "subjects": x.get("subject", []) or [],
            }
            key = (norm(title), norm(author), clean_isbn(isbn))
            if key not in seen and title:
                seen.add(key)
                out.append(item)
    return out


ATOM = "http://www.w3.org/2005/Atom"
OS = "http://a9.com/-/spec/opensearch/1.1/"


def opds_search(root_url, q):
    raw, _ = http_get(root_url)
    feed_root = ET.fromstring(raw)
    search_url = None
    for link in feed_root.findall(f".//{{{ATOM}}}link"):
        if link.attrib.get("rel") == "search" and "opensearchdescription+xml" in link.attrib.get("type", ""):
            search_url = urllib.parse.urljoin(root_url, link.attrib.get("href", ""))
            break
    if not search_url:
        return []
    raw, _ = http_get(search_url)
    desc = ET.fromstring(raw)
    template = None
    for u in desc.findall(f"{{{OS}}}Url"):
        if "{searchTerms}" in u.attrib.get("template", ""):
            template = u.attrib["template"]
            break
    if not template:
        return []
    url = template.replace("{searchTerms}", urllib.parse.quote(q))
    raw, _ = http_get(urllib.parse.urljoin(root_url, url))
    feed = ET.fromstring(raw)
    out = []
    for e in feed.findall(f"{{{ATOM}}}entry"):
        title = (e.findtext(f"{{{ATOM}}}title") or "").strip()
        author = (e.findtext(f"{{{ATOM}}}author/{{{ATOM}}}name") or "").strip()
        ident = (e.findtext("{http://purl.org/dc/elements/1.1/}identifier") or "").strip()
        cover = ""
        for l in e.findall(f"{{{ATOM}}}link"):
            rel = l.attrib.get("rel", "")
            typ = l.attrib.get("type", "")
            if "image" in rel or typ.startswith("image/"):
                cover = urllib.parse.urljoin(root_url, l.attrib.get("href", ""))
                break
        if title:
            out.append({
                "title": title, "author": author,
                "isbn": clean_isbn(ident) if is_isbn(ident) else "",
                "cover": cover, "source": "OPDS", "language": "ru"
            })
    return out


def score(q, x):
    qn = norm(q)
    qt = tokens(q)
    title = norm(x.get("title", ""))
    author = norm(x.get("author", ""))
    score = 0

    # Главное — соответствие названия, а не случайное совпадение слов в описании.
    if title == qn:
        score += 220
    elif qn and qn in title:
        score += 150
    else:
        tt = set(tokens(title))
        overlap = sum(1 for w in qt if w in tt)
        score += overlap * 25
        if qt and overlap == len(qt):
            score += 55

    # Авторские совпадения.
    aq = set(qt) & set(tokens(author))
    score += len(aq) * 18

    score += lang_bonus(x.get("language", ""))

    source = x.get("source", "")
    if "Google Books" in source:
        score += 8
    elif "Open Library" in source:
        score += 3

    # Сборники/издания оставляем, но не позволяем им автоматически вытеснять точное название.
    if "сборник" in title and qn != title:
        score -= 8

    # Явный фанфик/fiction шум — сильный штраф, но не ломаем поиск полностью.
    blob = " ".join([title, " ".join(x.get("subjects", []) or [])]).lower()
    if any(w in blob for w in ("fanfiction", "фанфик", "fan fiction")):
        score -= 100

    return score


def search(q):
    q = q.strip()
    if not q:
        return []

    allr = []
    # Ошибки одного каталога не ломают остальные.
    for fn in (google, openlibrary):
        try:
            allr.extend(fn(q))
        except Exception:
            pass

    for name, url in SOURCES.get("opds", []):
        try:
            for x in opds_search(url, q):
                x["source"] = name + " (OPDS)"
                allr.append(x)
        except Exception:
            pass

    # Дедупликация: одинаковые издания из разных источников объединяем;
    # разные ISBN сохраняем как разные издания.
    seen = set()
    unique = []
    for x in allr:
        isbn = clean_isbn(x.get("isbn", ""))
        key = (norm(x.get("title", "")), norm(x.get("author", "")), isbn)
        if key in seen:
            continue
        seen.add(key)
        unique.append(x)

    unique.sort(key=lambda x: score(q, x), reverse=True)
    return unique[:60]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/api/search":
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            try:
                results = search(q)
                body = json.dumps({"query": q, "results": results}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"query": q, "results": [], "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return
        return super().do_GET()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8765"))
    Handler.directory = str(ROOT)
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
