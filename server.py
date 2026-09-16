#!/usr/bin/env python3
# Книжный шкаф — поисковый сервер.
# Источники:
#   1) FantLab — основной источник для русской фантастики/фэнтези и изданий.
#   2) Google Books — общая/зарубежная база.
#   3) Open Library — резервная международная база.
#   4) OPDS из sources.json — дополнительные каталоги.
#
# Python 3.10+, только стандартная библиотека.

import json
import re
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8")) if (ROOT / "sources.json").exists() else {}

UA = "VisualKeiBookshelf/0.3"

def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()

def clean_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", str(s))
    s = re.sub(r"\[[^\]]+\]", "", s)
    return re.sub(r"\s+", " ", s).strip()

def norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[\u2010-\u2015]", "-", s)
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()

def isbn_clean(s):
    return re.sub(r"[^0-9Xx]", "", s or "").upper()

def looks_isbn(q):
    x = isbn_clean(q)
    return bool(re.fullmatch(r"(?:97[89]\d{10}|\d{9}[\dX])", x))

def tokens(s):
    return [x for x in norm(s).split() if len(x) > 1]

def language_score(x):
    lang = (x.get("language") or "").lower()
    if lang in ("ru", "rus", "russian"):
        return 25
    if "ru" in lang:
        return 20
    return 0

def score(q, x):
    qn = norm(q)
    title = norm(x.get("title", ""))
    author = norm(x.get("author", ""))
    if not qn:
        return 0

    s = 0
    if qn == title:
        s += 140
    elif qn in title:
        s += 105
    else:
        qt = tokens(qn)
        tt = set(tokens(title))
        if qt:
            overlap = sum(t in tt for t in qt)
            s += int(65 * overlap / len(qt))

    if qn == author:
        s += 120
    elif qn in author:
        s += 90

    # Авторский запрос: несколько слов совпадают с именем автора.
    at = set(tokens(author))
    qt = tokens(qn)
    if qt and at:
        s += 35 * sum(t in at for t in qt)

    s += language_score(x)

    src = (x.get("source") or "").lower()
    if "fantlab" in src:
        s += 18
    elif "google" in src:
        s += 8
    elif "open library" in src:
        s += 0

    # Явный фанфик/фикшен-мусор отодвигаем, но не удаляем полностью.
    bad = ("фанфик", "fanfiction", "fanfic", "ficbook")
    if any(b in title for b in bad):
        s -= 80

    return s

# ---------------- Google Books ----------------

def google_request(q):
    url = (
        "https://www.googleapis.com/books/v1/volumes?"
        "maxResults=40&printType=books&q=" + urllib.parse.quote(q)
    )
    data, _ = http_get(url)
    return json.loads(data)

def google(q):
    queries = []
    if looks_isbn(q):
        queries.append("isbn:" + isbn_clean(q))
    else:
        queries.extend([
            'intitle:"' + q.replace('"', "") + '"',
            q,
        ])

    out = []
    seen = set()

    for qq in queries:
        try:
            j = google_request(qq)
        except Exception:
            continue

        for v in j.get("items", []):
            x = v.get("volumeInfo", {})
            ids = x.get("industryIdentifiers") or []
            isbn = ""
            for z in ids:
                ident = isbn_clean(z.get("identifier", ""))
                if len(ident) in (10, 13):
                    isbn = ident
                    break

            cover = (x.get("imageLinks") or {}).get("thumbnail", "")
            cover = cover.replace("http:", "https:")
            title = x.get("title", "")
            author = ", ".join(x.get("authors", []))

            key = (norm(title), norm(author), isbn)
            if key in seen:
                continue
            seen.add(key)

            out.append({
                "title": title,
                "author": author,
                "isbn": isbn,
                "cover": cover,
                "language": x.get("language", ""),
                "publisher": x.get("publisher", ""),
                "published": x.get("publishedDate", ""),
                "source": "Google Books",
                "source_url": "https://books.google.com/books?id=" + v.get("id", ""),
            })
    return out

# ---------------- Open Library ----------------

