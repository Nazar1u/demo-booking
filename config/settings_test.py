"""Налаштування для тестів: справжні settings плюс безпечні дефолти оточення.

Змінні виставляються **до** імпорту `settings`, щоб тести не залежали від
наявності `.env` і не бралися за справжні ключі. Через `setdefault`, тому CI
може перекрити будь-що явно. Ключі Servio підставні навмисно: жоден тест не
має права звертатися до живого API.

Єдине власне налаштування — швидкий хешер паролів.
"""

import os

os.environ.setdefault('SECRET_KEY', 'test-only-not-a-secret')
os.environ.setdefault('DEBUG', 'False')
os.environ.setdefault('DATABASE_URL', 'sqlite://:memory:')
os.environ.setdefault('SERVIO_COMPANY_KEY', 'TEST-COMPANY-KEY')
os.environ.setdefault('SERVIO_HOTEL_ID', '161')
os.environ.setdefault('SERVIO_API_BASE', 'https://servio.invalid/hms/api')
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'demo')
os.environ.setdefault('LOG_LEVEL', 'CRITICAL')

from .settings import *  # noqa: F403

# PBKDF2 навмисно повільний — у тестах це єдине, що займало час.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
