# Build Guide: Index 01 → NemoClaw Voice Relay

How to build this from scratch on a new host. Every step here was corrected after
the first real build on `relayhost` (2026-08-20) — the original guide was written
against assumptions, and roughly half of them were wrong. `SPEC.md` records why.

**Confidence markers**, because a rebuild guide that overstates itself is worse
than none:

- **[verified]** — done exactly this way on `relayhost` and observed to work.
- **[corrected]** — the original instruction was wrong; this is what actually
  works, established by the failure it caused.
- **[unverified]** — reasoning, not experience. Has not been run on a clean host.

Follow the phases in order. Each has an exit test; don't move on until it passes,
because debugging four layers at once is miserable and the ring makes a poor
debugger.

---

## Phase 0 — Foundations

### 0.1 Confirm the tailnet [verified]

```bash
tailscale status
tailscale status --json | grep DNSName
```

On the phone, confirm the Tailscale app is connected to the same tailnet. Note the
MagicDNS name — you need it for the webhook URL.

If `tailscale status --json` reports `CertDomains: None`, HTTPS certificates are
not enabled for the tailnet and `tailscale serve` will fail. Enable them in the
admin console under **DNS → HTTPS Certificates**.

### 0.2 Service user [corrected]

The original guide created a dedicated `index01` system user. **Don't.** The relay
must exec the NemoClaw CLI, whose binary and state live in the owner's home
directory, and every route around that is worse — see Q6 in `SPEC.md`. The relay
runs as the user who owns the NemoClaw install.

S9 is knowingly unmet as a result. The split-worker design that recovers it is
described in Q6.

```bash
sudo install -d -o "$USER" -g "$USER" -m 0750 /var/lib/index01
sudo install -d -o root -g root -m 0755 /opt/index01
```

### 0.3 Python environment [corrected]

```bash
sudo apt install -y python3-venv
sudo python3 -m venv /opt/index01/venv
sudo /opt/index01/venv/bin/pip install fastapi uvicorn httpx python-multipart
```

**`python-multipart` is not optional.** The Pebble webhook posts
`multipart/form-data`, and Starlette cannot parse a form without it. Omitting it
produces a relay that accepts `curl` JSON perfectly and rejects every real note.

### 0.4 Create the Telegram bot [corrected]

1. **@BotFather** → `/newbot`. Save the token; it is a full credential.
2. `/setprivacy` → Enable.
3. Message the bot once, then read `message.from.id`:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```

**This must be a bot nothing else long-polls.** Telegram hands each update to
exactly one `getUpdates` consumer. Sharing a bot with an existing OpenClaw Telegram
channel means the two steal updates from each other and approval taps vanish at
random — intermittently, which is the worst way for a security control to fail.
The relay logs `getUpdates CONFLICT` loudly if this happens, but the fix is a
second bot. See Q5.

### 0.5 Environment file [verified]

Copy `index01.env.example` to `/etc/index01.env`, `chmod 0600`, `chown root:root`,
and fill it in. Generate the shared secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Leave `REQUIRE_APPROVAL_ALL=true`. See Phase 3 for when it is safe to change.

### 0.6 Install and start [verified]

`setup-phase0.sh` does 0.2, 0.3, 0.5 and this step, and is idempotent.

```bash
sudo ./setup-phase0.sh
sudo systemctl start index01-relay
sudo tailscale serve --bg 8787
```

**Use `serve`, not `funnel`** — funnel publishes to the open internet.

### 0.7 systemd hardening [corrected]

The unit in this repo is the corrected one. Two directives are deliberately
**absent**, and both are the kind a tidy-up would helpfully re-add:

- **`MemoryDenyWriteExecute`** — the NemoClaw CLI is Node. This blocks V8's JIT and
  the agent subprocess dies with an error that does not point back at systemd.
- **`PrivateTmp`** — suspect this first if the relay reaches Telegram but not the
  sandbox; openshell IPC may use a socket under `/tmp`.

And `ReadWritePaths` must include **`~/.config/openshell`**. The CLI rewrites
`active_gateway` on *every* invocation; without write access every turn fails with
`failed to set permissions … Read-only file system`, followed by a misleading
complaint that the active gateway is wrong. It isn't — the CLI just could not
record its choice.

### Exit test [verified]

From the phone on the tailnet, `https://<host>.<tailnet>.ts.net/health` returns
`{"ok": true}`. From a non-tailnet LAN device, `curl http://<lan-ip>:8787/health`
is refused — that confirms the loopback bind (S1, AC8).

