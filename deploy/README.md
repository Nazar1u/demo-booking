# Розгортання на сервер

За умовами ТЗ сервер налаштовується **без ручної роботи** — через скрипти,
а не поштучними командами в консолі. Тут два скрипти й один рядок, який
доводиться виконати людині.

## Чому один крок робить людина

Провайдер віддав віртуалку з root-паролем у відкритому вигляді в PDF. Агент
не працює з паролями: щоб він міг зайти, на сервері спершу має з'явитися
його публічний ключ. Це і є той єдиний вхід по паролю — далі все по ключу.

## Про root-пароль

**За рішенням власника проєкту пароль з ТЗ лишається чинним** — його не
змінюють і не блокують. Тому `harden.sh` за замовчуванням нічого не забирає:
він лише переконується, що вхід по ключу працює, і показує стан sshd.

Що це означає на практиці: пароль, який їздив у PDF, лишається діючим
креденшлом на публічному порту 22. Прикриває це `ufw` (пускає тільки 22 і
8080) і те, що застосунок працює під окремим користувачем `deploy` без
пароля. Якщо колись знадобиться закрити — ключ уже на місці:

```powershell
ssh -i $key root@157.90.116.151 "DISABLE_PASSWORD_AUTH=yes bash /root/harden.sh"
```

## Крок 0 — один раз, руками

Публічний ключ проєкту (не секрет):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO7elGTzOoO9KVjbmwiv1GL6XumQy8ZlAw9G3cLytGPA demo-booking deploy key
```

З Windows-термінала, одним рядком — попросить пароль root:

```powershell
type $env:USERPROFILE\.ssh\demo-booking_ed25519.pub | ssh root@157.90.116.151 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Перевірка, що ключ став (пароля вже не спитає):

```powershell
ssh -i $env:USERPROFILE\.ssh\demo-booking_ed25519 root@157.90.116.151 "hostname; cat /etc/os-release | head -2"
```

Після цього агент працює сам.

## Крок 1 — провізіонінг

```powershell
$key = "$env:USERPROFILE\.ssh\demo-booking_ed25519"
$pub = Get-Content "$key.pub"

# скопіювати скрипти
scp -i $key deploy\provision.sh deploy\harden.sh root@157.90.116.151:/root/

# запустити
ssh -i $key root@157.90.116.151 "PUBKEY='$pub' SERVER_IP=157.90.116.151 bash /root/provision.sh"
```

`provision.sh` ідемпотентний — повторний запуск нічого не ламає і **не
перегенеровує вже створені секрети**. Що робить:

| Крок | Деталі |
|---|---|
| Користувач `deploy` | без пароля, вхід тільки по ключу, у групі `docker` |
| Docker | Engine + compose plugin з офіційного apt-репозиторію |
| `ufw` | впускає лише 22 і 8080, решта — deny |
| `/opt/demo-booking` | власник `deploy`, права 750 |
| `.env` | `SECRET_KEY` і `POSTGRES_PASSWORD` **генеруються на сервері** |

`ALLOWED_HOSTS` обов'язково містить `127.0.0.1,localhost` окрім зовнішнього
IP: `HEALTHCHECK` контейнера стукає в `127.0.0.1:8000`, і без loopback Django
відповідає йому 400 — контейнер назавжди лишається `health: starting`, хоча
ззовні сайт працює.

**Сервер на aarch64 (ARM64), Ubuntu 26.04.** Збірка на сервері нативна, але
образ із GitHub Actions за замовчуванням буде amd64 і не запуститься — див.
Фазу 9 в [../docs/PLAN.md](../docs/PLAN.md).

Секрети народжуються на сервері і залишаються там: вони не проходять ні
через репозиторій, ні через чат, ні через логи CI.

## Крок 2 — перевірити вхід від `deploy`

**До** захисту sshd, інакше можна замкнути себе назовні:

```powershell
ssh -i $key deploy@157.90.116.151 "id; docker --version; ls -la /opt/demo-booking"
```

Має показати `deploy` у групі `docker` і наявний `.env`.

## Крок 3 — SSH-конфіг

```powershell
ssh -i $key root@157.90.116.151 "bash /root/harden.sh"
```

За замовчуванням лише гарантує `PubkeyAuthentication yes` і друкує фактичний
стан sshd. Парольний вхід **не** вимикає — пароль з ТЗ має лишатися чинним.

Перед перезавантаженням перевіряє конфіг через `sshd -t` і відкочує зміни,
якщо той не пройшов; використовує `reload`, а не `restart`, тож поточна сесія
не обривається.

Опційні обмеження, якщо колись знадобляться:

```powershell
# SSH лише по ключу
ssh -i $key root@157.90.116.151 "DISABLE_PASSWORD_AUTH=yes bash /root/harden.sh"
# те саме + заблокувати пароль root
ssh -i $key root@157.90.116.151 "DISABLE_PASSWORD_AUTH=yes LOCK_ROOT=yes bash /root/harden.sh"
```

## Крок 4 — перший деплой

```powershell
# код на сервер
scp -i $key docker-compose.yml Dockerfile entrypoint.sh deploy@157.90.116.151:/opt/demo-booking/
scp -i $key -r booking config manage.py pyproject.toml uv.lock deploy@157.90.116.151:/opt/demo-booking/

# збірка і старт
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose up -d --build"

# перевірка
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose ps && curl -fsS localhost:8000/healthz"
```

Ззовні: <http://157.90.116.151:8080/> і <http://157.90.116.151:8080/admin/>
(`admin` / `demo`).

У Фазі 9 копіювання файлів замінюється на `docker compose pull` образу з
GHCR — саме для цього в `docker-compose.yml` є `WEB_IMAGE`.

## Діагностика

```powershell
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose logs --tail 100 web"
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose ps"
ssh -i $key root@157.90.116.151 "ufw status verbose"
```

Типове:

- **`/healthz` віддає 503** — web піднявся, а Postgres ні. Дивитись
  `docker compose logs db`
- **сторінка не відкривається ззовні, а `curl localhost:8000` на сервері
  працює** — це `ufw` або порт у compose
- **`Bad Request (400)`** — `ALLOWED_HOSTS` у `.env` не містить адреси,
  з якої заходиш
- **`CSRF verification failed` при вході в адмінку** — у `.env` немає
  `CSRF_TRUSTED_ORIGINS=http://<ip>:8080`
- **`exec ./entrypoint.sh: no such file or directory`** — CRLF у
  `entrypoint.sh`. Від цього стоїть `.gitattributes`, але при копіюванні
  через scp з Windows перевір: `file entrypoint.sh`
