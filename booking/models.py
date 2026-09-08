import datetime as dt

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Booking(models.Model):
    """Бронь, створена через цей сайт.

    Це журнал: рядок з'являється лише на останньому кроці, коли Servio вже
    прийняв бронь (див. docs/PLAN.md, Фаза 4). Редагувати його немає сенсу —
    джерелом істини для готелю є HMS, а не ця база.

    `raw_request` / `raw_response` зберігаються навмисно: контракт Servio
    недокументований (docs/servio-api.md), і без сирих payload'ів розбір
    інциденту неможливий.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Очікує оплати'
        CONFIRMED = 'confirmed', 'Підтверджена'
        FAILED = 'failed', 'Відхилена — оплату не вдалося ініціювати'
        EXPIRED = 'expired', 'Відхилена — час на оплату вийшов'

    CURRENCY_CHOICES = [
        (980, 'UAH'),
        (840, 'USD'),
        (978, 'EUR'),
    ]

    # --- Пошук ---------------------------------------------------------
    check_in = models.DateField('Заїзд')
    check_out = models.DateField('Виїзд')
    time_arrival = models.TimeField('Час заїзду', default='14:00')
    time_departure = models.TimeField('Час виїзду', default='12:00')
    adults = models.PositiveSmallIntegerField('Дорослих', default=1)
    children = models.PositiveSmallIntegerField('Дітей', default=0)
    # Servio очікує childrenAges для кожної дитини; порожній список — норма.
    children_ages = models.JSONField('Вік дітей', default=list, blank=True)

    # --- Оффер ---------------------------------------------------------
    # Ця трійка йде в /book незмінною, інакше Servio відхилить запит.
    room_type_id = models.IntegerField('ID типу номера')
    contract_condition_id = models.IntegerField('ID тарифу')
    api_price_list_id = models.IntegerField('ID прайс-листа')

    room_name = models.CharField('Номер', max_length=255)
    price = models.DecimalField('Сума', max_digits=10, decimal_places=2)
    currency = models.PositiveSmallIntegerField(
        'Валюта', choices=CURRENCY_CHOICES, default=980,
    )

    # --- Гість ---------------------------------------------------------
    guest_name = models.CharField("Ім'я гостя", max_length=255)
    guest_email = models.EmailField('Email')
    guest_phone = models.CharField('Телефон', max_length=32)
    country_of_residence = models.CharField(
        'Країна проживання', max_length=2, default='ua',
    )
    comment = models.TextField('Коментар', blank=True)

    # --- Інтеграція з Servio -------------------------------------------
    servio_booking_id = models.CharField(
        'Номер броні в Servio', max_length=64, blank=True,
        help_text='apiReservationID з відповіді /book',
    )
    # Nullable навмисно: Servio не завжди віддає посилання — залежно від
    # paymentServiceID оплата може йти через iframe або form-post.
    payment_url = models.URLField('Посилання на оплату', max_length=1000, blank=True)
    status = models.CharField(
        'Статус', max_length=16, choices=Status.choices, default=Status.PENDING,
    )
    raw_request = models.JSONField('Сирий запит', default=dict, blank=True)
    raw_response = models.JSONField('Сира відповідь', default=dict, blank=True)

    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'бронь'
        verbose_name_plural = 'броні'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=Q(check_out__gt=F('check_in')),
                name='booking_check_out_after_check_in',
            ),
        ]
        indexes = [
            models.Index(fields=['status'], name='booking_status_idx'),
            models.Index(fields=['check_in'], name='booking_check_in_idx'),
            models.Index(fields=['servio_booking_id'], name='booking_servio_id_idx'),
        ]

    def __str__(self):
        return f'{self.guest_name} — {self.room_name} ({self.check_in} → {self.check_out})'

    @property
    def nights(self):
        return (self.check_out - self.check_in).days

    @property
    def guests(self):
        return self.adults + self.children

    @property
    def currency_code(self):
        return dict(self.CURRENCY_CHOICES).get(self.currency, str(self.currency))

    # --- вікно на оплату ------------------------------------------------
    # Оплату ми не моніторимо (умова ТЗ), тому «не оплачено» тут означає
    # рівно «час вийшов, а ми не отримали підтвердження». Це рішення
    # власника проєкту, а не сигнал від платіжного сервісу.

    @property
    def payment_deadline(self):
        window = dt.timedelta(minutes=settings.PAYMENT_WINDOW_MINUTES)
        return self.created_at + window

    @property
    def seconds_left(self):
        if self.status != self.Status.PENDING:
            return 0
        left = (self.payment_deadline - timezone.now()).total_seconds()
        return max(0, int(left))

    @property
    def is_payment_expired(self):
        """Час вийшов — незалежно від того, чи хтось уже оновив статус."""
        return (
            self.status == self.Status.PENDING
            and timezone.now() >= self.payment_deadline
        )

    @property
    def is_payable(self):
        """Чи можна вести гостя на оплату з нашого сайту.

        `payment_url` веде на Servio, і відкликати його ми не можемо —
        забороняємо лише доступ через себе.
        """
        return (
            self.status == self.Status.PENDING
            and bool(self.payment_url)
            and not self.is_payment_expired
        )

    def expire(self, save=True):
        """Перевести протерміновану бронь у `expired`. Ідемпотентно."""
        if not self.is_payment_expired:
            return False
        self.status = self.Status.EXPIRED
        if save:
            self.save(update_fields=['status', 'updated_at'])
        return True
