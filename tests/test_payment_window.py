"""Вікно на оплату: після дедлайну бронь відхиляється, і наш сайт більше
не веде на оплату.

Важлива межа, яку ці тести фіксують явно: посилання живе на боці Servio, і
відкликати його ми не можемо. Перевіряється саме те, що **наш** сайт його не
віддає.
"""

import datetime as dt
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from booking.models import Booking

PAY_URL = 'https://pay.riverwood.com.ua/api/v1/checkout/deadbeef/form'

DEFAULTS = {
    'check_in': dt.date(2027, 3, 10),
    'check_out': dt.date(2027, 3, 12),
    'adults': 2,
    'room_type_id': 1373,
    'contract_condition_id': 107,
    'api_price_list_id': 2,
    'room_name': 'Номер Стандарт Дабл з видом на ліс',
    'price': Decimal('16200.00'),
    'guest_name': 'Тест Тестенко',
    'guest_email': 'test@example.com',
    'guest_phone': '+380441234567',
    'servio_booking_id': '0000049452',
    'payment_url': PAY_URL,
}


def make(minutes_ago=0, **overrides):
    booking = Booking.objects.create(**(DEFAULTS | overrides))
    if minutes_ago:
        # created_at має auto_now_add, тому правимо через UPDATE.
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=minutes_ago),
        )
        booking.refresh_from_db()
    return booking


class DeadlineTests(TestCase):
    def test_deadline_is_twenty_minutes_after_creation(self):
        booking = make()
        delta = booking.payment_deadline - booking.created_at
        self.assertEqual(delta, dt.timedelta(minutes=20))

    @override_settings(PAYMENT_WINDOW_MINUTES=5)
    def test_window_is_configurable(self):
        booking = make()
        self.assertEqual(
            booking.payment_deadline - booking.created_at,
            dt.timedelta(minutes=5),
        )

    def test_fresh_booking_is_payable(self):
        booking = make()
        self.assertTrue(booking.is_payable)
        self.assertFalse(booking.is_payment_expired)
        self.assertGreater(booking.seconds_left, 19 * 60)

    def test_booking_past_the_deadline_is_expired(self):
        booking = make(minutes_ago=21)
        self.assertTrue(booking.is_payment_expired)
        self.assertFalse(booking.is_payable)
        self.assertEqual(booking.seconds_left, 0)

    def test_exactly_at_the_deadline_counts_as_expired(self):
        booking = make(minutes_ago=20)
        self.assertTrue(booking.is_payment_expired)

    def test_a_minute_before_the_deadline_still_counts(self):
        booking = make(minutes_ago=19)
        self.assertFalse(booking.is_payment_expired)
        self.assertTrue(booking.is_payable)

    def test_booking_without_a_payment_url_is_not_payable(self):
        self.assertFalse(make(payment_url='').is_payable)

    def test_only_pending_bookings_expire(self):
        for status in (Booking.Status.FAILED, Booking.Status.CONFIRMED,
                       Booking.Status.EXPIRED):
            with self.subTest(status=status):
                booking = make(minutes_ago=60, status=status)
                self.assertFalse(booking.is_payment_expired)
                self.assertFalse(booking.is_payable)

    def test_expire_is_idempotent(self):
        booking = make(minutes_ago=21)
        self.assertTrue(booking.expire())
        self.assertEqual(booking.status, Booking.Status.EXPIRED)
        self.assertFalse(booking.expire())

    def test_expire_does_nothing_before_the_deadline(self):
        booking = make(minutes_ago=5)
        self.assertFalse(booking.expire())
        self.assertEqual(booking.status, Booking.Status.PENDING)


class PayViewTests(TestCase):
    def _own(self, booking):
        s = self.client.session
        s['booking.created_pk'] = booking.pk
        s.save()

    def test_redirects_to_servio_while_the_window_is_open(self):
        booking = make()
        self._own(booking)
        response = self.client.get(reverse('booking:pay', args=[booking.pk]))
        self.assertRedirects(response, PAY_URL, fetch_redirect_response=False)

    def test_refuses_after_the_deadline_and_expires_the_booking(self):
        booking = make(minutes_ago=21)
        self._own(booking)
        response = self.client.get(
            reverse('booking:pay', args=[booking.pk]), follow=True,
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)
        self.assertContains(response, 'Час на оплату вийшов')
        # головне: посилання Servio не віддане
        self.assertNotContains(response, PAY_URL)

    def test_refuses_for_someone_elses_session(self):
        booking = make()
        response = self.client.get(
            reverse('booking:pay', args=[booking.pk]), follow=True,
        )
        self.assertNotContains(response, PAY_URL)

    def test_refuses_when_there_is_no_payment_url(self):
        booking = make(payment_url='')
        self._own(booking)
        response = self.client.get(
            reverse('booking:pay', args=[booking.pk]), follow=True,
        )
        self.assertContains(response, 'немає посилання на оплату')

    def test_missing_booking_is_404(self):
        self.assertEqual(
            self.client.get(reverse('booking:pay', args=[999999])).status_code,
            404,
        )


