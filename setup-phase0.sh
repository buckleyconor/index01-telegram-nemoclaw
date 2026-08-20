#!/usr/bin/env bash
# Phase 0 — Foundations. Run from the repo directory:
#
#   sudo ./setup-phase0.sh
#
# Idempotent: safe to re-run. Does NOT write /etc/index01.env (that holds
# credentials, S8) and does NOT start the service -- it will not start until the
# env file exists. See the printed next steps.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

# The relay runs as democenter rather than a dedicated user: it has to exec the
# NemoClaw CLI, which lives in that home directory. S9 is deferred deliberately --
# the reasoning is in index01-relay.service and SPEC section 12 (Q6).
SVC_USER=democenter

echo "==> preflight"
if ! id "$SVC_USER" &>/dev/null; then
    echo "    user $SVC_USER not found" >&2
    exit 1
fi
if [[ ! -x /home/democenter/.local/bin/nemoclaw ]]; then
    echo "    warning: nemoclaw not found at the expected path;"
    echo "    Step 4 will fail until NEMOCLAW_CMD points somewhere real"
fi

echo "==> directories"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 /var/lib/index01
install -d -o root -g root -m 0755 /opt/index01

echo "==> python environment"
if ! dpkg -s python3-venv &>/dev/null; then
    apt-get install -y python3-venv
fi
if [[ ! -x /opt/index01/venv/bin/python ]]; then
    python3 -m venv /opt/index01/venv
fi
/opt/index01/venv/bin/pip install --quiet --upgrade pip
# python-multipart is required: the Pebble webhook posts multipart/form-data,
# and Starlette cannot parse a form without it.
/opt/index01/venv/bin/pip install --quiet fastapi uvicorn httpx python-multipart
echo "    $(/opt/index01/venv/bin/python -V)"

echo "==> application files"
install -m 0644 -o root -g root "$REPO/index01_relay.py" /opt/index01/index01_relay.py
install -m 0644 -o root -g root "$REPO/index01-relay.service" \
    /etc/systemd/system/index01-relay.service
# nemoclaw-run.sh is intentionally not installed. It is the sudo wrapper for the
# dedicated-user design, kept in the repo as the seed for the future split worker.

echo "==> environment file (S8: 0600, root-owned, never in git)"
if [[ ! -f /etc/index01.env ]]; then
    install -m 0600 -o root -g root "$REPO/index01.env.example" /etc/index01.env
    echo "    created /etc/index01.env from the example -- FILL IT IN, it has empty secrets"
    NEEDS_SECRETS=1
else
    chmod 0600 /etc/index01.env
    chown root:root /etc/index01.env
    echo "    /etc/index01.env already exists, left untouched"
    NEEDS_SECRETS=0
fi

echo "==> systemd"
systemctl daemon-reload
systemctl enable index01-relay >/dev/null
echo "    unit enabled (not started yet)"

cat <<'NEXT'

--------------------------------------------------------------------
Phase 0 is staged. Remaining steps need decisions or credentials:

1. Generate the shared secret and put it in /etc/index01.env:
     python3 -c "import secrets; print(secrets.token_urlsafe(32))"

2. Put a Telegram bot token and your numeric user id in /etc/index01.env.
   IMPORTANT: this must NOT be @existing_bot. That bot is already being
   long-polled by the-king's OpenClaw channel, and Telegram gives each
   update to only one poller -- approval taps would vanish at random.
   Create a second bot with @BotFather (/newbot, then /setprivacy -> Enable).

   Get your numeric id after messaging the new bot:
     curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   Read message.from.id

3. Start it and expose it on the tailnet:
     sudo systemctl start index01-relay
     sudo systemctl status index01-relay
     sudo tailscale serve --bg 8787
     tailscale serve status

   If `serve` errors about certificates: this tailnet reports no CertDomains,
   so HTTPS certs are probably not enabled yet. Turn them on in the admin
   console under DNS -> HTTPS Certificates, then retry.

4. Exit test:
     from the Pixel, on the tailnet:
       https://relayhost.example-tailnet.ts.net/health   -> {"ok": true}
     from a non-tailnet LAN device (acceptance criterion 8):
       curl http://192.0.2.10:8787/health        -> must be refused

Note: every turn is gated while REQUIRE_APPROVAL_ALL=true, so even "what is
the uptime" will ask for a tap. That is deliberate until a restricted
read-only agent exists -- see SPEC section 7.
--------------------------------------------------------------------
NEXT
