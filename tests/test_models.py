"""Тести моделі `Booking` і команди `ensure_admin`.

`ensource_admin` виконується з entrypoint контейнера на кожному старті, тому
її ідемпотентність — умова деплою, а не зручність.
"""

import datetime as dt
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from booking.models import Booking

CHECK_IN = dt.date(2027, 3, 10)
CHECK_OUT = dt.date(2027, 3, 12)

DEFAULTS = {
    'check_in': CHECK_IN,
    'check_out': CHECK_OUT,
    'adults': 2,
    'room_type_id': 1373,
    'contract_condition_id': 107,
    'api_price_list_id': 2,
    'room_name': 'Номер Стандарт Дабл з видом на ліс',
    'price': Decimal('16200.00'),
    'guest_name': 'Тест Тестенко',
    'guest_email': 'test@example.com',
    'guest_phone': '+380441234567',
}


def make(**overrides):
    return Booking.objects.create(**(DEFAULTS | overrides))


class BookingModelTests(TestCase):
    def test_defaults(self):
        booking = make()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.currency, 980)
        self.assertEqual(booking.children, 0)
        self.assertEqual(booking.children_ages, [])
        self.assertEqual(booking.country_of_residence, 'ua')
        self.assertEqual(booking.payment_url, '')
        self.assertEqual(booking.raw_request, {})
        self.assertIsNotNone(booking.created_at)

    def test_check_in_and_out_times_default_to_hotel_policy(self):
        booking = make()
        booking.refresh_from_db()
        self.assertEqual(booking.time_arrival, dt.time(14, 0))
        self.assertEqual(booking.time_departure, dt.time(12, 0))

    def test_nights_guests_and_currency(self):
        booking = make(children=1, children_ages=[7])
        self.assertEqual(booking.nights, 2)
        self.assertEqual(booking.guests, 3)
        self.assertEqual(booking.currency_code, 'UAH')

    def test_unknown_currency_falls_back_to_the_numeric_code(self):
        self.assertEqual(make(currency=784).currency_code, '784')

    def test_str_is_readable_in_the_admin(self):
        text = str(make())
        self.assertIn('Тест Тестенко', text)
        self.assertIn('2027-03-10', text)

    def test_check_out_must_be_after_check_in(self):
        for check_in, check_out in [
            (CHECK_OUT, CHECK_IN),
            (CHECK_IN, CHECK_IN),
        ]:
            with self.subTest(check_in=check_in, check_out=check_out):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    make(check_in=check_in, check_out=check_out)

    def test_json_fields_survive_a_round_trip(self):
        """JSONField має бути портативним: у Фазі 6 та сама модель піде на
        Postgres без змін."""
        raw = {'rooms': [{'roomTypeID': 1373, 'childrenAges': [4, 9]}],
               'nested': {'ключ': 'значення', 'flag': True, 'none': None}}
        booking = make(raw_request=raw, children_ages=[4, 9])
        booking.refresh_from_db()
        self.assertEqual(booking.raw_request, raw)
        self.assertEqual(booking.children_ages, [4, 9])

    def test_newest_first(self):
        old = make(guest_name='Перший Гість')
        new = make(guest_name='Другий Гість')
        self.assertEqual(list(Booking.objects.all()), [new, old])

    def test_long_payment_urls_fit(self):
        """Платіжні посилання з підписами бувають довгі — поле на 1000."""
        url = ('https://pay.example/checkout?' + 'a=1&' * 300)[:1000]
        self.assertEqual(len(url), 1000)
        booking = make(payment_url=url)
        booking.refresh_from_db()
        self.assertEqual(booking.payment_url, url)


class EnsureAdminTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command('ensure_admin', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_creates_a_superuser(self):
        output = self.run_command()
        user = get_user_model().objects.get(username='admin')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password('demo'))
        self.assertIn('створений', output)

    def test_is_idempotent(self):
        self.run_command()
        output = self.run_command()
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertIn('без змін', output)

    def test_repairs_a_downgraded_account(self):
        self.run_command()
        User = get_user_model()
        User.objects.filter(username='admin').update(
            is_staff=False, is_superuser=False,
        )
        output = self.run_command()

        user = User.objects.get(username='admin')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertIn('is_staff', output)

    def test_resets_a_changed_password(self):
        self.run_command()
        User = get_user_model()
        user = User.objects.get(username='admin')
        user.set_password('something-else')
        user.save()

        self.run_command()
        user.refresh_from_db()
        self.assertTrue(user.check_password('demo'))

    def test_skip_password_reset_keeps_the_existing_password(self):
        self.run_command()
        User = get_user_model()
        user = User.objects.get(username='admin')
        user.set_password('something-else')
        user.save()

        self.run_command('--skip-password-reset')
        user.refresh_from_db()
        self.assertTrue(user.check_password('something-else'))

    def test_warns_about_the_demo_password(self):
        self.assertIn('демо-значення', self.run_command())
