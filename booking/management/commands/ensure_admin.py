from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Створити або оновити адміністратора з даних оточення.

    Викликається з entrypoint контейнера на кожному старті, тому має бути
    ідемпотентною: за умовами ТЗ заходити на сервер руками і робити
    createsuperuser не можна.
    """

    help = 'Ідемпотентно провізіонує адміністратора з ADMIN_* змінних оточення'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-password-reset',
            action='store_true',
            help='Не перезаписувати пароль, якщо користувач уже існує',
        )

    def handle(self, *args, **options):
        username = settings.ADMIN_USERNAME
        password = settings.ADMIN_PASSWORD
        email = settings.ADMIN_EMAIL

        if not username or not password:
            self.stderr.write(
                'ADMIN_USERNAME або ADMIN_PASSWORD не задані — пропускаю.'
            )
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(
            **{User.USERNAME_FIELD: username},
            defaults={'email': email, 'is_staff': True, 'is_superuser': True},
        )

        changes = []
        if created:
            user.set_password(password)
            changes.append('пароль')
        elif not options['skip_password_reset']:
            if not user.check_password(password):
                user.set_password(password)
                changes.append('пароль')

        if not user.is_staff:
            user.is_staff = True
            changes.append('is_staff')
        if not user.is_superuser:
            user.is_superuser = True
            changes.append('is_superuser')
        if email and user.email != email:
            user.email = email
            changes.append('email')

        if created or changes:
            user.save()

        verb = 'створений' if created else ('оновлений' if changes else 'без змін')
        detail = f' ({", ".join(changes)})' if changes else ''
        self.stdout.write(self.style.SUCCESS(f'Адмін {username!r}: {verb}{detail}'))

        if password == 'demo':
            self.stdout.write(self.style.WARNING(
                'ADMIN_PASSWORD має демо-значення. Для будь-якого не-демо '
                'середовища задай його через оточення.'
            ))
