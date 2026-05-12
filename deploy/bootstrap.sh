#!/usr/bin/env bash
# Idempotent first-time provisioning for the Lightsail box.
#
# Run as ``ubuntu`` with sudo available. Re-running on an already-provisioned
# box should be safe; every step is guarded against repeat application.
#
#     sudo ./deploy/bootstrap.sh
#
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E "$0" "$@"
fi

APP_DIR=/opt/iotagle/app
VENV_DIR=/opt/iotagle/venv
SERVICE_USER=iotagle

echo ">> apt update + install"
DEBIAN_FRONTEND=noninteractive apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    python3.11 python3.11-venv python3.11-dev \
    build-essential libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev \
    nginx ufw fail2ban git curl ca-certificates

echo ">> service user (${SERVICE_USER})"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home /opt/iotagle --shell /usr/sbin/nologin \
        --user-group "${SERVICE_USER}"
fi

echo ">> app directories"
mkdir -p /opt/iotagle
# Repo owned by ubuntu (who deploys via SSH), group iotagle (service reads).
# setgid bit on the directory propagates the group to new files.
chown -R ubuntu:"${SERVICE_USER}" /opt/iotagle
chmod 2750 /opt/iotagle
mkdir -p "${APP_DIR}"
chown ubuntu:"${SERVICE_USER}" "${APP_DIR}"
chmod 2750 "${APP_DIR}"

echo ">> sudoers drop-in for deploys"
SUDOERS_FILE=/etc/sudoers.d/iotagle-deploy
cat > "${SUDOERS_FILE}.tmp" <<'EOF'
# Allow the deploying user to bounce the iotagle service without a password.
# Nothing else; visudo -c -f validates this file before installation.
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart iotagle.service, /bin/systemctl status iotagle.service, /bin/systemctl is-active iotagle.service
EOF
chmod 0440 "${SUDOERS_FILE}.tmp"
visudo -c -f "${SUDOERS_FILE}.tmp"
mv "${SUDOERS_FILE}.tmp" "${SUDOERS_FILE}"

echo ">> ssh: key-only auth"
SSHD_CONF=/etc/ssh/sshd_config.d/iotagle.conf
cat > "${SSHD_CONF}" <<'EOF'
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
ChallengeResponseAuthentication no
EOF
chmod 0644 "${SSHD_CONF}"
systemctl reload ssh || systemctl reload sshd || true

echo ">> ufw"
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw --force enable

echo ">> fail2ban enabled"
systemctl enable --now fail2ban

echo ">> nginx site"
NGINX_SITE=/etc/nginx/sites-available/iotagle
if [[ -f "${APP_DIR}/deploy/nginx/iotagle.conf" ]]; then
    install -m 0644 "${APP_DIR}/deploy/nginx/iotagle.conf" "${NGINX_SITE}"
    ln -sf "${NGINX_SITE}" /etc/nginx/sites-enabled/iotagle
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl reload nginx
else
    echo "   (no nginx config yet — clone the repo into ${APP_DIR} then rerun)"
fi

echo ">> systemd unit + socket"
if [[ -f "${APP_DIR}/deploy/systemd/iotagle.service" ]]; then
    install -m 0644 "${APP_DIR}/deploy/systemd/iotagle.service" /etc/systemd/system/iotagle.service
    install -m 0644 "${APP_DIR}/deploy/systemd/iotagle.socket"  /etc/systemd/system/iotagle.socket
    systemctl daemon-reload
    systemctl enable --now iotagle.socket
    if [[ -d "${VENV_DIR}" ]]; then
        systemctl enable --now iotagle.service
    else
        echo "   (venv missing; service not started yet)"
    fi
else
    echo "   (no systemd unit yet — clone the repo into ${APP_DIR} then rerun)"
fi

echo ">> done"
echo "Next steps if first-time setup:"
echo "  sudo -u ubuntu git clone https://github.com/<you>/iotagle.git ${APP_DIR}"
echo "  python3.11 -m venv ${VENV_DIR} && ${VENV_DIR}/bin/pip install -r ${APP_DIR}/requirements.txt"
echo "  sudo ./deploy/bootstrap.sh    # rerun to install the nginx site + service"
