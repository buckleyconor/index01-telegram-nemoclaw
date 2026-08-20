# Spec: Index 01 → NemoClaw Voice Relay with Telegram Approval Gate

**Component name:** `index01-relay`
**Status:** Draft
**Author:** Conor
**Target host:** `relayhost` (Dell Pro Max GB10, arm64)

---

## 1. Purpose

Let a Pebble Index 01 voice note trigger a NemoClaw agent turn on the GB10, with
every side-effecting action gated behind an explicit human approval delivered as
a Telegram inline button.

The ring is an unauthenticated physical trigger with lossy transcription. The
design assumption throughout is that **any given input may be wrong, mis-heard,
or issued by someone who is not the owner**. The approval gate is not a
convenience feature; it is the primary control.

## 2. Goals

| # | Goal |
|---|------|
| G1 | Voice note → agent turn with no phone interaction required |
| G2 | No action with side effects executes without an explicit approval tap |
| G3 | Agent output returns to the owner's phone within one turn |
| G4 | Ring continues to work as a plain notes device for non-agent notes |
| G5 | Runs entirely on the tailnet; nothing published to the public internet |

## 3. Non-goals

- Multi-user support. Single owner, single Telegram account.
- Conversational context across turns. Each voice note is one stateless turn.
- Streaming responses. Replies are delivered as complete messages.
- Handling raw audio. Transcript only in v1.
- High availability. Best-effort; a missed note is acceptable.

## 4. Architecture

```
  ┌──────────┐  BLE      ┌───────────────┐
  │ Index 01 │──────────▶│ Pebble app    │  local STT (Parakeet 0.6B)
  │  (ring)  │           │ (Pixel 9a)    │
  └──────────┘           └───────┬───────┘
                                 │ HTTPS POST (webhook)
                                 │ tailnet only
                                 ▼
                    ┌────────────────────────┐
                    │ tailscale serve :443   │
                    │   → 127.0.0.1:8787     │
                    └────────────┬───────────┘
                                 ▼
              ┌──────────────────────────────────────┐
              │ index01-relay  (FastAPI, systemd)    │
              │  • auth + dedup                      │
              │  • trigger-phrase filter             │
              │  • approval store (SQLite)           │
              │  • Telegram bot (poll + send)        │
              └───────┬──────────────────┬───────────┘
                      │                  │
        subprocess ▼  │                  │ ▲ HTTPS
              ┌───────────────┐    ┌─────┴──────────┐
              │ NemoClaw      │    │ Telegram Bot   │
              │ + OpenShell   │    │ API            │
              │ sandbox       │    └─────┬──────────┘
              └───────────────┘          │
                                         ▼
                                  Owner's Telegram
                                  (approve / deny)
```

## 5. Data flow and state machine

Each voice note becomes one **job** with the following states:

```
RECEIVED ──▶ FILTERED (no trigger phrase) ──▶ [end]
    │
    ▼
 PLANNING ──▶ PROPOSED ──▶ APPROVED ──▶ EXECUTING ──▶ REPORTED
    │             │  │                       │
    │             │  └──▶ DENIED ──▶ [end]   └──▶ FAILED
    │             └─────▶ EXPIRED ──▶ [end]
    ▼
 REPORTED (read-only turns skip the gate entirely)
```

Jobs persist in SQLite so a relay restart does not orphan a pending approval.

### Classification rule (critical)

Whether a job needs approval is decided by **static tool allowlist**, not by the
model's own judgement. A model asked "is this dangerous?" is being asked to
police itself and can be talked out of the answer by the very input under
suspicion.

- `TOOLS_READONLY` — executes immediately, result reported.
- Anything not in `TOOLS_READONLY` — proposal only, requires approval.
- Unrecognised tool name — treated as requiring approval (fail closed).

## 6. Interfaces

### 6.1 Inbound webhook

`POST /index01`

| Header | Required | Notes |
|--------|----------|-------|
| `x-index01-secret` | yes | compared with `hmac.compare_digest` |
| `content-type` | yes | `application/json` |

**Payload schema: `multipart/form-data`** — see Q1, resolved from Pebble's
published docs. `content-type` is `multipart/form-data; boundary=<uuid>`, not
`application/json`. The transcript is the `transcription` part; `recordedAt`
supplies the dedup identifier. JSON bodies are still accepted so the endpoint can
be driven by `curl` and the test suite.

