"""Open the review page at an address where its annotations persist.

Opening the HTML straight from the filesystem works, but a browser may refuse
localStorage for a file URL, and then a review is lost on refresh. Serving the one
directory removes that variable, so this is the recommended way in.

Usage::

    python evals/knowledge_v2/open_review.py
    python evals/knowledge_v2/open_review.py --port 8790 --no-browser
"""

from __future__ import annotations

import argparse
import http.server
import socketserver
import threading
import webbrowser
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PAGE_DIR = REPO_ROOT / "reports" / "knowledge-v2" / "review"
PAGE = PAGE_DIR / "index.html"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the stdlib signature
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()

    if not PAGE.is_file():
        print(f"No page at {PAGE}.")
        print("Build it first: python evals/knowledge_v2/build_review_page.py")
        return 2

    handler = partial(QuietHandler, directory=str(PAGE_DIR))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", arguments.port), handler) as server:
        url = f"http://127.0.0.1:{arguments.port}/index.html"
        print(f"复核页面: {url}")
        print("批注保存在这个地址的浏览器存储里；改完点「导出批注」，把 JSON 交回来。")
        print("按 Ctrl+C 结束。")
        if not arguments.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