def openlibrary(q):
    params = {
        "limit": "40",
        "fields": "key,title,author_name,isbn,language,first_publish_year,cover_i,publisher",
    }

    if looks_isbn(q):
        params["isbn"] = isbn_clean(q)
    else:
        params["q"] = q

    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    try:
        data, _ = http_get(url)
        j = json.loads(data)
    except Exception:
        return []

    out = []
    for x in j.get("docs", []):
        cover = ""
        if x.get("cover_i"):
            cover = f"https://covers.openlibrary.org/b/id/{x['cover_i']}-M.jpg"

        langs = x.get("language") or []
        lang = ",".join(langs) if isinstance(langs, list) else str(langs)

        out.append({
            "title": x.get("title", ""),
            "author": ", ".join(x.get("author_name", [])),
            "isbn": next((isbn_clean(i) for i in (x.get("isbn") or []) if len(isbn_clean(i)) in (10, 13)), ""),
            "cover": cover,
            "language": lang,
            "publisher": ", ".join(x.get("publisher", [])[:2]),
            "published": str(x.get("first_publish_year", "") or ""),
            "source": "Open Library",
            "source_url": "https://openlibrary.org" + x.get("key", ""),
        })
    return out

# ---------------- FantLab ----------------

FANTLAB = "https://api.fantlab.ru"

def fantlab_get(path, params):
    url = FANTLAB + path + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    data, _ = http_get(url, headers={"Accept": "application/json"})
    return json.loads(data)

def fantlab_search_works(q):
    try:
        return fantlab_get(
            "/search-works",
            {"q": q, "page": 1, "onlymatches": 1},
        )
    except Exception:
        return []

def fantlab_search_autors(q):
    try:
        return fantlab_get(
            "/search-autors",
            {"q": q, "page": 1, "onlymatches": 1},
        )
    except Exception:
        return []

def fantlab_search_editions(q):
    try:
        return fantlab_get(
            "/search-editions",
            {"q": q, "page": 1, "onlymatches": 1},
        )
    except Exception:
        return []

def fantlab(q):
    out = []
    seen = set()

    # Если это ISBN — ищем непосредственно издание.
    if looks_isbn(q):
        for x in fantlab_search_editions(isbn_clean(q)):
            title = clean_html(x.get("name", ""))
            authors = clean_html(x.get("autors", ""))
            isbn = isbn_clean(x.get("isbn2") or x.get("isbn1") or "")
            item = {
                "title": title,
                "author": authors,
                "isbn": isbn,
                "cover": "",
                "language": "ru",
                "publisher": clean_html(x.get("publisher", "")),
                "published": str(x.get("year", "") or ""),
                "source": "FantLab",
                "source_url": "https://fantlab.ru/edition" + str(x.get("edition_id", "")),
            }
            key = (norm(title), norm(authors), isbn)
            if key not in seen and title:
                seen.add(key)
                out.append(item)
        return out

    # 1. Произведения — лучше всего для поиска названия и автора.
    for x in fantlab_search_works(q):
        title = clean_html(x.get("rusname") or x.get("fullname") or x.get("name"))
        author = clean_html(x.get("all_autor_rusname") or x.get("autor1_rusname") or "")
        if not title:
            continue

        item = {
            "title": title,
            "author": author,
            "isbn": "",
            "cover": "",
            "language": "ru",
            "publisher": "",
            "published": str(x.get("year", "") or ""),
            "source": "FantLab",
            "source_url": "https://fantlab.ru/work" + str(x.get("work_id", "")),
            "_work_id": x.get("work_id"),
            "_pic_edition_id": x.get("pic_edition_id_auto") or x.get("pic_edition_id"),
        }
        key = (norm(title), norm(author), "")
        if key not in seen:
            seen.add(key)
            out.append(item)

    # 2. Если запрос похож на фамилию/имя автора, авторский поиск помогает
    # найти его произведения даже когда общий поиск дал мало результатов.
    for a in fantlab_search_autors(q)[:5]:
        aname = clean_html(a.get("rusname") or a.get("name") or "")
        if not aname:
            continue
        # Добавляем только саму авторскую запись как мягкий результат;
        # конкретные книги будут найдены отдельным поиском по исходному запросу.
        item = {
            "title": "",
            "author": aname,
            "isbn": "",
            "cover": "",
            "language": "ru",
            "publisher": "",
            "published": "",
            "source": "FantLab — автор",
            "source_url": "https://fantlab.ru/autor" + str(a.get("autor_id", "")),
        }
        key = ("", norm(aname), "")
        if key not in seen:
            seen.add(key)
            out.append(item)

    # 3. Поиск изданий по исходному запросу — даёт ISBN/издателя.
    for x in fantlab_search_editions(q)[:25]:
        title = clean_html(x.get("name", ""))
        author = clean_html(x.get("autors", ""))
        isbn = isbn_clean(x.get("isbn2") or x.get("isbn1") or "")
        if not title:
            continue

        item = {
            "title": title,
            "author": author,
            "isbn": isbn,
            "cover": "",
            "language": "ru",
            "publisher": clean_html(x.get("publisher", "")),
            "published": str(x.get("year", "") or ""),
            "source": "FantLab",
            "source_url": "https://fantlab.ru/edition" + str(x.get("edition_id", "")),
        }
        key = (norm(title), norm(author), isbn)
        if key not in seen:
            seen.add(key)
            out.append(item)

    return out

