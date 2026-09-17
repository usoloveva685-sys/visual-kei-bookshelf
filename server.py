#!/usr/bin/env python3
import json, re, urllib.parse, urllib.request, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import xml.etree.ElementTree as ET
import html

ROOT = Path(__file__).resolve().parent
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8")) if (ROOT / "sources.json").exists() else {"opds": []}
UA = "VisualKeiBookshelf/0.3 (+human-facing book discovery)"


def http_get(url, headers=None, timeout=5):
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
        raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=4)
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
    # Labirint is intentionally NOT called during normal search.
    # It is an image/spine enrichment source and can be slow or unavailable.
    return results

# ---------------- MegaKnigi adapter ----------------
MEGAKNIGI_BASE = "https://megaknigi.ru"

def megaknigi_clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def megaknigi_attr(tag, name):
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    return html.unescape(m.group(1).strip()) if m else ""


def megaknigi_fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Referer": MEGAKNIGI_BASE + "/",
    })
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.geturl(), r.read().decode("utf-8", "ignore")


def megaknigi_card_blocks(html):
    starts = list(re.finditer(
        r'<div\b[^>]*class=["\'][^"\']*bookCard[^"\']*["\'][^>]*>',
        html, re.I
    ))
    blocks = []
    for i, sm in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        blocks.append(html[sm.start():end])
    return blocks


def megaknigi_parse_card(card):
    links = []
    for m in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*>(.*?)</a>',
        card, re.I | re.S
    ):
        href = urllib.parse.urljoin(MEGAKNIGI_BASE, m.group(1))
        txt = megaknigi_clean(m.group(2))
        if href not in [x[0] for x in links]:
            links.append((href, txt))
    if not links:
        return []

    title = ""
    for p in [
        r'<[^>]+class=["\'][^"\']*(?:book-title|bookTitle|book-name|bookName|title)[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>'
    ]:
        m = re.search(p, card, re.I | re.S)
        if m and megaknigi_clean(m.group(1)):
            title = megaknigi_clean(m.group(1))
            break
    if not title:
        for _, txt in links:
            if txt and txt.lower() not in {"читать", "скачать", "подробнее"}:
                title = txt
                break

    cover = ""
    for tag in re.findall(r'<img\b[^>]*>', card, re.I | re.S):
        src = (megaknigi_attr(tag, "src") or megaknigi_attr(tag, "data-src")
               or megaknigi_attr(tag, "data-original") or megaknigi_attr(tag, "data-lazy-src"))
        if src and not src.startswith("data:"):
            full = urllib.parse.urljoin(MEGAKNIGI_BASE, src)
            if "/storage/cover/" in full.lower():
                cover = full
                break

    return [{
        "title": title,
        "author": "",
        "isbn": "",
        "cover": cover,
        "source": "MegaKnigi",
        "url": href
    } for href, _ in links]


def megaknigi_extract_jsonld(html):
    authors, images = [], []
    for raw in re.findall(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S
    ):
        try:
            obj = json.loads(raw.strip())
        except Exception:
            continue
        objs = obj if isinstance(obj, list) else [obj]
        for o in objs:
            if not isinstance(o, dict):
                continue
            a = o.get("author")
            if isinstance(a, dict) and a.get("name"):
                authors.append(str(a["name"]))
            elif isinstance(a, list):
                for x in a:
                    if isinstance(x, dict) and x.get("name"):
                        authors.append(str(x["name"]))
                    elif isinstance(x, str):
                        authors.append(x)
            elif isinstance(a, str):
                authors.append(a)
            im = o.get("image")
            if isinstance(im, str):
                images.append(im)
            elif isinstance(im, dict) and im.get("url"):
                images.append(str(im["url"]))
            elif isinstance(im, list):
                images.extend([str(x) for x in im if isinstance(x, str)])
    return authors, images


