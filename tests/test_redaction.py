"""The bot token must never reach a log record.

Regression test for a real leak: httpx logs the full request URL at INFO, and the
Telegram token is a path segment, so every poll wrote a live credential to the
systemd journal.
"""
import io
import logging
import os
import sys

os.environ.update(
    INDEX01_SECRET="s3cr3t-shared-with-the-pebble-app-abcdef",
    TELEGRAM_BOT_TOKEN="1234567890:AAFakeTokenShapedLikeTheRealOne_x-Y",
    TELEGRAM_OWNER_ID="1",
    DB_PATH=os.environ.get("INDEX01_TEST_TMP", "/tmp") + "/redact.db",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx

import index01_relay as R

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

FAILS = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# Capture through a handler carrying the same filter the module installs.
buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.addFilter(R._RedactSecrets())
root = logging.getLogger()
root.handlers = [handler]
root.setLevel(logging.INFO)


def emitted(fn):
    buf.truncate(0)
    buf.seek(0)
    fn()
    return buf.getvalue()


print("--- every shape the token can reach a log in ---")

out = emitted(lambda: logging.getLogger("index01").info(
    'HTTP Request: GET %s "HTTP/1.1 200 OK"', URL))
check("httpx-style INFO line with token inline", TOKEN not in out, out.strip())

def _err():
    try:
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", URL),
            response=httpx.Response(500),
        )
    except httpx.HTTPError as exc:
        # Mirrors the real handler in tg_call().
        logging.getLogger("index01").warning(
            "telegram %s failed: %s", "getUpdates", f"{exc!r} url={exc.request.url}")

out = emitted(_err)
check("httpx exception repr carrying the request URL", TOKEN not in out, out.strip())

out = emitted(lambda: logging.getLogger("index01").info("polling %s", URL))
check("token passed as a %-format argument", TOKEN not in out, out.strip())

out = emitted(lambda: logging.getLogger("index01").info(f"inline {URL}"))
check("token in an already-formatted f-string", TOKEN not in out, out.strip())

check("redaction marker is present", "bot<redacted>" in out, out.strip())

print("--- shared secret (SPEC Q2 path-secret fallback) ---")
SECRET = os.environ["INDEX01_SECRET"]

out = emitted(lambda: logging.getLogger("uvicorn.access").info(
    '127.0.0.1:0 - "POST /index01/%s HTTP/1.1" 202', SECRET))
check("secret in a uvicorn access-log path", SECRET not in out, out.strip())

out = emitted(lambda: logging.getLogger("index01").warning(
    "rejected webhook with bad secret, expected %s", SECRET))
check("secret in one of our own log calls", SECRET not in out, out.strip())

print("--- module configuration ---")
check("module URL really does embed the token", TOKEN in R.TELEGRAM_API)
check("httpx request logging is quietened",
      logging.getLogger("httpx").level >= logging.WARNING,
      logging.getLogger("httpx").level)
check("a non-token message is left untouched",
      "hello world" in emitted(lambda: logging.getLogger("index01").info("hello world")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all redaction checks passed")
