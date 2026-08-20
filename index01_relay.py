"""index01-relay — Pebble Index 01 voice note -> NemoClaw turn, gated by Telegram approval.

See SPEC.md. Two spec assumptions were resolved against the installed toolchain
(NemoClaw v0.0.102 / OpenClaw 2026.7.1) and shaped this implementation:

Q4  `openclaw agent` has no dry-run or plan-emit mode. The agent cannot show an
    intended tool call before running it, so the spec's stated fallback applies:
    the approval gate covers the ENTIRE turn, and the proposal shows the verbatim
    transcript rather than a tool name plus arguments. Classification is done on
    the transcript by the static rules in classify(), never by asking the model.

Q5  The relay owns every Telegram message it sends and every update it consumes.
    The agent is never invoked with --deliver, so it cannot reply to Telegram out
    of band and cannot route around the gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required environment variable {name}")
    return val or ""


INDEX01_SECRET = _env("INDEX01_SECRET", required=True)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_OWNER_ID = int(_env("TELEGRAM_OWNER_ID", required=True))
TRIGGER_PHRASE = _env("TRIGGER_PHRASE", "hey nemo").strip().lower()
NEMOCLAW_TIMEOUT = int(_env("NEMOCLAW_TIMEOUT", "300"))
APPROVAL_TTL = int(_env("APPROVAL_TTL", "900"))
DB_PATH = _env("DB_PATH", "/var/lib/index01/jobs.db")
LOG_TRANSCRIPTS = _env("LOG_TRANSCRIPTS", "false").lower() == "true"
# Route every turn through the approval gate, including ones the classifier would
# pass as read-only. Defaults on because a read-only turn currently runs on an
# agent that can still mutate (see NEMOCLAW_AGENT_RO): until a restricted agent
# exists, "read-only" describes the phrasing, not the capability. Set to false
# once NEMOCLAW_AGENT_RO points at an agent that genuinely cannot change anything.
REQUIRE_APPROVAL_ALL = _env("REQUIRE_APPROVAL_ALL", "true").lower() == "true"

# How to reach NemoClaw. NEMOCLAW_CMD is a JSON list used as an argv prefix so the
# privilege boundary is configuration, not code: point it straight at the binary
# when the relay can reach it, or at a sudo wrapper when the relay runs as a
# different user than the one that owns the NemoClaw state directory.
NEMOCLAW_CMD: list[str] = json.loads(
    _env("NEMOCLAW_CMD", '["/home/democenter/.local/bin/nemoclaw"]')
)
NEMOCLAW_SANDBOX = _env("NEMOCLAW_SANDBOX", "the-king")
# Read-only turns and mutating turns can be routed to different in-sandbox agents
# so that S6 (narrowest viable toolset) is enforceable per class. Until a
# restricted agent exists, both default to the sandbox's `main` agent -- see the
# residual-risk note in SPEC.md section 7.
NEMOCLAW_AGENT_RO = _env("NEMOCLAW_AGENT_RO", "main")
NEMOCLAW_AGENT_RW = _env("NEMOCLAW_AGENT_RW", "main")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# The Telegram bot token is a path segment of every API URL, so anything that
# logs a URL logs a live credential (S8). httpx does exactly that at INFO for
# each request, and its exception reprs embed the URL too, so the error paths in
# tg_call() leak even with httpx quietened. Redact at the handler, where every
# emitter -- ours, httpx, uvicorn -- has to pass through.
_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")

# Literal secrets worth catching wherever they surface. INDEX01_SECRET matters if
# the Pebble app cannot send custom headers and it has to move into the URL path
# (SPEC Q2), because uvicorn's access log records request paths verbatim.
_LITERAL_SECRETS = tuple(s for s in (INDEX01_SECRET, TELEGRAM_BOT_TOKEN) if len(s) >= 8)


class _RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_RE.sub("bot<redacted>", message)
        for secret in _LITERAL_SECRETS:
            if secret in redacted:
                redacted = redacted.replace(secret, "<redacted>")
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to every handler that can emit a record.

    Called at import and again at startup: uvicorn installs its own handlers
    (notably the access log) after this module is imported, and a filter on the
    root logger does not cover them.
    """
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")
    for name in names:
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, _RedactSecrets) for f in handler.filters):
                handler.addFilter(_RedactSecrets())


