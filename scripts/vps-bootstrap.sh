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
# default, and the tunnel means nothing but SSH is exposed anyway.

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
  SWAP_GB=2; [ "$TOTAL_MB" -ge 7000 ] && SWAP_GB=1
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
# SSH only. The Cloudflare Tunnel dials *out*, so 80/443 never need to
# be open -- which is the security win of the tunnel and the reason not
# to reflexively open them "just in case".
say "Configuring the firewall: SSH only"
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
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

Next:
  git clone https://github.com/captainsaify/textile-erp.git
  cd textile-erp
  ./scripts/migrate-import.sh /tmp/textile-erp-<stamp>.tar.gz

DONE