Set the app's **Send** option to transcription only. Audio parts are counted and
ignored rather than read, but not sending them at all is the S13-aligned choice.

**Response:** `202` immediately with `{"ok": true, "job_id": "..."}`. The relay
must never hold the connection open for the agent turn.

### 6.2 Telegram

| Direction | Endpoint | Use |
|-----------|----------|-----|
| out | `sendMessage` | proposals, results, errors |
| out | `answerCallbackQuery` | acknowledge a button tap |
| out | `editMessageText` | disable buttons after decision |
| in | `getUpdates` (long poll) | receive button callbacks |

Long polling is chosen over a Telegram webhook so no inbound path from the
public internet is required.

**Every** inbound update is validated against `TELEGRAM_OWNER_ID` before
processing. Anyone who discovers the bot username can message it; without this
check a stranger can approve remediation actions.

### 6.3 NemoClaw

Invoked via `asyncio.create_subprocess_exec` with an argument list. Never
`create_subprocess_shell`, never string interpolation into a shell — the
transcript is untrusted input and would become arbitrary code execution.

**Exact CLI syntax: UNVERIFIED against v0.0.103.** Confirm from `nemoclaw --help`
before first run.

## 7. Security requirements

| # | Requirement | Rationale |
|---|-------------|-----------|
| S1 | Bind uvicorn to `127.0.0.1` only | `tailscale serve` proxies to localhost; `0.0.0.0` would also expose the port to the home LAN |
| S2 | `tailscale serve`, never `tailscale funnel` | funnel publishes to the public internet |
| S3 | Constant-time secret comparison | avoids timing oracle on the shared secret |
| S4 | Telegram sender allowlist on every update | bot is reachable by any Telegram user |
| S5 | Agent runs inside OpenShell sandbox (Landlock, deny-all egress) | transcript is untrusted; retrieved content can carry injected instructions |
| S6 | Narrowest viable toolset granted to the agent | limits blast radius of a mis-heard or injected instruction |
| S7 | Payload logging records field *names* only, never values | the webhook fires on every recording, not just agent-directed ones, so logging values writes private notes to syslog unencrypted and unrotated |
| S8 | Secrets via systemd `LoadCredential` or `0600` env file, never in git | bot token is a full credential for the bot |
| S9 | ~~Service runs as dedicated unprivileged user `index01`~~ **Deferred — runs as `democenter`** | see Q6. Not met; recovery path is the split worker |
| S10 | Approvals expire after `APPROVAL_TTL` (default 15 min) | a stale proposal must not be executable hours later |
| S11 | Deduplicate on recording ID | webhook retry must not double-fire the agent |
| S12 | No work/customer data through this path | Telegram bot chats are not E2E encrypted; check Dell IT policy before routing anything work-related |
| S13 | Pebble app set to local-only STT | cloud STT sends raw audio off the phone |

### Known accepted risks

- **Physical trigger, no authentication.** Anyone able to press the ring within
  BLE range of the phone can queue a job. Mitigated by S10 and the approval gate,
  not eliminated.
- **Transcription error.** Wind and background noise produce wrong words. The
  approval message shows the *verbatim transcript* alongside the proposed action
  so a mis-hear is visible before approval.
- **Unsynced audio on a lost ring.** Up to 5 minutes of recordings may be stored
  on-ring pending sync. No mitigation available.
- **The gate is coarse, not per-tool.** Q4 resolved against us: the approval tap
  authorises a whole agent turn, not a named tool call. Approving "restart the
  worker" authorises whatever the agent decides that means, and the sandbox
  policy — not the gate — is what bounds it. The proposal message says so
  explicitly rather than implying a precision the design does not have.
- **Read-only turns are only as read-only as the agent they run on.** With no
  per-invocation tool restriction available, a turn classified read-only still
  runs an agent that could call mutating tools. `NEMOCLAW_AGENT_RO` exists so a
  restricted agent can be pointed at, but until one is created it defaults to
  `main` and acceptance criterion 2's "no ability to change anything" is **not**
  yet met. Creating that agent is the correct close-out for Phase 3.

## 8. Configuration