---

## Phase 1 — Webhook and payload

### 1.1 Configure the app [verified]

Webhook settings live under the **main Index tab settings** in the Pebble app.

| Field | Value |
|-------|-------|
| URL | `https://<host>.<tailnet>.ts.net/index01` |
| Headers | `x-index01-secret: <INDEX01_SECRET>` |
| Send | transcription only |
| Trigger | **all** |

**Custom headers are supported** — Pebble's docs say they "used to be limited to
just a non-standard auth header but now can be anything", so the path-secret
fallback (Q2) is not needed.

**Set Trigger to "all".** If it is set to a specific button combination you are not
performing, recordings still create notes and the webhook silently never fires.

### 1.2 Expect notes to keep working [verified]

The webhook is sent **in addition to** the app's own agent execution, so notes,
reminders and actions still run on the phone. A note being filed is *not* evidence
the webhook fired.

This also explains a confusing symptom: questions asked without the trigger phrase
still get answered — by the app's on-device LLM, on the phone, with no involvement
from this host at all. G4 in `SPEC.md` holds for free.

### 1.3 Confirm the payload [verified]

```bash
journalctl -u index01-relay -f
```

The journal is systemd's log store, not a file. With `LOG_TRANSCRIPTS=true` the
relay logs payload **field names only** — never values, because the webhook fires
on every recording including private ones (S7).

Observed shape:

```json
{"transcription": "...", "recordedAt": "1787261486034", "client": "ring"}
```

`recordedAt` arrives as a **string**, there is **no per-recording UUID**, and no
`audio` part when Send is transcription-only. If the field names differ on your
version, that log line is how you find out.

### Exit test

A note reaches the relay and returns `202`. A note without the trigger phrase is
filtered with no Telegram traffic (AC1).

---

## Phase 2 — The approval gate

Every refusal path works **before** NemoClaw is wired, because none of them execute
anything. Do these first so a failure here is unambiguous. [verified]

- A mutating note produces a proposal showing the **verbatim transcript** and two
  buttons, and nothing runs (AC3)
- **Deny** disables the buttons and records the outcome in the message (AC4)
- A proposal older than `APPROVAL_TTL` refuses when tapped (AC5)
- A restart with one pending leaves it approvable (AC9)

Showing the transcript is the whole point. "Approve: restart nemo-worker-3" tells
you nothing about whether you actually said that — and in the first hour of real
use the ring produced both "what's the op time?" and "what's the uptime?" from the
same intent.

---

## Phase 3 — Wire NemoClaw

### 3.1 Confirm the CLI by hand first [corrected]

```bash
nemoclaw <sandbox> agent --agent <id> --session-key smoke-1 \
  -m "reply with the single word OK" --timeout 120 --json
```

The original guide gave `nemoclaw run --prompt ...`, which is not a real command.

**Use `-m`, never `--message-file`.** `nemoclaw <name> agent` is a pass-through to
`openclaw agent` running *inside* the sandbox container, so a host path passed to
`--message-file` does not exist there and fails with `ENOENT`. Inline text is safe:
the relay always builds an explicit argv for `create_subprocess_exec` and no shell
ever parses it.

**Never pass `--deliver`.** With it the agent replies to Telegram itself and routes
straight around the gate.

If it works by hand but not under systemd, the cause is the sandboxing in 0.7.

### 3.2 Expect slow, variable turns [verified]

Observed between ~80 s and over 400 s for comparable prompts. `NEMOCLAW_TIMEOUT` is
load-bearing; on expiry the process is killed and `FAILED` is reported to Telegram
rather than hanging silently.

### 3.3 Diagnosing a failed turn [verified]

The subprocess stderr is stored on the job row:

```bash
sqlite3 /var/lib/index01/jobs.db \
  "SELECT state, error FROM jobs ORDER BY created_at DESC LIMIT 1;"
```

A filesystem error naming a path under `$HOME` means that path needs adding to
`ReadWritePaths`.

### 3.4 A genuinely read-only agent [unverified]