install_log_redaction()

# One line per poll, every 25 seconds, forever. Noise even once redacted.
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("index01")

# --------------------------------------------------------------------------
# Job states
# --------------------------------------------------------------------------

RECEIVED = "RECEIVED"
FILTERED = "FILTERED"
PLANNING = "PLANNING"
PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
EXECUTING = "EXECUTING"
REPORTED = "REPORTED"
DENIED = "DENIED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"

# Jobs that are waiting for the proposal message to reach Telegram. Kept distinct
# from PROPOSED so that a Telegram outage is retried rather than silently losing
# the proposal; a job only becomes PROPOSED once the owner can actually see it.
PENDING_NOTIFY = "PENDING_NOTIFY"

# --------------------------------------------------------------------------
# Classification (static; never delegated to the model)
# --------------------------------------------------------------------------

# Any of these in the transcript forces the approval gate, even if the phrasing
# also looks like a question. Checked first and deliberately broad: a false
# positive costs one button tap, a false negative runs unreviewed.
MUTATING_PATTERNS = re.compile(
    r"\b("
    r"restart|reboot|shutdown|shut\s?down|power\s?(off|cycle)|"
    r"stop|start|kill|terminate|destroy|drop|"
    r"delete|remove|rm|purge|wipe|format|truncate|clear|clean\s?up|"
    r"install|uninstall|upgrade|update|patch|deploy|rollback|roll\s?back|"
    r"create|make|write|edit|modify|change|rename|move|copy|"
    r"set|enable|disable|turn\s?(on|off)|toggle|"
    r"send|email|e-?mail|message|text|post|publish|tweet|reply|forward|"
    r"push|commit|merge|revert|rebase|tag|release|"
    r"chmod|chown|mount|unmount|umount|"
    r"scale|provision|allocate|resize|migrate|restore|"
    r"revoke|rotate|reset|grant|deny|ban|block|"
    r"rid|flush|empty|archive|"
    r"pay|transfer|order|buy|purchase|refund|cancel|book|schedule"
    r")\b",
    re.IGNORECASE,
)

# A transcript must match one of these to skip the gate. Anchored at the start of
# the request so that "check the disk" qualifies but "delete it, and check" does
# not reach here at all (the mutating check above fires first).
#
# `do` is deliberately not a bare opener: interrogative "do you know the uptime"
# is read-only, but imperative "do the thing from yesterday" is not, and the two
# are only distinguishable by what follows.
READONLY_PATTERNS = re.compile(
    r"^\s*(?:"
    r"what|what's|whats|which|who|whose|when|where|why|how|"
    r"is|are|was|were|does|did|can|could|should|"
    r"do\s+(?:you|we|i|they|it)\b|"
    r"show|list|get|read|check|display|print|report|"
    r"tell\s+me|give\s+me|describe|explain|summar(?:y|ise|ize)|"
    r"status|uptime|health|load|temperature|temp|disk|memory|ram|gpu|cpu"
    r")\b",
    re.IGNORECASE,
)


def classify(request_text: str) -> bool:
    """Return True when the turn requires an approval tap.

    Fail closed: anything that is not affirmatively recognised as a read-only
    question requires approval.
    """
    if REQUIRE_APPROVAL_ALL:
        return True
    if MUTATING_PATTERNS.search(request_text):
        return True
    if READONLY_PATTERNS.match(request_text):
        return False
    return True


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_db_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    assert _db is not None, "database not initialised"
    return _db