def megaknigi_enrich(item):
    # The detail page is the authoritative source for author and cover.
    try:
        status, _, html = megaknigi_fetch(item["url"])
    except Exception:
        return item

    authors, images = megaknigi_extract_jsonld(html)
    author = ""
    for a in authors:
        a = megaknigi_clean(a)
        if a and a.lower() not in {"проза", "детская литература", "фэнтези", "фантастика"}:
            author = a
            break

    if not author:
        for p in [
            r'<[^>]+class=["\'][^"\']*author[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            r'(?:Автор|Авторы)\s*[:\-]\s*([^<]{2,160})',
        ]:
            m = re.search(p, html, re.I | re.S)
            if m:
                cand = megaknigi_clean(m.group(1))
                if cand and cand.lower() not in {"проза", "детская литература"}:
                    author = cand
                    break
    if author:
        item["author"] = author

    detail_cover = ""
    for m in re.finditer(
        r'<meta\b[^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*>',
        html, re.I
    ):
        detail_cover = megaknigi_attr(m.group(0), "content")
        if detail_cover:
            break
    if not detail_cover and images:
        detail_cover = images[0]
    if detail_cover:
        item["cover"] = urllib.parse.urljoin(MEGAKNIGI_BASE, detail_cover)

    return item


def megaknigi(q):
    q = (q or "").strip()
    if not q:
        return []

    url = MEGAKNIGI_BASE + "/search?" + urllib.parse.urlencode({"q": q})
    try:
        _, _, html = megaknigi_fetch(url)
    except Exception:
        return []

    results, seen = [], set()
    for block in megaknigi_card_blocks(html):
        for item in megaknigi_parse_card(block):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            results.append(item)

    # Fallback if MegaKnigi changes its card class.
    if not results:
        for m in re.finditer(
            r'<a\b[^>]*href=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*>(.*?)</a>',
            html, re.I | re.S
        ):
            href = urllib.parse.urljoin(MEGAKNIGI_BASE, m.group(1))
            if href in seen:
                continue
            txt = megaknigi_clean(m.group(2))
            if txt and txt.lower() not in {"читать", "скачать"}:
                seen.add(href)
                results.append({
                    "title": txt, "author": "", "isbn": "", "cover": "",
                    "source": "MegaKnigi", "url": href
                })

    # Enrich the first 10 records with authoritative detail-page metadata.
    for i in range(min(3, len(results))):
        results[i] = megaknigi_enrich(results[i])

    return results[:30]


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
            raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=5)
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
            raw, _ = http_get(url, headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://www.bookvoed.ru/search"}, timeout=5)
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
                raw, _ = http_get(engine_url, headers={"Accept": "text/html", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}, timeout=5)
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


def _source_call(name, fn, q):
    t0 = time.monotonic()
    try:
        data = fn(q) or []
        return name, data, round(time.monotonic() - t0, 2), "ok"
    except Exception as e:
        return name, [], round(time.monotonic() - t0, 2), str(e)[:180]


def search(q):
    q = (q or "").strip()
    if not q:
        return [], {}

    # Run independent catalogs in parallel so one slow site cannot block all search.
    jobs = {
        "Google Books": google,
        "Open Library": openlibrary,
        "OPDS": opds,
        "MegaKnigi": megaknigi,
        "Буквоед": bookvoed_search,
    }
    pool = ThreadPoolExecutor(max_workers=5)
    futures = {pool.submit(_source_call, name, fn, q): name for name, fn in jobs.items()}
    allr = []
    status = {}
    deadline = time.monotonic() + 9
    try:
        while futures and time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            done = []
            for fut in as_completed(list(futures), timeout=remaining):
                done.append(fut)
            for fut in done:
                name = futures.pop(fut)
                try:
                    src, data, seconds, state = fut.result()
                except Exception as e:
                    src, data, seconds, state = name, [], 0, str(e)[:180]
                allr.extend(data)
                status[src] = {"count": len(data), "seconds": seconds, "status": state}
            if not futures:
                break
    except TimeoutError:
        pass
    finally:
        for fut, name in list(futures.items()):
            fut.cancel()
            status[name] = {"count": 0, "seconds": 9, "status": "timeout"}
        pool.shutdown(wait=False, cancel_futures=True)

    for x in allr:
        x["_score"] = score(q, x)
    return dedup(allr), status


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/api/search":
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
            try:
                results, status = search(q)
                body = json.dumps({"query": q, "results": results, "sources": status}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"query": q, "results": [], "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body)
            return
        if u.path == "/api/health":
            body = json.dumps({"ok": True, "sources": {"google": "enabled", "openlibrary": "enabled", "opds": len(SOURCES.get("opds", [])), "labirint": "enabled", "megaknigi": "enabled", "bookvoed": "enabled"}}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body); return
        return super().do_GET()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8765"))
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