| Variable | Example | Notes |
|----------|---------|-------|
| `INDEX01_SECRET` | random 32 bytes | shared with Pebble app header |
| `TELEGRAM_BOT_TOKEN` | from @BotFather | full credential |
| `TELEGRAM_OWNER_ID` | numeric user ID | allowlist of exactly one |
| `TRIGGER_PHRASE` | `hey nemo` | notes without it are ignored |
| `NEMOCLAW_TIMEOUT` | `300` | seconds |
| `APPROVAL_TTL` | `900` | seconds |
| `DB_PATH` | `/var/lib/index01/jobs.db` | |
| `LOG_TRANSCRIPTS` | `false` | `true` only during Phase 1 |

## 9. Deployment profile

| Item | Value |
|------|-------|
| Host | `relayhost`, arm64, Ubuntu |
| Runtime | Python 3.11+, systemd unit `index01-relay.service` |
| Listen | `127.0.0.1:8787` (loopback only) |
| Ingress | `tailscale serve --bg 8787` → `https://relayhost.<tailnet>.ts.net` |
| Egress | `api.telegram.org:443` only |
| Storage | SQLite at `/var/lib/index01/jobs.db`, < 10 MB expected |
| Dependencies | `fastapi`, `uvicorn`, `httpx`; NemoClaw v0.0.103 + OpenShell on host |
| Talks to | Pebble app (in), Telegram API (out/in), NemoClaw (local subprocess) |

## 10. Failure modes

| Failure | Behaviour |
|---------|-----------|
| Telegram unreachable | job stays `PROPOSED`, retried on next poll; never auto-approves |
| NemoClaw non-zero exit | `FAILED`, stderr tail sent to owner |
| NemoClaw timeout | process killed, `FAILED` reported |
| Duplicate webhook delivery | second delivery matches existing job ID, returns `202`, no re-run |
| Relay restart mid-job | `EXECUTING` jobs marked `FAILED` on boot; `PROPOSED` jobs survive |
| Unparseable payload | `400`, logged, no job created |

## 11. Acceptance criteria

1. A note without the trigger phrase produces no Telegram traffic and no agent run.
2. A read-only request returns a result to Telegram with no approval prompt.
3. A mutating request produces a proposal showing the verbatim transcript and the
   tool to be called, and **nothing executes** until Approve is tapped.
4. Tapping Deny leaves no trace of execution and disables the buttons.
5. A proposal older than `APPROVAL_TTL` cannot be approved.
6. A callback from any Telegram user other than `TELEGRAM_OWNER_ID` is rejected
   and logged.
7. Replaying the same webhook payload twice runs the agent once.
8. `curl` to `relayhost:8787` from another LAN (non-tailnet) device is refused.
9. Relay restart with a pending proposal preserves it as approvable.

## 12. Open questions

- **Q1: RESOLVED from Pebble's published webhook docs — the body is
  `multipart/form-data`, not JSON.** This invalidated the original assumption in
  section 6.1 outright; the relay parsed JSON only and would have returned `400`
  to every real delivery. Documented fields:

  | Field | Type | Presence |
  |---|---|---|
  | `transcription` | text | when transcription succeeded and text sending is on |
  | `audio` | `audio/mp4` | when audio sending is on — never read by the relay |
  | `recordedAt` | ms since epoch | always |
  | `client` | text, `ring` | always |

  There is no per-recording UUID. `recordedAt` serves as the stable identifier
  because it is the recording's own timestamp and so survives a retry unchanged;
  it is paired with a transcript digest so two notes sharing a millisecond are not
  collapsed.

  **Confirmed from the wire**, first real delivery from the ring:

  ```json
  {"transcription": "How many bones has the human body got?",
   "recordedAt": "1787261486034", "client": "ring"}
  ```

  Note `recordedAt` arrives as a *string*, and no `audio` part is present when the
  app's Send setting is transcription-only. The parser is narrowed to these exact
  keys: a fallback chain would let an unexpected payload silently match some other
  key and run the agent on the wrong text, whereas one key per field fails visibly.
- **Q2: RESOLVED — custom headers are supported.** Pebble's docs state headers
  "used to be limited to just a non-standard auth header but now can be anything."
  The `x-index01-secret` header works as specified and the path-secret fallback is
  not needed, so S3 keeps its original shape.
- **Q3: Still open.** Pebble documents no retry or timeout policy. Treat S11 as
  load-bearing on that basis rather than assuming at-most-once delivery; dedup on
  `recordedAt` is implemented and tested.
