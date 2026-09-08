#!/usr/bin/env bash
# Первинне налаштування сервера. Виконується від root, ідемпотентно —
# повторний запуск нічого не ламає і не перегенеровує вже створені секрети.
#
#   PUBKEY="ssh-ed25519 AAAA... deploy" bash provision.sh
#
# Що робить:
#   1. користувач deploy з доступом по ключу і в групі docker
#   2. Docker Engine + compose plugin з офіційного репозиторію
#   3. ufw: тільки 22 і 8080
#   4. /opt/demo-booking з .env; SECRET_KEY і POSTGRES_PASSWORD генеруються
#      ТУТ, на сервері — вони не проходять ні через репозиторій, ні через чат
#
# Захист sshd і блокування root-пароля — окремо, у harden.sh: спершу треба
# переконатися, що вхід по ключу справді працює, інакше можна замкнути себе.

set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/opt/demo-booking}"
SERVER_IP="${SERVER_IP:-$(hostname -I | awk '{print $1}')}"
WEB_PORT="${WEB_PORT:-8080}"
PUBKEY="${PUBKEY:-}"

# Публічні ідентифікатори віджета Riverwood (docs/servio-api.md), не секрети.
SERVIO_COMPANY_KEY="${SERVIO_COMPANY_KEY:-6DFA7A01-9E5E-4087-8073-1E0254672737}"
SERVIO_HOTEL_ID="${SERVIO_HOTEL_ID:-161}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ПОМИЛКА: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die 'запускати від root'

# --- 1. користувач deploy -------------------------------------------------

log "користувач ${DEPLOY_USER}"
if id "$DEPLOY_USER" >/dev/null 2>&1; then
    echo "вже існує"
else
    adduser --disabled-password --gecos '' "$DEPLOY_USER"
    echo "створено"
fi

# Пароля в цього користувача немає взагалі — тільки ключ.
passwd -l "$DEPLOY_USER" >/dev/null

SSH_DIR="/home/${DEPLOY_USER}/.ssh"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$SSH_DIR"

if [ -n "$PUBKEY" ]; then
    touch "${SSH_DIR}/authorized_keys"
    if grep -qxF "$PUBKEY" "${SSH_DIR}/authorized_keys"; then
        echo "ключ уже доданий"
    else
        printf '%s\n' "$PUBKEY" >> "${SSH_DIR}/authorized_keys"
        echo "ключ доданий"
    fi
    chown "$DEPLOY_USER:$DEPLOY_USER" "${SSH_DIR}/authorized_keys"
    chmod 600 "${SSH_DIR}/authorized_keys"
elif [ ! -s "${SSH_DIR}/authorized_keys" ]; then
    die "PUBKEY не передано, а ${SSH_DIR}/authorized_keys порожній — вхід буде неможливий"
else
    echo "PUBKEY не передано, лишаю наявні ключі"
fi

# --- 2. Docker ------------------------------------------------------------

log 'Docker Engine + compose plugin'
if command -v docker >/dev/null 2>&1; then
    echo "уже встановлений: $(docker --version)"
else
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) die "непідтримувана ОС: ${ID:-невідома}. Потрібен Debian або Ubuntu" ;;
    esac

    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
    echo "встановлено: $(docker --version)"
fi

# Без sudo: compose запускається від deploy, якому достатньо групи docker.
usermod -aG docker "$DEPLOY_USER"
echo "${DEPLOY_USER} у групі docker"

# --- 3. файрвол -----------------------------------------------------------

log "ufw: 22 і ${WEB_PORT}"
apt-get install -y -qq ufw
# Спершу дозволяємо ssh — інакше enable обірве поточну сесію.
ufw allow 22/tcp >/dev/null
ufw allow "${WEB_PORT}/tcp" >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/    /'

# --- 4. каталог застосунку і .env ----------------------------------------

log "${APP_DIR}"
install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"

ENV_FILE="${APP_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    echo ".env уже є — не перегенеровую (секрети лишаються ті самі)"
else
    # Секрети народжуються тут і залишаються тут.
    SECRET_KEY="$(openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-50)"
    POSTGRES_PASSWORD="$(openssl rand -base64 32 | tr -d '\n=+/' | cut -c1-32)"

    umask 077
    cat > "$ENV_FILE" <<EOF
# Згенеровано provision.sh $(date -Is)
# Цей файл живе тільки на сервері. У git його немає і бути не має.

SECRET_KEY=${SECRET_KEY}
DEBUG=False
# Loopback потрібен обовʼязково: HEALTHCHECK контейнера стукає в
# 127.0.0.1:8000, і без цього Django відповідав би йому 400, а контейнер
# ніколи не ставав би healthy.
ALLOWED_HOSTS=${SERVER_IP},127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=http://${SERVER_IP}:${WEB_PORT}

POSTGRES_DB=booking
POSTGRES_USER=booking
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
CONN_MAX_AGE=60
CONN_HEALTH_CHECKS=True

# admin:demo — вимога ТЗ для демо-стенда
ADMIN_USERNAME=admin
ADMIN_PASSWORD=demo

SERVIO_API_BASE=https://smartspot.servio.support/ServioQR/hms/api
SERVIO_COMPANY_KEY=${SERVIO_COMPANY_KEY}
SERVIO_HOTEL_ID=${SERVIO_HOTEL_ID}
SERVIO_TIMEOUT=15

WEB_PORT=${WEB_PORT}
GUNICORN_WORKERS=3
GUNICORN_TIMEOUT=120
LOG_LEVEL=INFO
EOF
    chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo ".env створений, секрети згенеровані на сервері"
fi

log 'готово'
cat <<EOF
    користувач:  ${DEPLOY_USER}
    каталог:     ${APP_DIR}
    docker:      $(docker --version)
    compose:     $(docker compose version --short 2>/dev/null || echo '?')
    порт:        ${WEB_PORT}

Далі:
  1. перевірити вхід по ключу:  ssh ${DEPLOY_USER}@${SERVER_IP}
  2. і лише ПОТІМ harden.sh (за замовчуванням нічого не забирає;
     парольний вхід вимикається лише з DISABLE_PASSWORD_AUTH=yes)
EOF
