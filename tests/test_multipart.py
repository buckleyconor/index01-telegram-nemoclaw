"""The Pebble Index 01 posts multipart/form-data, not JSON.

Field names come from Pebble's published webhook docs:
  transcription (text) | audio (audio/mp4) | recordedAt (ms epoch) | client ('ring')
"""
import asyncio
import json
import os
import sys
import time

SCRATCH = os.environ.get("INDEX01_TEST_TMP", "/tmp")
DB = os.path.join(SCRATCH, "multipart.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.unlink(DB + suffix)

os.environ.update(
    INDEX01_SECRET="test-secret",
    TELEGRAM_BOT_TOKEN="1234567890:AAFakeTokenShapedLikeTheRealOne_x-Y",
    TELEGRAM_OWNER_ID="4242",
    TRIGGER_PHRASE="hey nemo",
    DB_PATH=DB,
    LOG_TRANSCRIPTS="false",
    REQUIRE_APPROVAL_ALL="true",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index01_relay as R

SENT, AGENT_RUNS = [], []


async def fake_tg_call(method, **payload):
    SENT.append((method, payload))
    return {"message_id": 1000 + len(SENT)} if method == "sendMessage" else {}


async def fake_run_agent(job_id, request_text, agent_id):
    AGENT_RUNS.append((job_id, request_text, agent_id))
    return True, "ok"


async def fake_poll():
    await asyncio.Event().wait()


R.tg_call, R.run_agent, R.telegram_poll_loop = fake_tg_call, fake_run_agent, fake_poll

from fastapi.testclient import TestClient

FAILS = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


HDR = {"x-index01-secret": "test-secret"}
RECORDED_AT = str(int(time.time() * 1000))


def settle():
    """The route returns 202 immediately and proposes on a background task."""
    time.sleep(0.35)

with TestClient(R.app) as client:
    print("--- the documented Pebble payload shape ---")
    SENT.clear()
    r = client.post("/index01", headers=HDR, files={
        "transcription": (None, "hey nemo restart nemo-worker-3"),
        "recordedAt": (None, RECORDED_AT),
        "client": (None, "ring"),
    })
    settle()
    check("multipart body accepted", r.status_code == 202, f"{r.status_code} {r.text}")
    job_id = r.json().get("job_id")
    check("job created", bool(job_id), r.text)
    job = R.job_get(job_id) if job_id else None
    check("transcription field read as the transcript",
          job and job["transcript"] == "hey nemo restart nemo-worker-3",
          job["transcript"] if job else None)
    check("dedup key derives from recordedAt",
          job and job["dedup_key"].startswith(f"id:{RECORDED_AT}:"),
          job["dedup_key"] if job else None)
    check("gated, not executed", AGENT_RUNS == [], AGENT_RUNS)
    check("proposal sent", any(m == "sendMessage" for m, _ in SENT))

    print("--- audio part is ignored, not read (S13) ---")
    SENT.clear()
    r = client.post("/index01", headers=HDR, files={
        "transcription": (None, "hey nemo check the disk"),
        "audio": ("clip.m4a", b"\x00\x01fake-mp4-bytes", "audio/mp4"),
        "recordedAt": (None, str(int(RECORDED_AT) + 1000)),
        "client": (None, "ring"),
    })
    check("accepted with an audio part present", r.status_code == 202, r.text)
    job2 = R.job_get(r.json()["job_id"])
    check("transcript unaffected by the audio part",
          job2["transcript"] == "hey nemo check the disk", job2["transcript"])

    print("--- S11: a retried delivery is one job ---")
    SENT.clear(); AGENT_RUNS.clear()
    parts = {
        "transcription": (None, "hey nemo restart the worker"),
        "recordedAt": (None, str(int(RECORDED_AT) + 2000)),
        "client": (None, "ring"),
    }
    a = client.post("/index01", headers=HDR, files=parts)
    settle()
    b = client.post("/index01", headers=HDR, files=parts)
    settle()
    check("both return 202", a.status_code == 202 and b.status_code == 202)
    check("second flagged duplicate", b.json().get("duplicate") is True, b.json())
    check("only one proposal sent",
          sum(1 for m, _ in SENT if m == "sendMessage") == 1, SENT)

    print("--- two notes in the same millisecond stay distinct ---")
    same_ms = str(int(RECORDED_AT) + 3000)
    x = client.post("/index01", headers=HDR, files={
        "transcription": (None, "hey nemo restart alpha"),
        "recordedAt": (None, same_ms), "client": (None, "ring")})
    y = client.post("/index01", headers=HDR, files={
        "transcription": (None, "hey nemo restart beta"),
        "recordedAt": (None, same_ms), "client": (None, "ring")})
    settle()
    check("different transcripts are not collapsed",
          x.json()["job_id"] != y.json()["job_id"] and not y.json().get("duplicate"),
          (x.json(), y.json()))

    print("--- trigger filter and auth still apply to multipart ---")
    SENT.clear()
    r = client.post("/index01", headers=HDR, files={
        "transcription": (None, "just a normal note about milk"),
        "recordedAt": (None, str(int(RECORDED_AT) + 4000)), "client": (None, "ring")})
    settle()
    check("note without trigger phrase is filtered", r.status_code == 202, r.text)
    check("no Telegram traffic for a filtered note", SENT == [], SENT)

    r = client.post("/index01", headers={"x-index01-secret": "wrong"}, files={
        "transcription": (None, "hey nemo restart it"),
        "recordedAt": (None, "1"), "client": (None, "ring")})
    check("bad secret still rejected on multipart", r.status_code == 401, r.status_code)

    print("--- transcription missing (audio-only send) ---")
    r = client.post("/index01", headers=HDR, files={
        "audio": ("clip.m4a", b"\x00\x01", "audio/mp4"),
        "recordedAt": (None, "2"), "client": (None, "ring")})
    check("audio-only delivery is a clean 400, not a crash",
          r.status_code == 400, f"{r.status_code} {r.text}")

    print("--- JSON still works, for curl and the other suites ---")
    r = client.post("/index01",
                    headers={**HDR, "content-type": "application/json"},
                    content=json.dumps({"transcript": "hey nemo restart it", "id": "json-1"}))
    check("JSON body still accepted", r.status_code == 202, r.text)
    r = client.post("/index01",
                    headers={**HDR, "content-type": "application/json"},
                    content="{not json")
    check("malformed JSON still 400", r.status_code == 400, r.status_code)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all multipart checks passed")
