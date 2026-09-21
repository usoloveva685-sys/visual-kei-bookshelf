import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path == "/" or path == "":
            index = ROOT / "index.html"
            if index.exists():
                return str(index)
            return str(STATIC / "index.html")

        root_file = ROOT / path.lstrip("/")
        if root_file.exists():
            return str(root_file)

        return str(STATIC / path.lstrip("/"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(
                parsed.query
            ).get("q", [""])[0]

            body = json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "results": [],
                    "diagnostic": "API_SEARCH_REACHED",
                },
                ensure_ascii=False,
            ).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()
            self.wfile.write(body)
            return

        return super().do_GET()


def main():
    port = 10000

    try:
        port = int(os.environ.get("PORT", port))
    except Exception:
        pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler,
    )

    print(f"Книжный шкаф: диагностический сервер запущен на порту {port}")
    print("API /api/search: DIAGNOSTIC MODE")

    server.serve_forever()


if __name__ == "__main__":
    main()
