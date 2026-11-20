#!/usr/bin/env bash
# Idempotent provisioning for the Lightsail box.
#
# Run as ``ubuntu`` (the admin account) with sudo available. Re-running on an
# already-provisioned box is safe; every step is guarded against repeat
# application.
#
#     sudo ./deploy/bootstrap.sh [--deploy-pubkey /path/to/iotagle_deploy.pub]
#
# Accounts:
#   ubuntu  admin. Full sudo, your personal key only. Never used by CI.
#   deploy  CI deploys. Owns the code + venv. Its single SSH key is pinned to
#           /usr/local/sbin/iotagle-deploy via a forced command, and its sudo
#           is limited to restarting/probing iotagle.service.
#   iotagle runs gunicorn. Read-only access to the code; owns /var/lib/iotagle.
#
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E "$0" "$@"
fi

DEPLOY_PUBKEY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --deploy-pubkey) DEPLOY_PUBKEY="${2:?--deploy-pubkey needs a path}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

PYTHON=python3.12
APP_DIR=/opt/iotagle/app
VENV_DIR=/opt/iotagle/venv
SERVICE_USER=iotagle
DEPLOY_USER=deploy
REPO_URL=https://github.com/jroark/iotagle.com.git

echo ">> apt update + install"
DEBIAN_FRONTEND=noninteractive apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    "${PYTHON}" "${PYTHON}-venv" \
    nginx ufw fail2ban python3-systemd git curl ca-certificates \
    unattended-upgrades needrestart

echo ">> service user (${SERVICE_USER})"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home /opt/iotagle --shell /usr/sbin/nologin \
        --user-group "${SERVICE_USER}"
fi

echo ">> deploy user (${DEPLOY_USER})"
# A real shell is required: sshd runs the forced command via the user's
# shell. The key's ``restrict`` + ``command=`` options are what stop it
# from being an interactive login.
if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/${DEPLOY_USER}" \
        --shell /bin/bash --user-group "${DEPLOY_USER}"
fi

echo ">> app directories"
# /opt/iotagle: root-owned; o+x so nginx (www-data) can traverse to
# app/static/ for /robots.txt and /favicon.ico without listing anything.
install -d -o root -g "${SERVICE_USER}" -m 0751 /opt/iotagle
# Code and venv are owned by deploy (who updates them), group iotagle (the
# service reads them). setgid propagates the group to new files.
if [[ ! -d "${APP_DIR}/.git" ]]; then
    install -d -o "${DEPLOY_USER}" -g "${SERVICE_USER}" -m 2751 "${APP_DIR}"
    sudo -u "${DEPLOY_USER}" git clone --quiet "${REPO_URL}" "${APP_DIR}"
fi
chown -R "${DEPLOY_USER}:${SERVICE_USER}" "${APP_DIR}"
chmod 2751 "${APP_DIR}"
# Lets the admin run read-only git commands in the deploy-owned checkout.
if ! git config --system --get-all safe.directory 2>/dev/null | grep -qx "${APP_DIR}"; then
    git config --system --add safe.directory "${APP_DIR}"
fi

echo ">> venv"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    install -d -o "${DEPLOY_USER}" -g "${SERVICE_USER}" -m 2751 "${VENV_DIR}"
    sudo -u "${DEPLOY_USER}" "${PYTHON}" -m venv "${VENV_DIR}"
fi
chown -R "${DEPLOY_USER}:${SERVICE_USER}" "${VENV_DIR}"
sudo -u "${DEPLOY_USER}" "${VENV_DIR}/bin/pip" install --quiet --disable-pip-version-check \
    --require-hashes --only-binary=:all: -r "${APP_DIR}/requirements.txt"

echo ">> deploy entry point"
install -o root -g root -m 0755 "${APP_DIR}/deploy/iotagle-deploy" /usr/local/sbin/iotagle-deploy

