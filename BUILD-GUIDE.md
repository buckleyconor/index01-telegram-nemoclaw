# Build Guide: Index 01 → NemoClaw Voice Relay

Follow the phases in order. Each phase has an exit test — don't move on until it
passes, because debugging four layers at once is miserable and the ring makes a
poor debugger.

Everything runs on `relayhost` unless stated otherwise.

---

## Phase 0 — Foundations

### 0.1 Confirm the tailnet

On the GB10:

```bash
tailscale status
tailscale ip -4
```

On the Pixel 9a: open the Tailscale app, confirm it's connected to the same
tailnet, and that you can reach the GB10:

```
http://relayhost.<your-tailnet>.ts.net
```

Find your exact MagicDNS name with `tailscale status --json | grep DNSName`.

### 0.2 Create the service user and directories

```bash
sudo useradd --system --home /var/lib/index01 --shell /usr/sbin/nologin index01
sudo mkdir -p /var/lib/index01 /opt/index01
sudo chown index01:index01 /var/lib/index01
```

### 0.3 Python environment

```bash
sudo apt install -y python3-venv
sudo python3 -m venv /opt/index01/venv
sudo /opt/index01/venv/bin/pip install fastapi uvicorn httpx
```

### 0.4 Create the Telegram bot

1. Message **@BotFather** in Telegram → `/newbot` → follow prompts.
2. Save the token. It looks like `123456789:AAH...` and is a full credential.
3. Send your new bot any message (it won't reply yet).
4. Get your numeric user ID:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
```

Read `message.from.id` — that's your `TELEGRAM_OWNER_ID`. Note it's the *user*
ID you want, not just the chat ID (they're the same for a DM, but the callback
validation checks the user).

5. In BotFather, `/setprivacy` → Enable. Reduces what the bot can see in groups.

### 0.5 Generate the shared secret

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 0.6 Write the environment file

```bash
sudo tee /etc/index01.env > /dev/null <<'EOF'
INDEX01_SECRET=<paste from 0.5>
TELEGRAM_BOT_TOKEN=<paste from 0.4>
TELEGRAM_OWNER_ID=<paste from 0.4>
TRIGGER_PHRASE=hey nemo
NEMOCLAW_TIMEOUT=300
APPROVAL_TTL=900
DB_PATH=/var/lib/index01/jobs.db
LOG_TRANSCRIPTS=true
EOF
sudo chmod 600 /etc/index01.env
sudo chown root:root /etc/index01.env
```

`LOG_TRANSCRIPTS=true` is temporary — Phase 5 turns it off.

### 0.7 Install the relay and systemd unit

Copy `index01_relay.py` to `/opt/index01/`, then:

```bash
sudo tee /etc/systemd/system/index01-relay.service > /dev/null <<'EOF'
[Unit]
Description=Index 01 voice relay
After=network-online.target

[Service]
User=index01
Group=index01
EnvironmentFile=/etc/index01.env
WorkingDirectory=/opt/index01
ExecStart=/opt/index01/venv/bin/uvicorn index01_relay:app --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=5

# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/index01

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now index01-relay
sudo systemctl status index01-relay
```

### 0.8 Expose it over Tailscale

```bash
sudo tailscale serve --bg 8787
tailscale serve status
```

This gives you HTTPS with a real certificate on your MagicDNS name.
**Use `serve`, not `funnel`** — funnel would publish this to the open internet.

### Exit test

From the Pixel (browser, on the tailnet):

```
https://relayhost.<tailnet>.ts.net/health
```

Should return `{"ok": true}`. From a non-tailnet device on your home LAN,
`curl http://<gb10-lan-ip>:8787/health` should fail — that confirms the loopback
bind is working.

---

## Phase 1 — Discover the real payload

The Pebble webhook body format isn't documented. You're going to read it.

### 1.1 Configure the webhook in the Pebble app

Pebble app → Settings → integrations → custom webhook (exact menu path may
differ by app version). Set:

- URL: `https://relayhost.<tailnet>.ts.net/index01`
- Header: `x-index01-secret: <your secret>`

**If the app doesn't support custom headers** — this is open question Q2 in the
spec. Fall back to putting the secret in the path (`/index01/<secret>`) and
update the route accordingly. Note that a path secret will appear in logs, so
keep the log level low.

### 1.2 Send one test note

Use the **text input** on the Index home screen in the Pebble app rather than the
ring. It produces the same result and is far faster to iterate with.

Send: `hey nemo what is the uptime on the gb10`

### 1.3 Read the payload

```bash
sudo journalctl -u index01-relay -f
```

Find the `RAW PAYLOAD` line. Write the real schema into the spec (section 6.1),
then **replace the `extract_transcript()` fallback chain** with the one real key.
Note whether there's a stable per-recording ID — you need it for dedup.

### Exit test

You can state, from observed data, what field holds the transcript and what field
(if any) holds a unique recording ID.

---

## Phase 2 — Echo to Telegram

Before involving the agent, prove the round trip.

Confirm that pressing the ring results in a Telegram message on your phone
containing the verbatim transcript. At this point the relay does nothing else.

Check both directions work:

```bash
# outbound
curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<OWNER_ID> -d text="relay test"
```

### Exit test

Ring press → Telegram notification within a few seconds. Also confirm a note
*without* the trigger phrase produces no Telegram traffic at all (acceptance
criterion 1).

---

## Phase 3 — Read-only agent turns

### 3.1 Verify the NemoClaw invocation

Do this before wiring it in:

```bash
nemoclaw --help
```

Find the real one-shot syntax on v0.0.103 and confirm it by hand:

```bash
nemoclaw run --prompt "what is the current uptime"   # <-- verify, don't assume
```

Then update `ask_nemoclaw()` with the confirmed argument list. Keep it as a
list of args to `create_subprocess_exec` — never build a shell string from the
transcript.

### 3.2 Establish the read-only toolset

Grant the agent the narrowest toolset that makes your use case work, and record
which tool names are read-only in `TOOLS_READONLY`. Start with something like
status/query tools only. Anything not on that list will be gated in Phase 4, and
an unrecognised tool name is gated by default.

### 3.3 Run it inside the sandbox

Use your existing OpenShell sandbox configuration — Landlock LSM, deny-all
egress. The transcript is untrusted input, and if the agent has retrieval or file
access, content it pulls in can carry instructions that drive its tools. The
sandbox is what contains that.

### Exit test

`hey nemo what's the GPU utilisation` returns a real answer to Telegram, with no
approval prompt, and the agent had no ability to change anything.

---

## Phase 4 — The approval gate

### 4.1 Proposal message format

When a job requires approval, the Telegram message must show:

1. The **verbatim transcript** — so a mis-heard word is visible before you approve
2. The **tool name and arguments** that would run
3. Two inline buttons: Approve / Deny

Showing the transcript is the whole point. "Approve: restart nemo-worker-3" tells
you nothing about whether you actually said that.

### 4.2 Callback validation

Every inbound Telegram update is checked:

```python
if update["callback_query"]["from"]["id"] != OWNER_ID:
    log.warning("rejected callback from %s", ...)
    return
```

Without this, anyone who finds your bot's username can tap Approve on your
remediation actions. This is not theoretical — bot usernames are enumerable.

### 4.3 Expiry

A proposal older than `APPROVAL_TTL` returns "expired" when tapped and does not
execute. Check the age at *tap* time, not just with a sweeper, or a restart
window lets a stale proposal through.

### 4.4 After the decision

`editMessageText` to remove the buttons and record the outcome in the message
itself. Prevents double-taps and gives you an audit trail in the chat.

### Exit test

Run through acceptance criteria 3–6 and 9 from the spec:

- Mutating request shows a proposal, nothing runs before the tap
- Deny leaves no execution trace
- Expired proposal refuses
- Restart the relay with a proposal pending — it survives and is still approvable
- Second Telegram account (or a friend) tapping Approve is rejected

---

## Phase 5 — Hardening

### 5.1 Turn off transcript logging

```bash
sudo sed -i 's/LOG_TRANSCRIPTS=true/LOG_TRANSCRIPTS=false/' /etc/index01.env
sudo systemctl restart index01-relay
```

Then remove the `RAW PAYLOAD` debug line entirely from the code. Left in place,
it writes every utterance to syslog unencrypted and unrotated.

### 5.2 Confirm deduplication

Replay a captured payload twice:

```bash
curl -X POST https://relayhost.<tailnet>.ts.net/index01 \
  -H "x-index01-secret: $SECRET" \
  -H "content-type: application/json" \
  -d @captured-payload.json
```

Run it twice. The agent should run once.

### 5.3 Move secrets to systemd credentials (optional)

If you want the token out of an env file entirely, switch to `LoadCredential=`
and read from `$CREDENTIALS_DIRECTORY`. The env file at `0600` owned by root is
acceptable for a personal deployment.

### 5.4 Set the Pebble app to local STT

In the Pebble app, set speech recognition to local-only. The cloud option sends
raw audio off the phone.

### 5.5 Decide the work-data boundary

Before you use this for anything work-adjacent: voice notes would flow ring →
Intune-managed phone → Telegram's servers → GB10. Telegram bot chats are not
end-to-end encrypted. Check what Dell IT policy permits before routing customer
or engagement details through it, and consider making that a hard rule you
enforce in your own head rather than in code — the ring won't ask.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Webhook never arrives | Pixel not on tailnet; check the Tailscale app is actually connected, not just installed |
| `401` in logs | secret mismatch, or app is stripping the custom header (see Q2) |
| Works on wifi, not on mobile data | Tailscale not running in background — check Android battery optimisation for the Tailscale app |
| Telegram silent | wrong `chat_id`, or you never sent the bot a first message |
| Buttons do nothing | long-poll loop not running, or callback rejected by the owner check — grep the logs |
| Agent runs twice | dedup not keyed on a stable ID; check Q3 and what Pebble retries look like |
| Certificate warning | using the raw tailnet IP rather than the MagicDNS name |

---

## What to verify rather than trust

Three things in this guide are written against unverified assumptions, flagged
here so you check them rather than inherit them:

1. **Pebble payload schema** — Phase 1 resolves this from real data.
2. **NemoClaw v0.0.103 CLI syntax** — confirm from `--help` before use.
3. **Whether NemoClaw can emit a plan before executing** — the whole approval
   gate depends on being able to see the intended tool call *before* it runs. If
   it can't, you fall back to gating the entire turn on the raw transcript, which
   works but is a blunter instrument. Establish this early in Phase 3; it's the
   assumption most likely to reshape the design.
