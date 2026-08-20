import os, sys

os.environ.update(
    INDEX01_SECRET="x",
    TELEGRAM_BOT_TOKEN="1:x",
    TELEGRAM_OWNER_ID="1",
    DB_PATH=os.environ.get("INDEX01_TEST_TMP", "/tmp") + "/t.db",
    # Exercise the regex classifier itself. The deployed default is true, which
    # short-circuits it; that behaviour is asserted separately at the end.
    REQUIRE_APPROVAL_ALL="false",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from index01_relay import classify, strip_trigger, extract_transcript, extract_dedup_key

READONLY = [
    "what is the uptime on the gb10",
    "what's the gpu utilisation",
    "how much disk is free",
    "show me the running sandboxes",
    "check the load average",
    "is the inference endpoint healthy",
    "tell me the temperature",
    "status of the-king",
    "why is the gpu hot",
    "do you know the uptime",
    "does the endpoint respond",
]

GATED = [
    "restart nemo-worker-3",
    "delete the old snapshots",
    "reboot the gb10",
    "send an email to the team",
    "install the new driver",
    "stop the-king sandbox",
    # read-ish phrasing that also mutates -> must still gate
    "what's the uptime, and restart the worker",
    "check the disk then delete the old logs",
    "show me the logs and clear them",
    # unrecognised phrasing -> fail closed
    "nemo-worker-3 please",
    "do the thing from yesterday",
    "",
    "asdf qwerty",
    "get rid of the old snapshots",
    "empty the trash",
    "please handle that ticket",
]

fails = []
for t in READONLY:
    if classify(t):
        fails.append(f"  FALSE-GATE (should be read-only): {t!r}")
for t in GATED:
    if not classify(t):
        fails.append(f"  MISSED GATE (should require approval): {t!r}")

print("--- strip_trigger ---")
for raw in [
    "hey nemo what is the uptime",
    "Hey Nemo, restart the worker",
    "ok so hey nemo — check the disk",
    "what is the uptime",
]:
    print(f"  {raw!r} -> {strip_trigger(raw)!r}")

print("--- payload parsing ---")
# First entry is the real shape sent by the ring, confirmed from the wire.
for p, want in [
    ({"transcription": "hey nemo hello", "recordedAt": "1787261486034",
      "client": "ring"}, "hey nemo hello"),
    ({"transcript": "hey nemo hello", "id": "rec-1"}, "hey nemo hello"),  # JSON callers
    ({"text": "hey nemo hello"}, None),          # no longer guessed at
    ({"data": {"transcription": "x"}}, None),    # nesting no longer probed
    ({"nothing": 1}, None),
]:
    got = extract_transcript(p)
    ok = "ok " if got == want else "BAD"
    print(f"  [{ok}] {p} -> {got!r} dedup={extract_dedup_key(p, got or '')[:24]!r}")
    if got != want:
        fails.append(f"  parser: {p!r} gave {got!r}, wanted {want!r}")

print("--- classifier ---")
if fails:
    print("FAILURES:")
    print("\n".join(fails))
    sys.exit(1)
print(f"  all {len(READONLY)} read-only and {len(GATED)} gated cases correct")

print("--- REQUIRE_APPROVAL_ALL (deployed default) ---")
import index01_relay

index01_relay.REQUIRE_APPROVAL_ALL = True
still_ungated = [t for t in READONLY if not index01_relay.classify(t)]
if still_ungated:
    print("FAILURES: flag did not force approval for:")
    print("\n".join(f"  {t!r}" for t in still_ungated))
    sys.exit(1)
print(f"  flag forces approval for all {len(READONLY)} read-only transcripts")
