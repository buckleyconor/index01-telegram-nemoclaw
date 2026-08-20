#!/usr/bin/env bash
# Privilege boundary between the relay and NemoClaw.
#
# The relay runs as `index01` (S9) and so cannot reach NemoClaw's state, which
# lives under democenter's home. Rather than widen the relay's access to that
# directory -- it holds credentials for every sandbox -- the relay crosses to
# democenter through exactly this wrapper, allowlisted in sudoers.
#
# Install:
#   sudo install -m 0755 nemoclaw-run.sh /opt/index01/nemoclaw-run.sh
#   echo 'index01 ALL=(democenter) NOPASSWD: /opt/index01/nemoclaw-run.sh' \
#     | sudo tee /etc/sudoers.d/index01-nemoclaw
#   sudo chmod 0440 /etc/sudoers.d/index01-nemoclaw
#   sudo visudo -c
#
# The relay always builds an explicit argv (never a shell string), so "$@" is
# forwarded verbatim and the transcript is never interpreted by a shell.
set -euo pipefail
exec /home/democenter/.local/bin/nemoclaw "$@"
