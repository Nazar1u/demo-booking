"""Наскрізні тести чотирьох кроків бронювання.

Клієнт Servio замокано: жоден тест не стукає в живий готель. Дані офферів —
з реальної фікстури `rooms-response.json`.
"""

import datetime as dt
import json
import pathlib
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from booking.models import Booking
from booking.services.servio import (
    BookingResult,
    PaymentResult,
    ServioAPIError,
    ServioUnavailableError,
    parse_room_offers,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'servio'

CHECK_IN = dt.date(2027, 3, 10)
CHECK_OUT = dt.date(2027, 3, 12)

SEARCH_POST = {
    'check_in': '2027-03-10',
    'check_out': '2027-03-12',
    'adults': '2',
    'children': '0',
    'children_ages': '',
}
GUEST_POST = {
    'full_name': 'Тест Тестенко',
    'email': 'test@example.com',
    'phone': '+380 44 123 45 67',
    'comment': 'вид на озеро, якщо можливо',
}

CRITERIA = {
    'check_in': CHECK_IN,
    'check_out': CHECK_OUT,
    'time_arrival': '14:00',
    'time_departure': '12:00',
    'adults': 2,
    'children': 0,
    'children_ages': (),
}


def real_offers():
    data = json.loads((FIXTURES / 'rooms-response.json').read_text(encoding='utf-8'))
    return parse_room_offers(data['data'], CRITERIA)


def booking_result(reservation_id='R-777', amount=Decimal('16200.0')):
    return BookingResult(
        reservation_id=reservation_id,
        hotel_id=161,
        total_amount=amount,
        currency=980,
        raw_request={'rooms': [{'roomTypeID': 1373}]},
        raw_response={'isError': False, 'data': {'apiReservationID': reservation_id}},
    )


class FakeClient:
    """Підміна ServioClient: повертає задане, записує виклики."""

    def __init__(self, offers=None, book=None, payment=None,
                 payment_info=None, search_error=None, book_error=None,
                 payment_error=None):
        self._offers = offers if offers is not None else real_offers()
        self._book = book if book is not None else booking_result()
        self._payment = payment or PaymentResult(
            payment_service_id=3, redirect_url='https://pay.example/R-777',
            raw_response={'isError': False},
        )
        self._payment_info = payment_info or {'services': [{'total': 16200}]}
        self._search_error = search_error
        self._book_error = book_error
        self._payment_error = payment_error
        self.searches = []
        self.bookings = []
        self.payments = []

    def __call__(self, request):  # замінює make_client(request)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def search_rooms(self, **criteria):
        self.searches.append(criteria)
        if self._search_error:
            raise self._search_error
        return self._offers

    def create_booking(self, offer, guest):
        self.bookings.append((offer, guest))
        if self._book_error:
            raise self._book_error
        return self._book

    def get_payment_info(self, account, currency=980, promo_code=None):
        if self._payment_error:
            raise self._payment_error
        return self._payment_info

    def make_payment(self, **kwargs):
        self.payments.append(kwargs)
        if self._payment_error:
            raise self._payment_error
        return self._payment


class FlowTestCase(TestCase):
    def use(self, client_stub):
        patcher = mock.patch('booking.views.make_client', client_stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client_stub

    def do_search(self, **overrides):
        return self.client.post(reverse('booking:search'), SEARCH_POST | overrides)

    def pick_first_offer(self):
        response = self.client.get(reverse('booking:rooms'))
        offer = response.context['offers'][0]
        self.client.post(reverse('booking:rooms'), {'offer_key': offer.offer_key})
        return offer


class Step1SearchTests(FlowTestCase):
    def test_defaults_point_at_march_2027(self):
        response = self.client.get(reverse('booking:search'))
        form = response.context['form']
        self.assertEqual(form.initial['check_in'], CHECK_IN)
        self.assertEqual(form.initial['check_out'], CHECK_OUT)

    def test_valid_search_goes_to_step_2(self):
        response = self.do_search()
        self.assertRedirects(
            response, reverse('booking:rooms'), fetch_redirect_response=False,
        )
        self.assertEqual(self.client.session['booking.search']['adults'], 2)

    def test_checkout_must_be_after_checkin(self):
        response = self.do_search(check_out='2027-03-09')
        self.assertContains(response, 'Виїзд має бути пізніше заїзду')

    def test_dates_outside_march_2027_are_rejected(self):
        response = self.do_search(check_in='2027-05-10', check_out='2027-05-12')
        self.assertContains(response, 'тільки на березень 2027')

    def test_too_many_guests_rejected(self):
        response = self.do_search(adults='9', children='5', children_ages='1,2,3,4,5')
        self.assertContains(response, 'Максимум 10 гостей')

    def test_children_require_ages(self):
        response = self.do_search(children='2')
        self.assertContains(response, 'Вкажіть вік кожної дитини')

    def test_children_ages_count_must_match(self):
        response = self.do_search(children='2', children_ages='5')
        self.assertContains(response, 'а дітей — 2')

    def test_child_older_than_the_hotel_limit_rejected(self):
        response = self.do_search(children='1', children_ages='15')
        self.assertContains(response, 'Старших оформлюйте як дорослих')

    def test_children_ages_reach_the_api(self):
        stub = self.use(FakeClient())
        self.do_search(children='2', children_ages='4, 9')
        self.client.get(reverse('booking:rooms'))
        self.assertEqual(stub.searches[0]['children'], 2)
        self.assertEqual(stub.searches[0]['children_ages'], (4, 9))


class Step2RoomsTests(FlowTestCase):
    def test_requires_a_search_first(self):
        response = self.client.get(reverse('booking:rooms'))
        self.assertRedirects(
            response, reverse('booking:search'), fetch_redirect_response=False,
        )

    def test_lists_available_offers_from_the_api(self):
        self.use(FakeClient())
        self.do_search()
        response = self.client.get(reverse('booking:rooms'))
        self.assertEqual(len(response.context['offers']), 11)
        self.assertContains(response, 'Номер Стандарт Дабл з видом на ліс')
        self.assertContains(response, '16200')

    def test_unavailable_offers_are_shown_separately(self):
        offers = real_offers()
        blocked = [
            offer for offer in offers
            if offer.room_type_id != 1373
        ]
        one_blocked = offers[0].__class__(
            **{**{f: getattr(offers[0], f) for f in offers[0].__slots__},
               'free_rooms': 0},
        )
        self.use(FakeClient(offers=[one_blocked, *blocked]))
        self.do_search()
        response = self.client.get(reverse('booking:rooms'))
        self.assertEqual(len(response.context['unavailable']), 1)
        self.assertContains(response, 'Немає вільних номерів')

    def test_api_unavailable_shows_a_message_not_a_crash(self):
        self.use(FakeClient(search_error=ServioUnavailableError('down')))
        self.do_search()
        response = self.client.get(reverse('booking:rooms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'зараз недоступний')
        self.assertEqual(response.context['offers'], [])

    def test_api_error_message_is_shown_to_the_user(self):
        self.use(FakeClient(search_error=ServioAPIError('Невірна дата')))
        self.do_search()
        response = self.client.get(reverse('booking:rooms'))
        self.assertContains(response, 'Невірна дата')

    def test_selecting_an_offer_goes_to_step_3(self):
        self.use(FakeClient())
        self.do_search()
        offer = self.pick_first_offer()
        self.assertEqual(
            self.client.session['booking.selected']['offer_key'], offer.offer_key,
        )

    def test_unknown_offer_key_is_rejected(self):
        self.use(FakeClient())
        self.do_search()
        self.client.get(reverse('booking:rooms'))
        response = self.client.post(
            reverse('booking:rooms'), {'offer_key': '1:2:3'}, follow=True,
        )
        self.assertContains(response, 'уже неактуальний')
        self.assertNotIn('booking.selected', self.client.session)

    def test_new_search_resets_the_selection(self):
        self.use(FakeClient())
        self.do_search()
        self.pick_first_offer()
        self.do_search()
        self.assertNotIn('booking.selected', self.client.session)


class Step3GuestTests(FlowTestCase):
    def setUp(self):
        self.stub = self.use(FakeClient())
        self.do_search()
        self.offer = self.pick_first_offer()

    def test_requires_a_selected_offer(self):
        self.client.session.flush()
        response = self.client.get(reverse('booking:guest'))
        self.assertRedirects(
            response, reverse('booking:search'), fetch_redirect_response=False,
        )

    def test_shows_the_chosen_offer_summary(self):
        response = self.client.get(reverse('booking:guest'))
        self.assertContains(response, self.offer.name)
        self.assertContains(response, '16200')

    def test_full_name_needs_two_words(self):
        response = self.client.post(
            reverse('booking:guest'), GUEST_POST | {'full_name': 'Тест'},
        )
        self.assertContains(response, 'та прізвище')
        self.assertFalse(Booking.objects.exists())

    def test_phone_is_validated(self):
        response = self.client.post(
            reverse('booking:guest'), GUEST_POST | {'phone': 'нема'},
        )
        self.assertContains(response, 'це не телефон')
        self.assertFalse(Booking.objects.exists())

    def test_car_number_required_when_travelling_by_car(self):
        response = self.client.post(
            reverse('booking:guest'), GUEST_POST | {'travels_by_car': 'on'},
        )
        self.assertContains(response, 'Вкажіть номер авто')

    def test_nothing_is_written_to_the_db_before_step_4(self):
        self.client.get(reverse('booking:search'))
        self.client.get(reverse('booking:rooms'))
        self.client.get(reverse('booking:guest'))
        self.assertFalse(Booking.objects.exists())


class Step4BookingTests(FlowTestCase):
    def start(self, stub=None):
        self.stub = self.use(stub or FakeClient())
        self.do_search()
        self.offer = self.pick_first_offer()
        return self.stub

    def test_happy_path_creates_a_booking_and_redirects_to_payment(self):
        self.start()
        response = self.client.post(reverse('booking:guest'), GUEST_POST)

        booking = Booking.objects.get()
        self.assertRedirects(
            response, 'https://pay.example/R-777', fetch_redirect_response=False,
        )
        self.assertEqual(booking.servio_booking_id, 'R-777')
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.price, Decimal('16200.0'))
        self.assertEqual(booking.room_name, self.offer.name)
        self.assertEqual(booking.guest_email, 'test@example.com')
        self.assertEqual(booking.check_in, CHECK_IN)
        self.assertEqual(booking.payment_url, 'https://pay.example/R-777')
        # оффер-трійка збережена
        self.assertEqual(booking.room_type_id, self.offer.room_type_id)
        self.assertEqual(booking.contract_condition_id, self.offer.contract_condition_id)
        self.assertEqual(booking.api_price_list_id, self.offer.api_price_list_id)
        # сирі payload'и обох викликів
        self.assertIn('book', booking.raw_response)
        self.assertIn('payment', booking.raw_response)

    def test_availability_is_revalidated_before_booking(self):
        stub = self.start()
        self.client.post(reverse('booking:guest'), GUEST_POST)
        # один пошук на крок 2, другий — перед створенням броні
        self.assertEqual(len(stub.searches), 2)

    def test_room_taken_between_step_2_and_step_4(self):
        stub = self.start()
        stub._offers = [o for o in real_offers() if o.offer_key != self.offer.offer_key]

        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)
        self.assertContains(response, 'уже недоступний на ці дати')
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(stub.bookings, [])

    def test_price_change_sends_the_user_back_to_confirm(self):
        stub = self.start()
        cheaper = self.offer.__class__(
            **{**{f: getattr(self.offer, f) for f in self.offer.__slots__},
               'total_price': Decimal('19999.0')},
        )
        stub._offers = [cheaper]

        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)
        self.assertContains(response, 'Ціна змінилась')
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(
            self.client.session['booking.selected']['total_price'], '19999.0',
        )

    def test_servio_rejects_the_booking(self):
        self.start(FakeClient(book_error=ServioAPIError('Номер зайнятий')))
        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)
        self.assertContains(response, 'Номер зайнятий')
        self.assertFalse(Booking.objects.exists())

    def test_booking_survives_a_failed_payment(self):
        """Найважливіше: номер у готелі вже зайнятий, тому бронь має
        зберегтися навіть якщо оплата не піднялась."""
        self.start(FakeClient(payment_error=ServioUnavailableError('payment down')))
        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)

        booking = Booking.objects.get()
        self.assertEqual(booking.status, Booking.Status.FAILED)
        self.assertEqual(booking.payment_url, '')
        self.assertIn('payment_error', booking.raw_response)
        self.assertContains(response, 'перейти до оплати не вдалося')

    def test_widget_payment_shows_the_done_page_instead_of_redirecting(self):
        self.start(FakeClient(payment=PaymentResult(
            payment_service_id=2, redirect_url='', requires_widget=True,
            raw_response={'merchantID': 'M', 'signature': 'S'},
        )))
        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)

        booking = Booking.objects.get()
        self.assertEqual(booking.payment_url, '')
        self.assertContains(response, 'платіжному віджеті')
        self.assertContains(response, booking.servio_booking_id)

    def test_missing_reservation_id_is_flagged(self):
        self.start(FakeClient(book=booking_result(reservation_id='')))
        response = self.client.post(reverse('booking:guest'), GUEST_POST, follow=True)

        booking = Booking.objects.get()
        self.assertEqual(booking.status, Booking.Status.FAILED)
        self.assertContains(response, 'не повернув її номер')

    def test_double_submit_does_not_create_a_second_booking(self):
        """У живому готелі другий сабміт — це друга зайнята кімната."""
        stub = self.start()
        self.client.post(reverse('booking:guest'), GUEST_POST)
        self.assertEqual(Booking.objects.count(), 1)

        booking = Booking.objects.get()
        response = self.client.post(reverse('booking:guest'), GUEST_POST)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertRedirects(
            response, reverse('booking:done', args=[booking.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(stub.bookings), 1)

    def test_payment_info_is_fetched_before_make_payment(self):
        stub = self.start()
        self.client.post(reverse('booking:guest'), GUEST_POST)
        self.assertEqual(stub.payments[0]['account'], 'R-777')
        self.assertEqual(stub.payments[0]['services'], [{'total': 16200}])

    def test_payment_info_without_services_does_not_break_the_flow(self):
        self.start(FakeClient(payment_info={'unexpected': True}))
        self.client.post(reverse('booking:guest'), GUEST_POST)
        self.assertEqual(self.stub.payments[0]['services'], [])
        self.assertEqual(Booking.objects.count(), 1)


class DonePageTests(FlowTestCase):
    def _create(self):
        self.use(FakeClient(payment=PaymentResult(
            payment_service_id=2, redirect_url='', requires_widget=True,
        )))
        self.do_search()
        self.pick_first_offer()
        self.client.post(reverse('booking:guest'), GUEST_POST)
        return Booking.objects.get()

    def test_shows_the_booking(self):
        booking = self._create()
        response = self.client.get(reverse('booking:done', args=[booking.pk]))
        self.assertContains(response, booking.servio_booking_id)
        self.assertContains(response, booking.room_name)
        self.assertContains(response, 'Тест Тестенко')

    def test_someone_elses_booking_is_not_shown(self):
        booking = self._create()
        self.client.cookies.clear()  # інша сесія
        response = self.client.get(reverse('booking:done', args=[booking.pk]))
        self.assertRedirects(
            response, reverse('booking:search'), fetch_redirect_response=False,
        )

    def test_flow_state_is_cleared_but_the_page_still_works_on_refresh(self):
        booking = self._create()
        url = reverse('booking:done', args=[booking.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertNotIn('booking.search', self.client.session)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_missing_booking_is_404(self):
        self.assertEqual(
            self.client.get(reverse('booking:done', args=[999999])).status_code, 404,
        )
