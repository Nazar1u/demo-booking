#!/usr/bin/env bash
# Налаштування SSH на сервері. Запускати від root.
#
# За рішенням власника проєкту root-пароль з ТЗ лишається чинним і не
# змінюється. Тому цей скрипт НІЧОГО не забирає за замовчуванням: він лише
# гарантує, що вхід по ключу увімкнений, і показує поточний стан.
#
# Обидва обмеження — опційні й вимикають парольний доступ, тому їх треба
# просити явно:
#
#   bash harden.sh                                  # тільки ключі + звіт
#   DISABLE_PASSWORD_AUTH=yes bash harden.sh        # SSH лише по ключу
#   DISABLE_PASSWORD_AUTH=yes LOCK_ROOT=yes bash harden.sh   # + заблокувати пароль root
#
# Запускати ТІЛЬКИ ПІСЛЯ того, як вхід по ключу перевірений живою спробою —
# інакше з DISABLE_PASSWORD_AUTH можна замкнути себе назовні.

set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DISABLE_PASSWORD_AUTH="${DISABLE_PASSWORD_AUTH:-no}"
LOCK_ROOT="${LOCK_ROOT:-no}"
CONF='/etc/ssh/sshd_config.d/99-demo-booking.conf'

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ПОМИЛКА: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die 'запускати від root'

# --- страховка від самоблокування ----------------------------------------

log 'перевірка, що вхід по ключу можливий'
AUTH="/home/${DEPLOY_USER}/.ssh/authorized_keys"
[ -s "$AUTH" ] || die "${AUTH} порожній або відсутній — спершу provision.sh"
KEYS="$(grep -cvE '^\s*(#|$)' "$AUTH")"
echo "ключів у ${DEPLOY_USER}: ${KEYS}"
[ "$KEYS" -ge 1 ] || die 'жодного ключа'

grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config \
    || die 'sshd_config не підключає sshd_config.d/*.conf — правити треба вручну'

# --- конфіг ---------------------------------------------------------------

log "$CONF"
install -d -m 755 /etc/ssh/sshd_config.d
{
    echo '# demo-booking'
    echo 'PubkeyAuthentication yes'
    if [ "$DISABLE_PASSWORD_AUTH" = 'yes' ]; then
        echo '# Вхід лише по ключу (увімкнено явно через DISABLE_PASSWORD_AUTH=yes).'
        echo 'PasswordAuthentication no'
        echo 'KbdInteractiveAuthentication no'
        echo 'PermitRootLogin prohibit-password'
    else
        echo '# Парольний вхід лишається: root-пароль з ТЗ має бути чинним.'
    fi
} > "$CONF"
chmod 644 "$CONF"

# Перевіряємо конфіг ДО перезапуску: зламаний sshd не підніметься взагалі.
log 'sshd -t'
if ! sshd -t; then
    rm -f "$CONF"
    die 'конфіг не пройшов перевірку, зміни відкочено'
fi
echo 'ok'

log 'перезавантаження sshd'
# reload, а не restart: наявні сесії не обриваються.
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo 'готово'

# --- root -----------------------------------------------------------------

if [ "$LOCK_ROOT" = 'yes' ]; then
    log 'блокування пароля root'
    passwd -l root
    echo 'пароль root заблокований'
else
    log 'пароль root не змінювався і не блокувався — за рішенням власника'
fi

# --- звіт -----------------------------------------------------------------

log 'фактичний стан sshd'
sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication)' \
    | sed 's/^/    /'

if [ "$DISABLE_PASSWORD_AUTH" != 'yes' ]; then
    cat <<'EOF'

    Зауваження: парольний вхід по SSH відкритий, тобто пароль з ТЗ —
    діючий креденшл на публічному порту 22. Що його прикриває зараз:
    ufw пускає лише 22 і 8080, а застосунок працює під окремим
    користувачем deploy без пароля. Якщо колись знадобиться закрити —
    DISABLE_PASSWORD_AUTH=yes, ключ уже на місці.
EOF
fi
