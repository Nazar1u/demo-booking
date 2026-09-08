"""Відхилити броні, які не оплатили у відведене вікно.

Оплату ми не моніторимо (умова ТЗ), тому «не оплачено» тут означає рівно
«вікно PAYMENT_WINDOW_MINUTES закінчилось, а підтвердження ми не отримали».
Це рішення власника проєкту, а не сигнал від платіжного сервісу.

Команда ідемпотентна: повторний запуск нічого не змінює.
"""

import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from booking.models import Booking


class Command(BaseCommand):
    help = 'Переводить неоплачені броні в статус expired після дедлайну'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показати, що було б змінено, і нічого не змінювати',
        )
        parser.add_argument(
            '--quiet', action='store_true',
            help='Нічого не виводити, якщо змінювати нічого',
        )

    def handle(self, *args, **options):
        window = dt.timedelta(minutes=settings.PAYMENT_WINDOW_MINUTES)
        cutoff = timezone.now() - window

        stale = Booking.objects.filter(
            status=Booking.Status.PENDING,
            created_at__lte=cutoff,
        )

        if options['dry_run']:
            for booking in stale:
                self.stdout.write(
                    f'[dry-run] pk={booking.pk} '
                    f'servio={booking.servio_booking_id or "—"} '
                    f'створена {booking.created_at:%Y-%m-%d %H:%M} → expired'
                )
            self.stdout.write(f'[dry-run] всього: {stale.count()}')
            return

        # Один UPDATE замість запису по одному: команда крутиться щохвилини.
        ids = list(stale.values_list('pk', flat=True))
        if not ids:
            if not options['quiet']:
                self.stdout.write('протермінованих броней немає')
            return

        Booking.objects.filter(pk__in=ids).update(
            status=Booking.Status.EXPIRED,
            updated_at=timezone.now(),
        )
        self.stdout.write(self.style.SUCCESS(
            f'відхилено як неоплачені: {len(ids)} '
            f'(pk: {", ".join(str(i) for i in ids)})'
        ))