echo ">> deploy SSH key"
DEPLOY_SSH_DIR="/home/${DEPLOY_USER}/.ssh"
# Root-owned so the deploy user can't edit its own authorized_keys.
install -d -o root -g root -m 0755 "${DEPLOY_SSH_DIR}"
if [[ -n "${DEPLOY_PUBKEY}" ]]; then
    key="$(tr -d '\r' < "${DEPLOY_PUBKEY}")"
    if [[ "$(printf '%s\n' "${key}" | wc -l)" -ne 1 || ! "${key}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+( .*)?$ ]]; then
        echo "expected exactly one ssh-ed25519 public key in ${DEPLOY_PUBKEY}" >&2
        exit 2
    fi
    printf 'restrict,command="/usr/local/sbin/iotagle-deploy" %s\n' "${key}" \
        > "${DEPLOY_SSH_DIR}/authorized_keys.tmp"
    chmod 0644 "${DEPLOY_SSH_DIR}/authorized_keys.tmp"
    mv "${DEPLOY_SSH_DIR}/authorized_keys.tmp" "${DEPLOY_SSH_DIR}/authorized_keys"
elif [[ ! -f "${DEPLOY_SSH_DIR}/authorized_keys" ]]; then
    echo "   (no deploy key installed yet; rerun with --deploy-pubkey)"
fi

echo ">> sudoers drop-in for deploys"
# Replaces the old drop-in that granted these to ``ubuntu``. Arguments are
# listed exactly, so no other systemctl invocation matches.
SUDOERS_FILE=/etc/sudoers.d/iotagle-deploy
cat > "${SUDOERS_FILE}.tmp" <<EOF
# Managed by iotagle deploy/bootstrap.sh. Validated with visudo -c.
${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart iotagle.service, /usr/bin/systemctl is-active --quiet iotagle.service
EOF
chmod 0440 "${SUDOERS_FILE}.tmp"
visudo -c -q -f "${SUDOERS_FILE}.tmp"
mv "${SUDOERS_FILE}.tmp" "${SUDOERS_FILE}"

echo ">> sshd hardening"
SSHD_CONF=/etc/ssh/sshd_config.d/iotagle.conf
cat > "${SSHD_CONF}.tmp" <<EOF
# Managed by iotagle deploy/bootstrap.sh.
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers ubuntu ${DEPLOY_USER}
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 20
EOF
chmod 0644 "${SSHD_CONF}.tmp"
mv "${SSHD_CONF}.tmp" "${SSHD_CONF}"
# Never reload sshd with a broken config; that's how you lose the box.
sshd -t
systemctl reload ssh || systemctl reload sshd

echo ">> ufw"
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw --force enable

echo ">> fail2ban"
install -m 0644 /dev/stdin /etc/fail2ban/jail.d/iotagle.conf <<'EOF'
# Managed by iotagle deploy/bootstrap.sh.
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable fail2ban
systemctl restart fail2ban

echo ">> unattended security upgrades"
install -m 0644 /dev/stdin /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
install -m 0644 /dev/stdin /etc/apt/apt.conf.d/52iotagle-unattended-upgrades <<'EOF'
// Managed by iotagle deploy/bootstrap.sh. Ubuntu's 50unattended-upgrades
// already limits origins to -security (and ESM); this only adds reboots.
// 09:00 UTC is 01:00-02:00 US Pacific, the quietest time for this site.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "09:00";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
# Restart services with outdated libraries automatically instead of
# prompting (there's no one at the prompt during an unattended upgrade).
install -d -m 0755 /etc/needrestart/conf.d
install -m 0644 /dev/stdin /etc/needrestart/conf.d/iotagle.conf <<'EOF'
$nrconf{restart} = 'a';
EOF
systemctl enable --now unattended-upgrades

echo ">> nginx site"
NGINX_SITE=/etc/nginx/sites-available/iotagle
install -m 0644 "${APP_DIR}/deploy/nginx/iotagle.conf" "${NGINX_SITE}"
ln -sf "${NGINX_SITE}" /etc/nginx/sites-enabled/iotagle
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo ">> systemd unit"
install -m 0644 "${APP_DIR}/deploy/systemd/iotagle.service" /etc/systemd/system/iotagle.service
# Left over from the unix-socket era; harmless if already gone.
if [[ -f /etc/systemd/system/iotagle.socket ]]; then
    systemctl disable --now iotagle.socket || true
    rm -f /etc/systemd/system/iotagle.socket
fi
systemctl daemon-reload
systemctl enable iotagle.service
systemctl restart iotagle.service

echo ">> health check"
for _ in $(seq 1 15); do
    if curl -fsS --max-time 3 http://127.0.0.1/healthz | grep -q '^ok$'; then
        echo ">> done: iotagle healthy"
        exit 0
    fi
    sleep 1
done
echo ">> iotagle did not become healthy; check: journalctl -u iotagle -n 100" >&2
exit 1