`REQUIRE_APPROVAL_ALL=true` gates every turn, including questions, because
`NEMOCLAW_AGENT_RO` defaults to an agent that can still mutate — so a "read-only"
classification describes the phrasing, not the capability.

**Adding an OpenClaw agent will not do it.** `openclaw agents add` isolates
workspace, auth and routing — its only flags are `--agent-dir`, `--workspace`,
`--model` and `--bind`. There is no per-agent tool restriction, so a second agent
in the same sandbox is exactly as powerful as `main`.

Capability lives one level up, per sandbox: network egress via
`nemoclaw <name> policy`, and exec via `openclaw exec-policy`, whose config and
approvals are sandbox-wide files. Closing AC2 therefore means a **second sandbox**:

```bash
# sketch only -- not performed, see SPEC section 7
nemoclaw <ro-sandbox> policy add public-reference     # and little else
nemoclaw <ro-sandbox> exec exec-policy preset deny-all
```

then a `NEMOCLAW_SANDBOX_RO` setting the relay does not yet have —
`NEMOCLAW_AGENT_RO` alone cannot express this. Until that exists,
`REQUIRE_APPROVAL_ALL` stays `true` and the classifier's read-only branch never
runs in production.

### Exit test

Approve on a proposal returns a real answer to Telegram; Deny still leaves no
execution trace.

---

## Phase 4 — Hardening

- `LOG_TRANSCRIPTS=false` once the schema is confirmed. [verified]
- Confirm no credential reaches the journal. `httpx` logs full request URLs at INFO
  and the Telegram token is a **path segment**, so every poll wrote a live token
  until this was fixed. The relay now redacts tokens and the shared secret at every
  log handler and quietens `httpx`. Verify: [verified]
  ```bash
  journalctl -u index01-relay \
    --since "$(systemctl show index01-relay -p ExecMainStartTimestamp --value)" \
    | grep -c "api.telegram.org/bot[0-9]"     # must be 0
  ```
  Anchor to the service start time — a relative window catches the previous
  process's logs and reports a false positive.
- Replay a captured payload twice; the agent must run once (AC7, S11). [unverified]
- Set the Pebble app to local-only STT (S13). [unverified]
- Decide the work-data boundary (S12). Telegram bot chats are not end-to-end
  encrypted. Note `/var/lib/index01/jobs.db` holds every transcript.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Notes are created but nothing reaches the relay | Trigger set to a button combination rather than "all" (1.1) |
| Questions get answered but the GPU is idle | The app's on-device LLM answered on the phone; the note had no trigger phrase (1.2) |
| Every webhook returns `400` | `python-multipart` missing, so the form body cannot be parsed (0.3) |
| `401` in logs | Secret mismatch, or the app is stripping the header |
| Agent fails instantly with a read-only filesystem error | `ReadWritePaths` missing a path under `$HOME`, usually `~/.config/openshell` (0.7) |
| Agent subprocess dies with no useful error | `MemoryDenyWriteExecute` re-added; it breaks V8's JIT (0.7) |
| Relay reaches Telegram but not the sandbox | Try dropping `PrivateTmp`, then `RestrictNamespaces` (0.7) |
| Buttons do nothing, intermittently | Another process polls the same bot token; grep for `getUpdates CONFLICT` (0.4) |
| Agent replies in Telegram without an approval tap | `--deliver` was passed (3.1) |
| Telegram silent | Wrong `chat_id`, or you never messaged the bot first |
| Works on wifi, not mobile data | Tailscale killed by Android battery optimisation |
| Certificate warning | Using the raw tailnet IP rather than the MagicDNS name |

---

## What to verify rather than trust

The three assumptions the original guide flagged are now resolved — Q1, Q4 and the
CLI syntax, all recorded in `SPEC.md`. What remains genuinely uncertain:

1. **The Pebble payload on your app version.** Confirmed once, from documentation
   and one real delivery. The field-names log line (1.3) is how you check.
2. **Pebble's retry and timeout policy** — still undocumented (Q3). Dedup on
   `recordedAt` is implemented and unit-tested but has never met a real retry.
3. **Reboot.** Everything needed is `enabled` and the sandbox container is
   `restart=unless-stopped`, but this has not been exercised end to end.