class DonePageDeadlineTests(TestCase):
    def _own(self, booking):
        s = self.client.session
        s['booking.created_pk'] = booking.pk
        s.save()

    def test_shows_the_payment_button_and_deadline(self):
        booking = make()
        self._own(booking)
        response = self.client.get(reverse('booking:done', args=[booking.pk]))
        self.assertContains(response, 'Перейти до оплати')
        self.assertContains(response, reverse('booking:pay', args=[booking.pk]))
        self.assertContains(response, 'Оплатити треба до')
        # прямого посилання на Servio на сторінці немає
        self.assertNotContains(response, PAY_URL)

    def test_expired_booking_shows_rejection_not_a_button(self):
        booking = make(minutes_ago=25)
        self._own(booking)
        response = self.client.get(reverse('booking:done', args=[booking.pk]))
        self.assertContains(response, 'Час на оплату вийшов')
        self.assertNotContains(response, 'Перейти до оплати')

    def test_opening_the_page_expires_a_stale_booking(self):
        """Ліниве протермінування: сторінка показує правду навіть якщо
        планувальник саме зараз не працює."""
        booking = make(minutes_ago=25)
        self._own(booking)
        self.client.get(reverse('booking:done', args=[booking.pk]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.EXPIRED)


class ExpireBookingsCommandTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command('expire_bookings', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_expires_only_what_is_past_the_deadline(self):
        fresh = make(minutes_ago=5)
        stale = make(minutes_ago=21)

        output = self.run_command()

        fresh.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(fresh.status, Booking.Status.PENDING)
        self.assertEqual(stale.status, Booking.Status.EXPIRED)
        self.assertIn(str(stale.pk), output)

    def test_is_idempotent(self):
        make(minutes_ago=21)
        self.run_command()
        output = self.run_command()
        self.assertIn('немає', output)
        self.assertEqual(
            Booking.objects.filter(status=Booking.Status.EXPIRED).count(), 1,
        )

    def test_dry_run_changes_nothing(self):
        stale = make(minutes_ago=21)
        output = self.run_command('--dry-run')
        stale.refresh_from_db()
        self.assertEqual(stale.status, Booking.Status.PENDING)
        self.assertIn('dry-run', output)

    def test_quiet_says_nothing_when_there_is_nothing_to_do(self):
        self.assertEqual(self.run_command('--quiet'), '')

    def test_does_not_touch_other_statuses(self):
        failed = make(minutes_ago=60, status=Booking.Status.FAILED)
        confirmed = make(minutes_ago=60, status=Booking.Status.CONFIRMED)
        self.run_command()
        failed.refresh_from_db()
        confirmed.refresh_from_db()
        self.assertEqual(failed.status, Booking.Status.FAILED)
        self.assertEqual(confirmed.status, Booking.Status.CONFIRMED)

    def test_does_not_call_servio(self):
        """Оплату ми не моніторимо — команда працює лише з часом."""
        make(minutes_ago=21)
        with mock.patch('booking.services.servio.ServioClient') as client:
            self.run_command()
        client.assert_not_called()


class AdminDeadlineTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_superuser('admin', '', 'demo')
        self.client.login(username='admin', password='demo')

    def test_expired_booking_link_is_inert(self):
        make(minutes_ago=25)
        response = self.client.get('/admin/booking/booking/')
        html = response.content.decode()
        self.assertIn('неактуальне', html)
        self.assertNotIn(f'href="{PAY_URL}"', html)

    def test_active_booking_link_is_clickable(self):
        make()
        response = self.client.get('/admin/booking/booking/')
        self.assertIn(f'href="{PAY_URL}"', response.content.decode())
