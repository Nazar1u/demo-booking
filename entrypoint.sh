#!/bin/sh
# Старт контейнера. Усі три підготовчі кроки ідемпотентні, тому безпечно
# виконуються на кожному запуску — за умовами ТЗ заходити на сервер руками
# і робити migrate/createsuperuser не можна.
set -eu

echo "==> migrate"
python manage.py migrate --noinput

echo "==> collectstatic"
python manage.py collectstatic --noinput --clear

echo "==> ensure_admin"
python manage.py ensure_admin

# Таймаут навмисно великий: пошук у Servio відповідає ~9 с, а крок
# бронювання робить три послідовних виклики до чужого API. Дефолтні 30 с
# gunicorn убивали б воркер посеред створення броні.
echo "==> gunicorn"
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --forwarded-allow-ips '*'
