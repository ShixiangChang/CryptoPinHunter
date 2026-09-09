# -*- coding: utf-8 -*-
"""serve.py —— 看板 HTTP 服务。

只托管 data/ 下的 .html 文件（不含数据库/密钥），默认端口 8777。
- 默认只绑 127.0.0.1（公网访问不了）；设环境变量 DASH_HOST=0.0.0.0 才对外。
- 环境变量 DASH_PASS 非空时启用 Basic Auth（对外访问时强烈建议）。

访问 http://127.0.0.1:8777 打开实盘看板。
"""
from __future__ import annotations

import base64
import http.server
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PORT = int(os.environ.get("PORT", "8777"))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import config

HOST = config.DASH_HOST
USER = config.DASH_USER
PASS = config.DASH_PASS


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DATA), **kw)

    def _authorized(self) -> bool:
        if not PASS:
            return True  # 未设密码，不启用鉴权（仅本机可访问）
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                return decoded == f"{USER}:{PASS}"
            except Exception:
                return False
        return False

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="dashboard"')
            self.end_headers()
            return
        if self.path in ("/", "/index.html"):
            self.path = "/live_dashboard.html"
        if not self.path.endswith(".html"):
            self.send_error(403, "Forbidden")
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # 静默，减少日志噪声


if __name__ == "__main__":
    auth_hint = "Basic Auth 开启" if PASS else "无鉴权（仅本机可访问）"
    print(f"[serve] 看板 http://{HOST}:{PORT}  ({auth_hint})")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