# ---------------- OPDS ----------------

def opds_search(root_url, q):
    # OPDS intentionally remains a supplementary source. Failures are isolated.
    try:
        import xml.etree.ElementTree as ET

        raw, _ = http_get(root_url)
        root = ET.fromstring(raw)

        search_url = None
        for link in root.findall(".//{http://www.w3.org/2005/Atom}link"):
            if (
                link.attrib.get("rel") == "search"
                and "opensearchdescription+xml" in link.attrib.get("type", "")
            ):
                search_url = urllib.parse.urljoin(root_url, link.attrib.get("href", ""))
                break

        if not search_url:
            return []

        raw, _ = http_get(search_url)
        desc = ET.fromstring(raw)

        template = None
        for u in desc.findall("{http://a9.com/-/spec/opensearch/1.1/}Url"):
            if "{searchTerms}" in u.attrib.get("template", ""):
                template = u.attrib["template"]
                break

        if not template:
            return []

        url = template.replace("{searchTerms}", urllib.parse.quote(q))
        raw, _ = http_get(urllib.parse.urljoin(root_url, url))
        feed = ET.fromstring(raw)

        out = []
        for e in feed.findall("{http://www.w3.org/2005/Atom}entry"):
            title = (e.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            author = (
                e.findtext("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name")
                or ""
            ).strip()
            ident = (
                e.findtext("{http://purl.org/dc/elements/1.1/}identifier")
                or ""
            ).strip()
            cover = ""

            for l in e.findall("{http://www.w3.org/2005/Atom}link"):
                rel = l.attrib.get("rel", "")
                if "image" in rel:
                    cover = urllib.parse.urljoin(root_url, l.attrib.get("href", ""))
                    break

            if title:
                out.append({
                    "title": title,
                    "author": author,
                    "isbn": isbn_clean(ident) if looks_isbn(ident) else "",
                    "cover": cover,
                    "language": "ru",
                    "publisher": "",
                    "published": "",
                    "source": "OPDS",
                })
        return out
    except Exception:
        return []

def search(q):
    q = (q or "").strip()
    if not q:
        return []

    allr = []

    # FantLab — основной жанровый русский источник.
    allr.extend(fantlab(q))

    # Общие международные источники.
    try:
        allr.extend(google(q))
    except Exception:
        pass

    try:
        allr.extend(openlibrary(q))
    except Exception:
        pass

    # Дополнительные OPDS-каталоги.
    for name, url in SOURCES.get("opds", []):
        rr = opds_search(url, q)
        for x in rr:
            x["source"] = name + " (OPDS)"
        allr.extend(rr)

    # Удаляем дубли, сохраняя разные ISBN как разные издания.
    unique = {}
    for x in allr:
        title = x.get("title", "")
        author = x.get("author", "")
        isbn = isbn_clean(x.get("isbn", ""))

        if not title and author:
            key = ("author", norm(author))
        else:
            key = (norm(title), norm(author), isbn)

        if key not in unique:
            unique[key] = x
        else:
            # Предпочитаем запись с обложкой/ISBN/издателем.
            old = unique[key]
            for field in ("cover", "isbn", "publisher", "published", "source_url"):
                if not old.get(field) and x.get(field):
                    old[field] = x[field]

    out = list(unique.values())
    out.sort(key=lambda x: score(q, x), reverse=True)
    return out[:60]

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)

        if u.path == "/api/search":
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            try:
                results = search(q)
                body = json.dumps(
                    {"query": q, "results": results},
                    ensure_ascii=False,
                ).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps(
                    {"query": q, "results": [], "error": str(e)},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            return

        return super().do_GET()

if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8765"))
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
