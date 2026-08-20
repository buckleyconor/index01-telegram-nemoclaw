# index01-relay

A Pebble Index 01 voice note triggers a NemoClaw agent turn on the GB10, with every
side-effecting action gated behind an explicit approval tap in Telegram.

The ring is an unauthenticated physical trigger with lossy transcription. The design
assumption throughout is that **any given input may be wrong, mis-heard, or issued by
someone who is not the owner**. The approval gate is not a convenience feature; it is
the primary control.

```
Index 01 ──BLE──▶ Pebble app ──HTTPS (tailnet)──▶ index01-relay ──subprocess──▶ NemoClaw
  (ring)          (local STT)                      │
                                                   └──▶ Telegram: proposal + Approve/Deny
```

## Where to look

| Document | For |
|----------|-----|
| **`SPEC.md`** | What it does and *why it is the way it is*. The open-questions section records which assumptions failed and what replaced them — read this before changing anything structural. |
| **`BUILD-GUIDE.md`** | Rebuilding from scratch on a new host, with confidence markers on each step. |
| **`index01_relay.py`** | The relay. Single file. |
| **`tests/`** | Four suites, runnable without credentials — Telegram and the agent are stubbed. |

## Running the tests

```bash
INDEX01_TEST_TMP=/tmp python3 tests/test_acceptance.py   # 42 checks: the full state machine
INDEX01_TEST_TMP=/tmp python3 tests/test_multipart.py    # 18 checks: the real Pebble payload
INDEX01_TEST_TMP=/tmp python3 tests/test_redaction.py    # 10 checks: no credential reaches a log
INDEX01_TEST_TMP=/tmp python3 tests/test_classify.py     # classifier and the gate flag
```

## Operating it

```bash
journalctl -u index01-relay -f                    # watch it work
sqlite3 /var/lib/index01/jobs.db \
  "SELECT state, error FROM jobs ORDER BY created_at DESC LIMIT 1;"   # why a turn failed
```

## Four things that will look like bugs

1. **Every turn asks for approval, even questions.** Deliberate. `REQUIRE_APPROVAL_ALL`
   is on because a "read-only" turn still runs on an agent that could mutate.
   Acceptance criterion 2 is knowingly unmet until a restricted agent exists.
2. **Notes still appear in the Pebble app, and questions still get answered there.**
   The webhook fires *in addition to* the app's own on-device agent. Answers you see
   without saying the trigger phrase came from the phone, not this host.
3. **The gate covers a whole agent turn, not a named tool call.** The agent cannot
   emit a plan before executing (Q4), so the proposal shows the verbatim transcript
   instead. It says so rather than implying precision the design lacks.
4. **Agent turns are slow and variable** — 80 s to over 400 s for similar prompts.

## Two things not to "fix"

Both look like obvious improvements and both break it, in ways whose symptoms do not
point at the cause:

- **`--message-file` instead of `-m`.** The path is resolved *inside* the sandbox
  container, so a host path is never there.
- **Adding `MemoryDenyWriteExecute` to the systemd unit.** The NemoClaw CLI is Node;
  it blocks V8's JIT and kills the agent subprocess.

## Not in this repo

`/etc/index01.env` holds the bot token and shared secret. `/var/lib/index01/jobs.db`
holds every transcript ever spoken to the ring — both are gitignored, and the database
is worth remembering before sharing this directory.
