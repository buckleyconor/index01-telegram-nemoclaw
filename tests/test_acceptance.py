"""Exercise SPEC.md section 11 acceptance criteria with Telegram and the agent stubbed."""
import asyncio, os, sys, time, json

SCRATCH = os.environ.get("INDEX01_TEST_TMP", "/tmp")
DB = os.path.join(SCRATCH, "acc.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.unlink(DB + suffix)

os.environ.update(
    INDEX01_SECRET="test-secret",
    TELEGRAM_BOT_TOKEN="1:x",
    TELEGRAM_OWNER_ID="4242",
    TRIGGER_PHRASE="hey nemo",
    APPROVAL_TTL="900",
    DB_PATH=DB,
    LOG_TRANSCRIPTS="false",
    # Most criteria below exercise the classifier's two branches, so the
    # short-circuit is off here. The deployed default (true) is asserted at the end.
    REQUIRE_APPROVAL_ALL="false",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index01_relay as R

SENT = []          # every outbound Telegram call
AGENT_RUNS = []    # every agent invocation

async def fake_tg_call(method, **payload):
    SENT.append((method, payload))
    if method == "sendMessage":
        return {"message_id": 1000 + len(SENT)}
    return {}

async def fake_run_agent(job_id, request_text, agent_id):
    AGENT_RUNS.append((job_id, request_text, agent_id))
    return True, f"result for: {request_text}"

async def fake_poll():
    await asyncio.Event().wait()

R.tg_call = fake_tg_call
R.run_agent = fake_run_agent
R.telegram_poll_loop = fake_poll

from fastapi.testclient import TestClient

FAILS = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)

def post(client, body, secret="test-secret", raw=None):
    return client.post(
        "/index01",
        content=raw if raw is not None else json.dumps(body),
        headers={"x-index01-secret": secret, "content-type": "application/json"},
    )

def settle():
    time.sleep(0.35)

with TestClient(R.app) as client:
    print("\n--- auth / payload ---")
    r = post(client, {"transcript": "hey nemo what is the uptime"}, secret="wrong")
    check("bad secret rejected with 401", r.status_code == 401, r.status_code)

    r = post(client, None, raw="{not json")
    check("unparseable payload rejected with 400", r.status_code == 400, r.status_code)

    r = client.get("/health")
    check("/health returns ok", r.status_code == 200 and r.json() == {"ok": True})

    print("\n--- AC1: no trigger phrase -> no traffic, no agent run ---")
    SENT.clear(); AGENT_RUNS.clear()
    r = post(client, {"transcript": "just a normal note about milk", "id": "n1"})
    settle()
    check("returns 202", r.status_code == 202, r.status_code)
    check("no Telegram traffic", SENT == [], SENT)
    check("no agent run", AGENT_RUNS == [], AGENT_RUNS)

    print("\n--- AC2: read-only turn -> result, no approval prompt ---")
    SENT.clear(); AGENT_RUNS.clear()
    r = post(client, {"transcript": "hey nemo what is the uptime", "id": "ro1"})
    settle()
    check("agent ran once", len(AGENT_RUNS) == 1, AGENT_RUNS)
    check("trigger phrase stripped from prompt",
          AGENT_RUNS and AGENT_RUNS[0][1] == "what is the uptime", AGENT_RUNS)
    check("routed to read-only agent",
          AGENT_RUNS and AGENT_RUNS[0][2] == R.NEMOCLAW_AGENT_RO, AGENT_RUNS)
    kb = [p.get("reply_markup") for m, p in SENT if m == "sendMessage"]
    check("no approval buttons offered", all(k is None for k in kb), kb)
    check("result delivered", any("result for" in p.get("text", "") for m, p in SENT if m == "sendMessage"))

    print("\n--- AC3: mutating turn -> proposal, nothing runs before the tap ---")
    SENT.clear(); AGENT_RUNS.clear()
    r = post(client, {"transcript": "hey nemo restart nemo-worker-3", "id": "rw1"})
    settle()
    job_rw = r.json()["job_id"]
    check("nothing executed yet", AGENT_RUNS == [], AGENT_RUNS)
    prop = [p for m, p in SENT if m == "sendMessage"]
    check("proposal sent", len(prop) == 1, prop)
    check("proposal shows verbatim transcript",
          prop and "hey nemo restart nemo-worker-3" in prop[0]["text"])
    check("proposal has Approve and Deny buttons",
          prop and len(prop[0].get("reply_markup", {}).get("inline_keyboard", [[]])[0]) == 2)
    check("job state is PROPOSED", R.job_get(job_rw)["state"] == R.PROPOSED,
          R.job_get(job_rw)["state"])

    print("\n--- AC6: callback from a non-owner is rejected ---")
    SENT.clear(); AGENT_RUNS.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb1", "from": {"id": 9999}, "data": f"a:{job_rw}"}))
    check("stranger's approval did not execute", AGENT_RUNS == [], AGENT_RUNS)
    check("job still PROPOSED", R.job_get(job_rw)["state"] == R.PROPOSED)
    check("no editMessageText from stranger", not any(m == "editMessageText" for m, _ in SENT))

    print("\n--- AC4: Deny leaves no execution trace and disables buttons ---")
    SENT.clear(); AGENT_RUNS.clear()
    r2 = post(client, {"transcript": "hey nemo delete the old snapshots", "id": "rw2"})
    settle()
    job_deny = r2.json()["job_id"]
    SENT.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb2", "from": {"id": 4242}, "data": f"d:{job_deny}"}))
    check("nothing executed", AGENT_RUNS == [], AGENT_RUNS)
    check("state is DENIED", R.job_get(job_deny)["state"] == R.DENIED)
    edits = [p for m, p in SENT if m == "editMessageText"]
    check("buttons removed via editMessageText", len(edits) == 1, SENT)
    check("edited message has no reply_markup", edits and "reply_markup" not in edits[0])

    print("\n--- AC3 cont: Approve executes ---")
    SENT.clear(); AGENT_RUNS.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb3", "from": {"id": 4242}, "data": f"a:{job_rw}"}))
    settle()
    check("agent ran after approval", len(AGENT_RUNS) == 1, AGENT_RUNS)
    check("routed to mutating agent",
          AGENT_RUNS and AGENT_RUNS[0][2] == R.NEMOCLAW_AGENT_RW, AGENT_RUNS)

    print("\n--- double-tap is refused ---")
    SENT.clear(); AGENT_RUNS.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb4", "from": {"id": 4242}, "data": f"a:{job_rw}"}))
    check("second Approve did not re-run", AGENT_RUNS == [], AGENT_RUNS)

    print("\n--- AC5: proposal older than APPROVAL_TTL cannot be approved ---")
    SENT.clear(); AGENT_RUNS.clear()
    r3 = post(client, {"transcript": "hey nemo reboot the gb10", "id": "rw3"})
    settle()
    job_old = r3.json()["job_id"]
    R.job_update(job_old, created_at=time.time() - R.APPROVAL_TTL - 60)
    SENT.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb5", "from": {"id": 4242}, "data": f"a:{job_old}"}))
    check("expired proposal did not execute", AGENT_RUNS == [], AGENT_RUNS)
    check("state is EXPIRED", R.job_get(job_old)["state"] == R.EXPIRED,
          R.job_get(job_old)["state"])
    answers = [p for m, p in SENT if m == "answerCallbackQuery"]
    check("user told it expired", any("xpired" in p.get("text", "") for p in answers), answers)

    print("\n--- AC7: replaying the same payload runs the agent once ---")
    SENT.clear(); AGENT_RUNS.clear()
    body = {"transcript": "hey nemo what is the gpu utilisation", "id": "dup-1"}
    a = post(client, body); settle()
    b = post(client, body); settle()
    check("both deliveries return 202", a.status_code == 202 and b.status_code == 202)
    check("second flagged duplicate", b.json().get("duplicate") is True, b.json())
    check("same job_id returned", a.json()["job_id"] == b.json()["job_id"])
    check("agent ran exactly once", len(AGENT_RUNS) == 1, AGENT_RUNS)

print("\n--- AC9: restart with a pending proposal keeps it approvable ---")
R._db.close()
R._db = None
SENT.clear(); AGENT_RUNS.clear()
with TestClient(R.app) as client:
    job = R.job_get(job_deny)
    check("denied job stayed denied across restart", job["state"] == R.DENIED)
    r4 = post(client, {"transcript": "hey nemo stop the-king sandbox", "id": "rw9"})
    settle()
    job_pending = r4.json()["job_id"]
    check("pending job is PROPOSED", R.job_get(job_pending)["state"] == R.PROPOSED)

R._db.close(); R._db = None
with TestClient(R.app) as client:
    check("proposal survived restart as PROPOSED",
          R.job_get(job_pending)["state"] == R.PROPOSED, R.job_get(job_pending)["state"])
    SENT.clear(); AGENT_RUNS.clear()
    asyncio.run(R.handle_callback(
        {"id": "cb9", "from": {"id": 4242}, "data": f"a:{job_pending}"}))
    settle()
    check("still approvable after restart", len(AGENT_RUNS) == 1, AGENT_RUNS)

    print("\n--- restart marks orphaned EXECUTING jobs FAILED ---")
    R.job_update(job_pending, state=R.EXECUTING)

R._db.close(); R._db = None
with TestClient(R.app) as client:
    check("orphaned EXECUTING job marked FAILED",
          R.job_get(job_pending)["state"] == R.FAILED, R.job_get(job_pending)["state"])

print("\n--- REQUIRE_APPROVAL_ALL: read-only turns are gated too ---")
R.REQUIRE_APPROVAL_ALL = True
with TestClient(R.app) as client:
    SENT.clear(); AGENT_RUNS.clear()
    r5 = post(client, {"transcript": "hey nemo what is the uptime", "id": "gateall-1"})
    settle()
    check("read-only turn did not execute", AGENT_RUNS == [], AGENT_RUNS)
    check("read-only turn is PROPOSED",
          R.job_get(r5.json()["job_id"])["state"] == R.PROPOSED,
          R.job_get(r5.json()["job_id"])["state"])
    prop = [p for m, p in SENT if m == "sendMessage"]
    check("approval buttons offered for a read-only turn",
          prop and len(prop[0].get("reply_markup", {}).get("inline_keyboard", [[]])[0]) == 2)
    check("trigger filter still applies with the flag on",
          post(client, {"transcript": "no trigger here", "id": "gateall-2"}).status_code == 202)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all acceptance checks passed")
