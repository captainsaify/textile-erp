#!/usr/bin/env bash
#
# Prepare a fresh Ubuntu VPS to run this stack. Idempotent -- safe to
# re-run. See docs/30_VpsMigration.md.
#
#   curl -fsSL https://raw.githubusercontent.com/captainsaify/textile-erp/main/scripts/vps-bootstrap.sh | sudo bash
#   # or, after cloning:
#   sudo ./scripts/vps-bootstrap.sh
#
# Deliberately does NOT touch sshd config. Locking yourself out of a
# box you just bought is a worse outcome than a slightly permissive
# default. Harden it afterwards, from a session you can already use,
# with scripts/harden-ssh.sh.
#
# Architecture-neutral: the Docker repo line is built from
# `dpkg --print-architecture`, so this works unchanged on arm64
# (Graviton) as well as x86_64. See docs/30 §9.

set -euo pipefail

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo."
. /etc/os-release 2>/dev/null || die "cannot identify the OS."
[ "${ID:-}" = "ubuntu" ] || [ "${ID:-}" = "debian" ] \
  || warn "tested on Ubuntu/Debian; '${ID:-unknown}' may differ."

TARGET_USER="${SUDO_USER:-${1:-erp}}"

# --- 1. packages ----------------------------------------------------
say "Updating package lists"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw jq >/dev/null

# --- 2. docker ------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  say "Docker already present: $(docker --version)"
else
  say "Installing Docker CE and the compose plugin"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

if id "$TARGET_USER" >/dev/null 2>&1 && [ "$TARGET_USER" != "root" ]; then
  usermod -aG docker "$TARGET_USER"
  say "Added ${TARGET_USER} to the docker group (log out and back in to use it)"
fi

# --- 3. swap --------------------------------------------------------
# The stack idles around 1.1GB, but `docker compose build` compiles
# wheels and will reach for far more. On a 4GB box a build without swap
# is where the OOM killer takes out postgres mid-migration.
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
  TOTAL_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
  # Sized against RAM, not a fixed number: on a 2GB box the build is
  # the tight moment and swap is what stops it being fatal, so that box
  # gets the most.
  if   [ "$TOTAL_MB" -lt 2500 ]; then SWAP_GB=4
  elif [ "$TOTAL_MB" -lt 5000 ]; then SWAP_GB=2
  else                                SWAP_GB=1
  fi
  say "Creating ${SWAP_GB}G of swap (RAM is ${TOTAL_MB}MB)"
  fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB*1024))
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -qw vm.swappiness=10
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
else
  say "Swap already configured"
fi

# --- 4. firewall ----------------------------------------------------
# SSH, HTTP and HTTPS. This used to be SSH only, correctly: the
# Cloudflare Tunnel dialled *out*, so nothing inbound was needed. The
# stack now serves 443 itself (docs/30 §8), and 80 is not optional --
# Let's Encrypt's HTTP-01 challenge is what renews the certificate, so
# closing 80 buys nothing and expires the site in 90 days.
#
# Be clear about what this does and does not do. Docker publishes ports
# by writing its own DNAT rules, which are traversed *before* ufw's
# rules for routed traffic -- so a container port published to 0.0.0.0
# is reachable whether or not ufw allows it. These rules govern
# processes listening on the host; what actually keeps postgres and
# redis private is that compose never publishes them. Verify with
# `docker compose ps` that only nginx maps to 0.0.0.0, and treat any
# other 0.0.0.0 mapping as public regardless of what ufw reports.
say "Configuring the firewall: SSH, HTTP, HTTPS"
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/    /'

# --- 5. unattended security updates ---------------------------------
say "Enabling unattended security upgrades"
apt-get install -y -qq unattended-upgrades >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

# --- 6. log rotation ------------------------------------------------
# Without this the json-file driver grows without bound and fills the
# disk, which presents as postgres refusing writes for no visible
# reason (docs/16 §8 asks for exactly this).
say "Capping Docker log growth"
mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ] && ! grep -q max-size /etc/docker/daemon.json; then
  warn "/etc/docker/daemon.json exists; not overwriting. Add log-opts by hand."
elif [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'CONF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
CONF
  systemctl restart docker
fi

# --- 7. timezone ----------------------------------------------------
# Beat fires on UTC and each org resolves its own calendar, so this is
# only about readable logs -- but a log you have to add 5.5 hours to is
# a log nobody reads during an incident.
timedatectl set-timezone Asia/Kolkata 2>/dev/null || true
say "Timezone: $(timedatectl show -p Timezone --value 2>/dev/null || date +%Z)"

cat <<DONE

$(say "Ready")
  docker  : $(docker --version | cut -d, -f1)
  compose : $(docker compose version --short 2>/dev/null)
  ram     : $(awk '/MemTotal/{printf "%.1fGB", $2/1024/1024}' /proc/meminfo)
  swap    : $(awk '/SwapTotal/{printf "%.1fGB", $2/1024/1024}' /proc/meminfo)
  disk    : $(df -h / | awk 'NR==2{print $4" free"}')

$(if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 2500 ]; then cat <<'TIP'
On a 2GB host, set these in .env before starting (docs/30 §7) --
measured 1086MB at the defaults, 640MB with these:
  CELERY_WHATSAPP_CONCURRENCY=2
  CELERY_OCR_CONCURRENCY=1
  CELERY_SCHEDULED_CONCURRENCY=2
TIP
fi)
Next:
  git clone https://github.com/captainsaify/textile-erp.git
  cd textile-erp
  ./scripts/migrate-import.sh /tmp/textile-erp-<stamp>.tar.gz

DONE