def init_db() -> None:
    global _db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id        TEXT PRIMARY KEY,
            dedup_key     TEXT NOT NULL UNIQUE,
            transcript    TEXT NOT NULL,
            request_text  TEXT NOT NULL,
            state         TEXT NOT NULL,
            needs_approval INTEGER NOT NULL,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            message_id    INTEGER,
            result        TEXT,
            error         TEXT
        );
        CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        """
    )
    conn.commit()
    _db = conn


def kv_get(key: str, default: str | None = None) -> str | None:
    with _db_lock:
        row = db().execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def kv_set(key: str, value: str) -> None:
    with _db_lock:
        db().execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        db().commit()


def job_get(job_id: str) -> sqlite3.Row | None:
    with _db_lock:
        return db().execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def job_by_dedup(dedup_key: str) -> sqlite3.Row | None:
    with _db_lock:
        return db().execute(
            "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()


def job_create(
    job_id: str,
    dedup_key: str,
    transcript: str,
    request_text: str,
    state: str,
    needs_approval: bool,
) -> bool:
    """Insert a job. Returns False when dedup_key already exists (S11)."""
    now = time.time()
    try:
        with _db_lock:
            db().execute(
                "INSERT INTO jobs(job_id, dedup_key, transcript, request_text, state,"
                " needs_approval, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    dedup_key,
                    transcript,
                    request_text,
                    state,
                    int(needs_approval),
                    now,
                    now,
                ),
            )
            db().commit()
        return True
    except sqlite3.IntegrityError:
        return False


def job_update(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _db_lock:
        db().execute(
            f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id)
        )
        db().commit()


def jobs_in_state(state: str) -> list[sqlite3.Row]:
    with _db_lock:
        return db().execute("SELECT * FROM jobs WHERE state = ?", (state,)).fetchall()


# --------------------------------------------------------------------------
# Payload parsing
# --------------------------------------------------------------------------

# SPEC Q1, from Pebble's published webhook documentation. The real request is
# multipart/form-data -- NOT JSON -- with these fields:
#
#   transcription  text, present when transcription succeeded and text sending is on
#   audio          audio/mp4, present when audio sending is on (we never read it)
#   recordedAt     milliseconds since epoch, always present
#   client         text, always present, value 'ring'
#
# Confirmed against a real delivery from the ring:
#   {"transcription": "...", "recordedAt": "1787261486034", "client": "ring"}
#
# Narrow on purpose. A fallback chain would let an unexpected payload silently
# match some other key and run the agent on the wrong text; with one key per
# field, a schema change fails visibly instead. `transcript` and `id` are kept
# solely for JSON callers (curl, the test suite), which never see the real shape.
TRANSCRIPT_KEYS = ("transcription", "transcript")
RECORDING_ID_KEYS = ("recordedAt", "id")


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_transcript(payload: dict[str, Any]) -> str | None:
    # Flat lookup only. The real payload has no nesting, and guessing at wrapper
    # keys risks running the agent on whatever text happens to be found.
    return _first_str(payload, TRANSCRIPT_KEYS)


def extract_dedup_key(payload: dict[str, Any], transcript: str) -> str:
    recording_id = _first_str(payload, RECORDING_ID_KEYS)
    if recording_id:
        # Pair the timestamp with a transcript digest: recordedAt alone is a
        # millisecond clock reading, and two distinct notes sharing one would
        # silently swallow the second as a duplicate.
        digest = hashlib.sha256(transcript.encode()).hexdigest()[:16]
        return f"id:{recording_id}:{digest}"
    # No identifier at all: hash the transcript with a minute-truncated timestamp,
    # per SPEC 6.1. Weaker -- a genuine repeat within the same minute is dropped.
    minute = int(time.time() // 60)
    digest = hashlib.sha256(f"{transcript}|{minute}".encode()).hexdigest()
    return f"hash:{digest}"


async def parse_payload(request: Request) -> tuple[dict[str, Any], int]:
    """Return (text fields, count of binary parts) from the webhook body.

    Pebble posts multipart/form-data. JSON is still accepted so the endpoint can
    be driven by curl and by the test suite without building a multipart body.
    Raises ValueError on anything unparseable, which the caller turns into a 400.
    """
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:  # Starlette raises assorted parse errors
            raise ValueError(f"malformed multipart body: {exc}") from exc

        payload: dict[str, Any] = {}
        audio_parts = 0
        try:
            for key, value in form.multi_items():
                if isinstance(value, str):
                    payload[key] = value
                else:
                    # An UploadFile -- the audio part. Deliberately not read.
                    audio_parts += 1
        finally:
            await form.close()
        return payload, audio_parts

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    return payload, 0


def strip_trigger(transcript: str) -> str:
    """Remove the trigger phrase, returning the actual request."""
    lowered = transcript.lower()
    idx = lowered.find(TRIGGER_PHRASE)
    if idx == -1:
        return transcript.strip()
    tail = transcript[idx + len(TRIGGER_PHRASE) :]
    return tail.lstrip(" ,.:;-—").strip()


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    assert _http is not None, "http client not initialised"
    return _http


def _clip(text: str, limit: int = 3500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


async def tg_call(method: str, **payload: Any) -> dict[str, Any] | None:
    try:
        resp = await http().post(f"{TELEGRAM_API}/{method}", json=payload, timeout=30)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return None
    if not data.get("ok"):
        log.warning("telegram %s rejected: %s", method, data.get("description"))
        return None
    return data.get("result")


async def tg_send(text: str, reply_markup: dict[str, Any] | None = None) -> int | None:
    payload: dict[str, Any] = {"chat_id": TELEGRAM_OWNER_ID, "text": _clip(text)}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = await tg_call("sendMessage", **payload)
    return result.get("message_id") if result else None


def approval_keyboard(job_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"a:{job_id}"},
                {"text": "🚫 Deny", "callback_data": f"d:{job_id}"},
            ]
        ]
    }


def proposal_text(job: sqlite3.Row) -> str:
    # The verbatim transcript is the point of this message (SPEC 7, build guide
    # 4.1): it is the only way a mis-heard word is visible before approval.
    expires_in = max(0, int(job["created_at"] + APPROVAL_TTL - time.time()))
    return (
        "🔐 Approval required\n\n"
        f"Heard: “{job['transcript']}”\n\n"
        f"Would run a full agent turn in sandbox “{NEMOCLAW_SANDBOX}” "
        f"(agent: {NEMOCLAW_AGENT_RW}) with:\n"
        f"“{job['request_text']}”\n\n"
        "The agent may call any tool its sandbox policy permits — this gate "
        "covers the whole turn, not a single tool call.\n\n"
        f"Expires in {expires_in // 60}m {expires_in % 60}s."
    )


async def send_proposal(job: sqlite3.Row) -> None:
    message_id = await tg_send(proposal_text(job), approval_keyboard(job["job_id"]))
    if message_id is None:
        # Telegram unreachable: leave the job in PENDING_NOTIFY so the poll loop
        # retries. Never auto-approve (SPEC 10).
        log.warning("proposal for %s not delivered; will retry", job["job_id"])
        return
    job_update(job["job_id"], state=PROPOSED, message_id=message_id)
    log.info("job %s proposed (message %s)", job["job_id"], message_id)


# --------------------------------------------------------------------------
# Agent invocation
# --------------------------------------------------------------------------


async def run_agent(job_id: str, request_text: str, agent_id: str) -> tuple[bool, str]:
    """Run one agent turn. Returns (ok, output).

    The transcript is untrusted, so the process is always spawned with an explicit
    argument list and never through a shell (SPEC 6.3). --deliver is never passed:
    the relay owns all Telegram output, so the agent cannot reply around the gate.

    The message goes inline via -m, not --message-file. `nemoclaw <name> agent` is
    a pass-through to `openclaw agent` running *inside* the sandbox container, so
    --message-file is resolved against the container's filesystem and a host path
    is simply not there -- verified: an absolute host path fails with ENOENT while
    the same path is confirmed absent inside the sandbox. Inline text does put the
    transcript in this process's argv, and so briefly in `ps` output; on a
    single-owner host that is acceptable, and the transcript is already stored in
    SQLite and sent to Telegram.
    """
    argv = [
        *NEMOCLAW_CMD,
        NEMOCLAW_SANDBOX,
        "agent",
        "--agent",
        agent_id,
        # One stateless session per job: conversational context across turns is a
        # non-goal (SPEC 3) and sharing a session would let one note's content
        # influence the next.
        "--session-key",
        f"index01-{job_id}",
        "-m",
        request_text,
        "--timeout",
        str(NEMOCLAW_TIMEOUT),
        "--json",
    ]

    log.info("job %s invoking agent %s", job_id, agent_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return False, f"could not start NemoClaw: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=NEMOCLAW_TIMEOUT + 30
        )
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return False, f"agent timed out after {NEMOCLAW_TIMEOUT}s"

    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        tail = "\n".join(err.splitlines()[-15:]) or "(no stderr)"
        return False, f"exit {proc.returncode}\n{tail}"

    return True, extract_reply(out) or "(agent returned no text)"


# Checked in order at each level, shallowest match wins.
_REPLY_KEYS = ("reply", "response", "answer", "text", "content", "message", "output")


def _search_reply(data: Any) -> str | None:
    """Breadth-first hunt for the reply string in a decoded agent response.

    The response is a deep object whose exact shape is not contractual, so search
    for a plausible field rather than hard-coding a path that a version bump can
    silently break. Breadth-first so a top-level summary wins over a fragment
    buried in per-step metadata.
    """
    queue: list[Any] = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in _REPLY_KEYS:
                val = node.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def extract_reply(stdout: str) -> str | None:
    """Pull the reply text out of `openclaw agent --json` output.

    The JSON is pretty-printed across many lines and preceded by progress output,
    so decode from the first brace to the end rather than per line.
    """
    start = stdout.find("{")
    if start >= 0:
        try:
            data = json.loads(stdout[start:])
        except ValueError:
            data = None
        if data is not None:
            found = _search_reply(data)
            if found:
                return found
    # Unrecognised shape: hand back the raw output rather than swallow it, so a
    # format change shows up in Telegram instead of becoming a silent empty reply.
    return stdout.strip() or None


async def execute_job(job_id: str) -> None:
    job = job_get(job_id)
    if job is None:
        return
    agent_id = NEMOCLAW_AGENT_RW if job["needs_approval"] else NEMOCLAW_AGENT_RO
    job_update(job_id, state=EXECUTING)
    ok, output = await run_agent(job_id, job["request_text"], agent_id)
    if ok:
        job_update(job_id, state=REPORTED, result=output)
        await tg_send(f"✅ Done\n\nHeard: “{job['transcript']}”\n\n{output}")
    else:
        job_update(job_id, state=FAILED, error=output)
        await tg_send(f"❌ Failed\n\nHeard: “{job['transcript']}”\n\n{output}")
    log.info("job %s finished ok=%s", job_id, ok)


# --------------------------------------------------------------------------
# Approval handling
# --------------------------------------------------------------------------


def is_expired(job: sqlite3.Row) -> bool:
    # Checked at tap time, not only by a sweeper, so a restart window cannot let a
    # stale proposal through (build guide 4.3).
    return time.time() - job["created_at"] > APPROVAL_TTL


async def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    sender = (callback.get("from") or {}).get("id")

    # S4: every inbound update is checked against the owner allowlist. The bot is
    # reachable by any Telegram user who finds its username.
    if sender != TELEGRAM_OWNER_ID:
        log.warning("rejected callback from unauthorised user %s", sender)
        await tg_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Not authorised."
        )
        return

    data = callback.get("data") or ""
    if ":" not in data:
        await tg_call("answerCallbackQuery", callback_query_id=callback_id)
        return
    action, job_id = data.split(":", 1)

    job = job_get(job_id)
    if job is None:
        await tg_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Unknown job."
        )
        return

    message_id = job["message_id"]

    async def finalise(note: str) -> None:
        """Remove the buttons and record the outcome in the message itself."""
        if message_id:
            await tg_call(
                "editMessageText",
                chat_id=TELEGRAM_OWNER_ID,
                message_id=message_id,
                text=_clip(f"{proposal_text(job)}\n\n— {note}"),
            )

    if job["state"] != PROPOSED:
        await tg_call(
            "answerCallbackQuery",
            callback_query_id=callback_id,
            text=f"Already {job['state'].lower()}.",
        )
        return

    if is_expired(job):
        job_update(job_id, state=EXPIRED)
        await tg_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Expired."
        )
        await finalise("⌛ Expired, not run.")
        log.info("job %s tapped after expiry", job_id)
        return

    if action == "d":
        job_update(job_id, state=DENIED)
        await tg_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Denied."
        )
        await finalise("🚫 Denied. Nothing was run.")
        log.info("job %s denied", job_id)
        return

    if action == "a":
        job_update(job_id, state=APPROVED)
        await tg_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Approved."
        )
        await finalise("✅ Approved, running…")
        log.info("job %s approved", job_id)
        asyncio.create_task(execute_job(job_id))


async def handle_update(update: dict[str, Any]) -> None:
    if "callback_query" in update:
        await handle_callback(update["callback_query"])
        return
    message = update.get("message")
    if message:
        sender = (message.get("from") or {}).get("id")
        if sender != TELEGRAM_OWNER_ID:
            log.warning("ignoring message from unauthorised user %s", sender)


async def telegram_poll_loop() -> None:
    """Long-poll getUpdates. Chosen over a webhook so no inbound path from the
    public internet is required (SPEC 6.2)."""
    backoff = 1.0
    while True:
        try:
            offset = int(kv_get("tg_offset", "0") or 0)
            resp = await http().get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["callback_query", "message"]),
                },
                timeout=40,
            )
            data = resp.json()

            if not data.get("ok"):
                description = str(data.get("description", ""))
                if resp.status_code == 409 or "conflict" in description.lower():
                    # Another process is polling this same bot token. Telegram
                    # hands each update to only one consumer, so approval taps
                    # would be lost at random. This is fatal to the gate, not a
                    # transient error -- see the bot-sharing note in SPEC 12/Q5.
                    log.error(
                        "getUpdates CONFLICT: another consumer is polling this bot "
                        "token. Approval taps will be dropped. Give the relay its "
                        "own bot. (%s)",
                        description,
                    )
                    await asyncio.sleep(30)
                    continue
                log.warning("getUpdates rejected: %s", description)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue

            backoff = 1.0
            for update in data.get("result", []):
                kv_set("tg_offset", str(update["update_id"] + 1))
                try:
                    await handle_update(update)
                except Exception:
                    log.exception("error handling update %s", update.get("update_id"))

        except (httpx.HTTPError, ValueError) as exc:
            log.warning("getUpdates failed: %s", exc)
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected error in poll loop")
            await asyncio.sleep(5)

        await retry_pending_notifications()


async def retry_pending_notifications() -> None:
    """Resend proposals that never reached Telegram, and expire stale ones."""
    for job in jobs_in_state(PENDING_NOTIFY):
        if is_expired(job):
            job_update(job["job_id"], state=EXPIRED)
            continue
        await send_proposal(job)
    for job in jobs_in_state(PROPOSED):
        if is_expired(job):
            job_update(job["job_id"], state=EXPIRED)
            log.info("job %s expired unapproved", job["job_id"])


# --------------------------------------------------------------------------
# Job intake
# --------------------------------------------------------------------------


async def process_job(job_id: str) -> None:
    job = job_get(job_id)
    if job is None:
        return
    if job["needs_approval"]:
        job_update(job_id, state=PENDING_NOTIFY)
        await send_proposal(job_get(job_id))
    else:
        log.info("job %s classified read-only, executing without gate", job_id)
        await execute_job(job_id)


# --------------------------------------------------------------------------
# HTTP app
# --------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    # uvicorn's handlers, including the access log, exist only by now.
    install_log_redaction()
    init_db()
    _http = httpx.AsyncClient()

    # A job that was mid-flight when the relay died cannot be resumed: the
    # subprocess is gone. Mark it failed rather than leaving it stuck (SPEC 10).
    for job in jobs_in_state(EXECUTING):
        job_update(job["job_id"], state=FAILED, error="relay restarted mid-execution")
        log.warning("job %s marked FAILED after restart", job["job_id"])

    poller = asyncio.create_task(telegram_poll_loop())
    log.info(
        "index01-relay started (sandbox=%s trigger=%r ttl=%ss gate_all=%s transcripts=%s)",
        NEMOCLAW_SANDBOX,
        TRIGGER_PHRASE,
        APPROVAL_TTL,
        REQUIRE_APPROVAL_ALL,
        LOG_TRANSCRIPTS,
    )

    try:
        yield
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        if _http:
            await _http.aclose()


app = FastAPI(title="index01-relay", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/index01")
async def index01(request: Request) -> Response:
    supplied = request.headers.get("x-index01-secret", "")
    # S3: constant-time comparison avoids a timing oracle on the shared secret.
    if not hmac.compare_digest(supplied, INDEX01_SECRET):
        log.warning("rejected webhook with bad secret from %s", request.client)
        return Response(
            content=json.dumps({"ok": False, "error": "unauthorised"}),
            status_code=401,
            media_type="application/json",
        )

    try:
        payload, audio_parts = await parse_payload(request)
    except ValueError as exc:
        log.warning("unparseable payload: %s", exc)
        return Response(
            content=json.dumps({"ok": False, "error": "bad payload"}),
            status_code=400,
            media_type="application/json",
        )

    if audio_parts:
        # S13 and SPEC section 3: transcript only in v1. The audio is never read
        # off the wire, so nothing lands on disk beyond Starlette's own spooling.
        log.info("ignoring %d audio part(s); set the app to send transcription only",
                 audio_parts)

    if LOG_TRANSCRIPTS:
        # Field names only, never values (S7). The webhook fires on every
        # recording, not just ones addressed to the agent, so logging values here
        # would write private notes to syslog unencrypted. The names alone still
        # show immediately if an app update renames a field, which is the only
        # thing this line was ever needed for.
        log.info("payload fields: %s", sorted(payload))

    transcript = extract_transcript(payload)
    if not transcript:
        log.warning("no transcript field found in payload (keys: %s)", list(payload))
        return Response(
            content=json.dumps({"ok": False, "error": "no transcript"}),
            status_code=400,
            media_type="application/json",
        )

    # AC1: a note without the trigger phrase produces no Telegram traffic and no
    # agent run. Filtered before any job is created.
    if TRIGGER_PHRASE not in transcript.lower():
        log.info("note filtered (no trigger phrase)")
        return Response(
            content=json.dumps({"ok": True, "state": FILTERED}),
            status_code=202,
            media_type="application/json",
        )

    dedup_key = extract_dedup_key(payload, transcript)
    existing = job_by_dedup(dedup_key)
    if existing:
        # S11: a webhook retry must not double-fire the agent.
        log.info("duplicate delivery for job %s", existing["job_id"])
        return Response(
            content=json.dumps({"ok": True, "job_id": existing["job_id"], "duplicate": True}),
            status_code=202,
            media_type="application/json",
        )

    request_text = strip_trigger(transcript)
    if not request_text:
        log.info("trigger phrase with empty request, ignoring")
        return Response(
            content=json.dumps({"ok": True, "state": FILTERED}),
            status_code=202,
            media_type="application/json",
        )

    job_id = uuid.uuid4().hex
    needs_approval = classify(request_text)
    if not job_create(
        job_id, dedup_key, transcript, request_text, PLANNING, needs_approval
    ):
        existing = job_by_dedup(dedup_key)
        return Response(
            content=json.dumps(
                {"ok": True, "job_id": existing["job_id"] if existing else None, "duplicate": True}
            ),
            status_code=202,
            media_type="application/json",
        )

    log.info("job %s created (needs_approval=%s)", job_id, needs_approval)

    # The relay must never hold the connection open for the agent turn (SPEC 6.1).
    asyncio.create_task(process_job(job_id))

    return Response(
        content=json.dumps({"ok": True, "job_id": job_id}),
        status_code=202,
        media_type="application/json",
    )
