"""Вход по PIN-коду для админки и эмулятора (проверка для nginx auth_request).

nginx передаёт, какой сайт защищается, в заголовке X-Gate-Site (admin или emulator).
PIN-коды и секрет подписи — только на сервере, в /etc/smartcross/gate.env:
    PIN_ADMIN=...   PIN_EMULATOR=...   GATE_SECRET=<случайная строка>
После верного PIN браузер получает подписанный cookie на 30 дней.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PINS = {"admin": os.environ["PIN_ADMIN"], "emulator": os.environ["PIN_EMULATOR"]}
TITLES = {"admin": "Админка", "emulator": "Эмулятор участка"}
SECRET = os.environ["GATE_SECRET"].encode()
TTL = 30 * 86400


def sign(site: str, exp: int) -> str:
    return hmac.new(SECRET, f"{site}.{exp}".encode(), hashlib.sha256).hexdigest()


def valid(site: str, token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, sign(site, int(exp)))
    except ValueError:
        return False


def safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — вход</title>
<style>
:root{{--bg:#f2f4f3;--card:#fff;--ink:#1c2226;--muted:#66727a;--line:#d5dbd8;--accent:#d99a00;--err:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131618;--card:#1b2023;--ink:#e4e8e9;--muted:#98a3a8;--line:#2c3337;--accent:#f2b705;--err:#ff6b5e}}}}
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:16px/1.5 "Segoe UI",system-ui,sans-serif;padding:16px}}
form{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;width:min(340px,100%);display:flex;flex-direction:column;gap:14px}}
h1{{margin:0;font-size:20px}} p{{margin:0;color:var(--muted);font-size:14px}}
input{{font:600 26px/1 ui-monospace,Consolas,monospace;letter-spacing:.3em;text-align:center;padding:12px;border-radius:10px;
border:1px solid var(--line);background:var(--bg);color:var(--ink);text-transform:uppercase;width:100%}}
input:focus{{outline:2px solid var(--accent);outline-offset:1px}}
button{{font:600 16px "Segoe UI",system-ui,sans-serif;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#1c2226;cursor:pointer}}
.err{{color:var(--err);font-size:14px}}
</style></head><body>
<form method="post" action="/__gate/login">
<h1>{title}</h1><p>Умные переходы Люблино. Введите PIN-код.</p>
<input name="pin" autocomplete="off" autofocus aria-label="PIN-код" maxlength="16" required>
<input type="hidden" name="next" value="{next}">
{error}<button>Войти</button></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "gate"

    def log_message(self, fmt, *args):  # журнал входов — только неудачные попытки
        pass

    def _site(self) -> str:
        site = self.headers.get("X-Gate-Site", "")
        return site if site in PINS else ""

    def _cookie(self, site: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == f"sc_{site}":
                return v
        return ""

    def _page(self, site: str, nxt: str, error: str = "", code: int = 200) -> None:
        body = PAGE.format(title=TITLES[site], next=html.escape(safe_next(nxt)),
                           error=f'<div class="err">{error}</div>' if error else "").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        site = self._site()
        url = urllib.parse.urlparse(self.path)
        if not site:
            self.send_error(400)
        elif url.path == "/check":
            self.send_response(204 if valid(site, self._cookie(site)) else 401)
            self.end_headers()
        elif url.path == "/__gate/login":
            self._page(site, urllib.parse.parse_qs(url.query).get("next", ["/"])[0])
        elif url.path == "/__gate/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", f"sc_{site}=; Path=/; Max-Age=0")
            self.send_header("Location", "/__gate/login")
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        site = self._site()
        if not site or urllib.parse.urlparse(self.path).path != "/__gate/login":
            self.send_error(404)
            return
        n = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        pin = form.get("pin", [""])[0].strip().upper()
        nxt = safe_next(form.get("next", ["/"])[0])
        if hmac.compare_digest(pin, PINS[site].upper()):
            exp = int(time.time()) + TTL
            self.send_response(303)
            self.send_header("Set-Cookie", f"sc_{site}={exp}.{sign(site, exp)}; Path=/; Max-Age={TTL}; HttpOnly; SameSite=Lax")
            self.send_header("Location", nxt)
            self.end_headers()
        else:
            time.sleep(0.7)
            print(f"неверный PIN: сайт {site}, адрес {self.headers.get('X-Real-IP', '?')}", flush=True)
            self._page(site, nxt, "Неверный PIN-код", 401)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8090), Handler).serve_forever()
