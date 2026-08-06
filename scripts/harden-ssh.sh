#!/usr/bin/env bash
#
# Make port 22 safe to expose to the whole internet.
#
# Deliberately NOT part of `vps-bootstrap.sh`. That script says, in a
# comment, that it will not touch sshd -- locking yourself out of a box
# you just bought is worse than a permissive default. That reasoning
# still holds *at bootstrap*, when nobody has yet proved their key
# works. This script is the other half: run it once you have logged in
# with a key and know it works.
#
# Why expose 22 at all: the admin travels, so the source address changes
# constantly, and pinning the security group to "My IP" meant editing an
# AWS rule from a hotel before being able to read a log. The trade is a
# port that scanners can see -- 47 failed attempts and 8 bans in the
# first day -- against an account with no password to guess.
#
# Idempotent. Safe to re-run.
set -euo pipefail

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------
# The one check that matters. Turning off password authentication with
# no usable key in place is how a box becomes unreachable forever.
# ------------------------------------------------------------------
KEYS="${SUDO_USER:+/home/$SUDO_USER}/.ssh/authorized_keys"
[ -n "${SUDO_USER:-}" ] || KEYS="$HOME/.ssh/authorized_keys"
if [ ! -s "$KEYS" ]; then
  die "no keys in $KEYS -- refusing to disable password login, that would lock you out."
fi
say "Found $(grep -c . "$KEYS") authorized key(s) in $KEYS"

say "Writing sshd hardening"
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<EOF
# Port 22 is exposed to the internet so a travelling admin can reach it
# from any address. Everything below assumes the port is hostile.
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no
AllowUsers ${SUDO_USER:-$USER}
EOF

# Validate before touching the running daemon: a syntax error plus a
# restart is the same lockout by a different route.
say "Validating"
sudo sshd -t || die "sshd config invalid -- nothing was applied to the running daemon."
sudo systemctl reload ssh

say "Installing fail2ban"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban >/dev/null
sudo tee /etc/fail2ban/jail.d/sshd.local >/dev/null <<'EOF'
# Password auth is already off, so this is less about stopping a
# break-in than about keeping the auth log readable and shedding the
# constant background scanning.
[sshd]
enabled  = true
backend  = systemd
maxretry = 4
findtime = 10m
bantime  = 1h
EOF
sudo systemctl enable --now fail2ban >/dev/null

say "Effective settings"
sudo sshd -T | grep -iE '^(passwordauthentication|permitrootlogin|pubkeyauthentication|maxauthtries|permitemptypasswords|allowusers)' | sed 's/^/    /'

cat <<'EOF'

==> Done

Open port 22 in the cloud firewall now, not before.

Verify afterwards -- these are the checks that actually prove it:

  sudo sshd -T | grep -i passwordauth        # must say: no
  sudo fail2ban-client status sshd           # bans accumulate
  sudo journalctl -u ssh | grep -c "Accepted password"   # must be 0

  # every key that has ever succeeded -- expect exactly yours:
  sudo journalctl -u ssh | grep -oE 'SHA256:[A-Za-z0-9+/]+' | sort -u
EOF