- **Q4: RESOLVED — no plan mode; the coarse-gate fallback applies.** Verified
  against NemoClaw v0.0.102 / OpenClaw 2026.7.1 (not v0.0.103; building against
  what is installed). `openclaw agent --help` offers no `--dry-run` and no
  plan-emit flag: a turn runs to completion, tool calls included. There is
  therefore no way to show the intended tool call before it happens, so v1 gates
  the **entire turn** on the verbatim transcript, exactly as this question
  anticipated. Consequences:
  - Section 5's classification rule cannot key off a tool name. It is instead a
    static regex pass over the transcript (`classify()` in `index01_relay.py`):
    any mutating verb forces the gate; only an affirmatively recognised
    read-only question skips it; everything else fails closed.
  - The confirmed invocation is
    `nemoclaw <sandbox> agent --agent <id> --session-key <key> --message-file <path> --timeout <s> --json`.
    `--message-file` is used rather than `-m` so the untrusted transcript never
    reaches argv. `--deliver` is **never** passed — with it the agent would reply
    to Telegram itself and route around the gate.
  - A fresh `--session-key` per job keeps turns stateless (section 3).
- **Q5: RESOLVED — a second bot is required.** `@existing_bot` is already
  enabled, connected and in `mode:polling` for the `the-king` sandbox's OpenClaw
  Telegram channel. Telegram hands each update to exactly one `getUpdates`
  consumer, so a relay sharing that token would steal updates from OpenClaw and
  lose approval taps at random — silent, intermittent, and worst-case
  fail-*open*-looking (a tap that appears to do nothing). The relay detects the
  409 and logs it loudly rather than degrading quietly, but the fix is a
  dedicated bot. Outbound `sendMessage` would have been safe to share; inbound
  polling is not.

### Q6 (new): how the relay reaches NemoClaw — RESOLVED

NemoClaw's binary and state live under `/home/democenter`, which a dedicated
`index01` user cannot read (S9) and the unit cannot see (`ProtectHome=true`).
Three routes were considered; **the relay now runs as `democenter`**, and S9 is
deferred rather than met.

- **HTTP gateway (investigated, rejected).** The gateway does expose
  OpenAI-compatible `POST /v1/chat/completions` and `/v1/responses` that run the
  same codepath as `openclaw agent`, and they would have avoided delivery to
  Telegram for free — there is no `--deliver` equivalent to forget. But they are
  disabled by default and confirmed off for `the-king`; the bearer token is
  full-operator scope rather than a narrow run-one-turn capability, which cuts
  against S6; the token lives in `democenter`-owned files that `index01` still
  cannot read; and port 18789 on the host is an ephemeral `ssh -L` forward owned
  by a `democenter` session, not a stable listener. It relocates the permission
  wall instead of removing it, and adds a silent-breakage mode when the tunnel
  cycles.
- **`sudo` wrapper (rejected).** Incompatible with `NoNewPrivileges=true`, since
  sudo is setuid. Trading that away on the process that parses untrusted webhook
  input is the wrong side of the trade.
- **Run as `democenter` (chosen).** Keeps `NoNewPrivileges` and the rest of the
  hardening profile. Section 1 puts the approval gate, not uid separation, as the
  primary control, and the relay's whole purpose is to trigger work that runs with
  this user's privileges anyway.

Two systemd directives had to be dropped or avoided because the NemoClaw CLI is
Node: `MemoryDenyWriteExecute=true` blocks V8's JIT and kills the agent
subprocess, and `PrivateTmp=true` is the first suspect if the relay can reach
Telegram but not the sandbox. Both are documented in `index01-relay.service`.

**To recover S9 later:** split the relay in two — an `index01`-owned,
network-facing half that parses the webhook and owns Telegram plus SQLite, and a
`democenter`-owned worker that watches a spool directory and runs `nemoclaw`. No
sudo, no setuid, and `NoNewPrivileges` on both halves.

## 13. Phasing

| Phase | Deliverable | Exit criteria |
|-------|-------------|---------------|
| 0 | Tailnet + bot + service user | `/health` reachable over ts.net |
| 1 | Payload discovery | real schema documented, Q1 closed |
| 2 | Transcript → Telegram echo | ring press appears in Telegram |
| 3 | Read-only agent turns | AC 1, 2 pass |
| 4 | Approval gate | AC 3–6, 9 pass |
| 5 | Hardening | AC 7, 8 pass; S7 applied; logging reduced |
