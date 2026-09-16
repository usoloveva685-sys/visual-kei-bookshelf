#!/usr/bin/env python3
# Минимальный сервер-агрегатор для книжного шкафа.
# Python 3.10+; только стандартная библиотека.
import json, re, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parent
SOURCES=json.loads((ROOT/"sources.json").read_text(encoding="utf-8"))

def http_get(url, headers=None, timeout=12):
    req=urllib.request.Request(url, headers={"User-Agent":"VisualKeiBookshelf/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()

def google(q):
    url="https://www.googleapis.com/books/v1/volumes?maxResults=20&printType=books&q="+urllib.parse.quote(q)
    data,_=http_get(url)
    j=json.loads(data)
    out=[]
    for v in j.get("items",[]):
        x=v.get("volumeInfo",{})
        ids=x.get("industryIdentifiers",[])
        isbn=next((z.get("identifier","") for z in ids if z.get("identifier")), "")
        cover=(x.get("imageLinks") or {}).get("thumbnail","").replace("http:","https:")
        out.append({"title":x.get("title",""),"author":", ".join(x.get("authors",[])),
                    "isbn":isbn,"cover":cover,"source":"Google Books"})
    return out

def openlibrary(q):
    url="https://openlibrary.org/search.json?limit=20&q="+urllib.parse.quote(q)
    data,_=http_get(url)
    j=json.loads(data)
    out=[]
    for x in j.get("docs",[]):
        cover=f"https://covers.openlibrary.org/b/id/{x['cover_i']}-M.jpg" if x.get("cover_i") else ""
        out.append({"title":x.get("title",""),"author":", ".join(x.get("author_name",[])),
                    "isbn":(x.get("isbn") or [""])[0],"cover":cover,"source":"Open Library"})
    return out

ATOM={"atom":"http://www.w3.org/2005/Atom","os":"http://a9.com/-/spec/opensearch/1.1/"}

def opds_search(root,q):
    raw,_=http_get(root)
    rootxml=ET.fromstring(raw)
    search=None
    for link in rootxml.findall(".//{http://www.w3.org/2005/Atom}link"):
        if link.attrib.get("rel")=="search" and "opensearchdescription+xml" in link.attrib.get("type",""):
            search=urllib.parse.urljoin(root,link.attrib.get("href",""))
            break
    if not search: return []
    raw,_=http_get(search)
    desc=ET.fromstring(raw)
    template=None
    for u in desc.findall("{http://a9.com/-/spec/opensearch/1.1/}Url"):
        if "{searchTerms}" in u.attrib.get("template",""):
            template=u.attrib["template"]; break
    if not template: return []
    url=template.replace("{searchTerms}",urllib.parse.quote(q))
    raw,_=http_get(urllib.parse.urljoin(root,url))
    feed=ET.fromstring(raw)
    out=[]
    for e in feed.findall("{http://www.w3.org/2005/Atom}entry"):
        title=(e.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        author=(e.findtext("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name") or "").strip()
        ident=(e.findtext("{http://purl.org/dc/elements/1.1/}identifier") or "").strip()
        cover=""
        for l in e.findall("{http://www.w3.org/2005/Atom}link"):
            rel=l.attrib.get("rel","")
            if "image" in rel:
                cover=urllib.parse.urljoin(root,l.attrib.get("href","")); break
        if title:
            out.append({"title":title,"author":author,"isbn":ident if re.fullmatch(r"97[89]\d{10}",re.sub(r"[- ]","",ident)) else "",
                        "cover":cover,"source":"OPDS"})
    return out

def score(q,x):
    ql=q.lower()
    t=x.get("title","").lower()
    a=x.get("author","").lower()
    if ql==t: return 100
    if ql in t: return 80
    if ql in a: return 60
    words=[w for w in re.findall(r"\w+",ql) if len(w)>2]
    return 20+sum(w in (t+" "+a) for w in words)*5

def search(q):
    allr=[]
    for fn in (google,openlibrary):
        try: allr.extend(fn(q))
        except Exception: pass
    for name,url in SOURCES.get("opds",[]):
        try:
            rr=opds_search(url,q)
            for x in rr: x["source"]=name+" (OPDS)"
            allr.extend(rr)
        except Exception: pass
    # Deduplicate by normalized title+author+isbn.
    seen=set(); out=[]
    for x in sorted(allr,key=lambda z:score(q,z),reverse=True):
        key=(re.sub(r"\W","",x.get("title","").lower()),
             re.sub(r"\W","",x.get("author","").lower()),
             x.get("isbn",""))
        if key in seen: continue
        seen.add(key); out.append(x)
    return out[:40]

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def do_GET(self):
        u=urllib.parse.urlsplit(self.path)
        if u.path=="/api/search":
            q=urllib.parse.parse_qs(u.query).get("q",[""])[0].strip()
            body=json.dumps({"query":q,"results":search(q)},ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body); return
        return super().do_GET()

if __name__=="__main__":
    import os
    port=int(os.environ.get("PORT","8765"))
    # Render requires the service to listen on 0.0.0.0 and the assigned PORT.
    Handler.directory=str(ROOT / "static")
    print(f"Книжный шкаф: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
