# demo-booking

Демо-сайт бронювання готелю Riverwood поверх недокументованого API віджета
HMS Servio.

- **Сайт:** <http://157.90.116.151:8080/>
- **Адмінка:** <http://157.90.116.151:8080/admin/> — `admin` / `demo`
- **Health-check:** <http://157.90.116.151:8080/healthz>

> ⚠️ Це працює з **бойовим API живого готелю**. Кожна бронь займає реальний
> номер. Форма пускає тільки **березень 2027**, щоб не блокувати найближчі
> дати — обмеження вбудоване в код, а не тримається на дисциплині.

## Що вміє

Чотири кроки: дати й гості → вибір номера з актуальними цінами і залишком →
дані гостя → створення броні в HMS і перехід до оплати. Бронь зберігається в
Postgres, видна в адмінці. Неоплачена бронь через 20 хвилин відхиляється.

## Стек

Python 3.14 · Django 6.1 · PostgreSQL 17 · httpx · gunicorn · whitenoise ·
Docker · uv · pytest · ruff

Без JS-фреймворків і без CDN: класичний POST/redirect/GET, єдиний інлайновий
скрипт блокує повторний сабміт (пошук у Servio відповідає ~9 с).

## Локальний запуск

Потрібен лише [uv](https://docs.astral.sh/uv/) — Python він поставить сам.

```powershell
Copy-Item env.example .env
# згенерувати SECRET_KEY і вписати в .env:
uv run python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"

uv sync
uv run manage.py migrate
uv run manage.py ensure_admin      # створить admin:demo
uv run manage.py runserver
```

Локально працює SQLite — Docker і Postgres для розробки не потрібні.

> `env.example` названий без крапки навмисно: харнес агента блокує запис у
> `.env*`. Django читає саме `.env`.

### Тести й лінт

```powershell
uv run pytest -q                                              # 161 тест, ~4 с
uv run ruff check .
uv run python manage.py test --settings=config.settings_test   # теж працює
```

Тести не звертаються до живого API: транспорт httpx підмінений, ключі Servio
в тестових налаштуваннях підставні. Фікстури в `tests/fixtures/servio/` —
справжні відповіді, знятні з API.

## Змінні оточення

| Змінна | Дефолт | Призначення |
|---|---|---|
| `SECRET_KEY` | — | обов'язкова, без дефолту навмисно |
| `DEBUG` | `False` | |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | на сервері **мусить** містити `127.0.0.1` — туди стукає healthcheck контейнера |
| `CSRF_TRUSTED_ORIGINS` | — | обов'язкова на нестандартному порту, інакше не працює логін в адмінку |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | єдине місце, де вибирається БД |
| `CONN_MAX_AGE` / `CONN_HEALTH_CHECKS` | `0` / `False` | у контейнері `60` / `True` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `demo` | для `ensure_admin` |
| `SERVIO_API_BASE` | базовий URL Servio | |
| `SERVIO_COMPANY_KEY` | — | GUID tenant готелю |
| `SERVIO_HOTEL_ID` | `0` | числовий id готелю (`161`) |
| `SERVIO_TIMEOUT` | `15` | секунди |
| `PAYMENT_WINDOW_MINUTES` | `20` | скільки бронь чекає на оплату |
| `LOG_LEVEL` | `INFO` | |
| `EMAIL_BACKEND` | console | сайт листів не надсилає |

Змінні для сервера й compose — у другій половині `env.example`.

## Структура

```
booking/
  models.py            Booking — журнал броней
  forms.py             валідація дат, гостей, телефону
  views.py             4 кроки + /pay/<pk>/ + /healthz
  session.py           стан кроків 1–3 (у БД нічого не пишемо до кроку 4)
  admin.py             штатна адмінка Django, режим read-only
  services/servio.py   ЄДИНА точка контакту з чужим API
  management/commands/
    ensure_admin.py    ідемпотентне створення адміністратора
    expire_bookings.py відхилення неоплачених броней
config/                settings, urls, wsgi
deploy/                provision.sh, harden.sh, runbook
docs/
  PLAN.md              план і журнал рішень по фазах
  servio-api.md        реверс-інжиніринг API Servio
tests/                 161 тест + фікстури живих відповідей
```

## Рішення, які варто знати

**Уся інтеграція — в одному модулі.** `booking/services/servio.py` віддає
назовні dataclass'и; view не знає структури чужого JSON.

**HTTP-статус Servio завжди 200.** Помилка — це `isError: true` в тілі.
Клієнт ніде не дивиться на код відповіді. Порожній `data: []` означає «немає
доступності», а не помилку.

**Retry розділений за ідемпотентністю.** Пошук повторюється до трьох разів;
`/book` і `/make-payment` — **ніколи**: Servio не ідемпотентний, і друга
спроба створила б другу бронь у живому готелі.

**`follow_redirects=True` обов'язковий.** `/make-payment` відповідає HTTP 307;
без цього клієнт отримує тіло редіректу замість JSON. У браузері це не
видно — `fetch()` слідує редіректам сам.

**Бронь пишеться в БД до спроби оплати.** Якщо оплата зірветься, номер у
готелі вже зайнятий, і слід мусить залишитись. Зворотний порядок втратив би
реальну бронь — це перевірено на живій броні.

**Перед `/book` доступність перезапитується.** Номер розібрали між кроками →
назад на крок 2; ціна змінилась → назад на крок 3 з попередженням.

**Телефон нормалізується** у формат, який приймає Servio (`+` і цифри
підряд). Користувач пише як звик.

**Оплату не моніторимо** (умова ТЗ). Тому `expired` означає «вікно
закінчилось, а підтвердження ми не отримали», а не «точно не оплачено».

## Деплой

CI/CD: [.github/workflows/ci.yml](.github/workflows/ci.yml).

Push у `main` → `lint + тести (SQLite)` ‖ `тести (Postgres 17)` ‖
`збірка образу + запуск стека` → `образ під arm64 у GHCR` →
`SSH: pull, up -d, /healthz`.

Міграції, `collectstatic` і `ensure_admin` виконує `entrypoint.sh` при
старті контейнера, тому окремого кроку міграцій у пайплайні немає: здоровий
`/healthz` означає, що вони пройшли.

### Секрети репозиторію

Потрібні три. Приватний ключ передається з файлу, а не через буфер обміну:

```powershell
gh secret set SSH_HOST --body "157.90.116.151"
gh secret set SSH_USER --body "deploy"
gh secret set SSH_PRIVATE_KEY < $env:USERPROFILE\.ssh\demo-booking_ed25519
```

Без `gh` — Settings → Secrets and variables → Actions → New repository secret.
`GITHUB_TOKEN` створюється автоматично, для GHCR його достатньо.

### Сервер

Налаштовується скриптами, не руками — див.
[deploy/README.md](deploy/README.md). Коротко: `provision.sh` створює
користувача `deploy`, ставить Docker, закриває `ufw` до 22 і 8080, генерує
`.env` **на сервері** (секрети не проходять ні через git, ні через CI).

Сервер — **aarch64**, Ubuntu 26.04. Тому реліз збирається на ARM-раннері:
образ з `ubuntu-latest` (amd64) там не запуститься взагалі.

### Вручну

```powershell
$key = "$env:USERPROFILE\.ssh\demo-booking_ed25519"
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose ps"
ssh -i $key deploy@157.90.116.151 "cd /opt/demo-booking && docker compose logs --tail 100 web"
```

## Обмеження

- **Статус оплати невідомий.** Вебхука немає, опитування немає. `expired`
  ставиться по таймеру
- **Посилання на оплату відкликати неможливо** — воно живе на боці Servio.
  Після дедлайну ми лише перестаємо його віддавати
- **`confirmed` не виставляється ніде** — підтвердити бронь може лише готель
  у HMS, а наша адмінка read-only
- **Скасування броні через API недоступне** — `isReservationCancellationEnabled:
  false` у налаштуваннях готелю
- **HTTP без TLS** — так вимагає ТЗ. Усі TLS-перемикачі env-driven, тож
  розгортання за HTTPS не потребує змін коду
- **Один сервер, без rollback-кроку.** Відкат — це деплой попереднього тега
  образу (вони незмінні, по SHA)
- Контракт `/book` і `/make-payment` відреверсений, не документований
  вендором: він може змінитися без попередження. Тому кожен виклик зберігає
  сирі payload'и в `raw_request` / `raw_response`
