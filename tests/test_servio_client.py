"""Тести клієнта Servio — на записаних фікстурах, без мережі.

Фікстури в tests/fixtures/servio/ — це справжні відповіді, знятні з живого
API 2026-09-08 (див. docs/servio-api.md). Транспорт підмінений
`httpx.MockTransport`, тож жоден тест нікуди не стукає.

Написано на `SimpleTestCase`, щоб працювало і через `manage.py test`, і
через pytest у Фазі 5 без переписування.
"""

import datetime as dt
import json
import pathlib
from decimal import Decimal

import httpx
from django.test import SimpleTestCase

from booking.services.servio import (
    Guest,
    RoomOffer,
    ServioAPIError,
    ServioClient,
    ServioProtocolError,
    ServioUnavailableError,
    parse_booking_result,
    parse_payment_result,
    parse_room_offers,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'servio'

BASE_URL = 'https://servio.test/hms/api'
COMPANY_KEY = 'TEST-COMPANY-KEY'
HOTEL_ID = 161

CHECK_IN = dt.date(2027, 3, 10)
CHECK_OUT = dt.date(2027, 3, 12)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def make_client(handler, **kwargs):
    """Клієнт з підміненим транспортом і без пауз між спробами."""
    client = ServioClient(
        base_url=BASE_URL,
        company_key=COMPANY_KEY,
        hotel_id=HOTEL_ID,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )
    client.retry_backoff = 0
    return client


def envelope(data, *, is_error=False, message='Операція пройшла успішно'):
    return httpx.Response(
        200, json={'isError': is_error, 'message': message, 'data': data},
    )


SEARCH_CRITERIA = {
    'check_in': CHECK_IN,
    'check_out': CHECK_OUT,
    'time_arrival': '14:00',
    'time_departure': '12:00',
    'adults': 2,
    'children': 0,
    'children_ages': (),
}


class ParseRoomOffersTests(SimpleTestCase):
    """Розбір справжньої відповіді /rooms на 10–12 березня 2027."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data = fixture('rooms-response.json')['data']
        cls.offers = parse_room_offers(cls.data, SEARCH_CRITERIA)

    def test_every_room_type_becomes_an_offer(self):
        # У Riverwood по одному тарифу на тип номера → 11 офферів.
        self.assertEqual(len(self.offers), 11)
        self.assertEqual(
            len(self.offers),
            sum(len(rt['contractConditions']) for rt in self.data),
        )

    def test_offer_identity_is_the_triple_servio_needs(self):
        offer = next(o for o in self.offers if o.room_type_id == 1373)
        self.assertEqual(offer.contract_condition_id, 107)
        self.assertEqual(offer.api_price_list_id, 2)
        self.assertEqual(offer.api_room_type_id, 2)
        self.assertEqual(offer.offer_key, '1373:107:2')

    def test_prices_are_decimal_not_float(self):
        offer = next(o for o in self.offers if o.room_type_id == 1373)
        self.assertIsInstance(offer.total_price, Decimal)
        self.assertEqual(offer.total_price, Decimal('16200.0'))
        self.assertEqual(offer.rack_rate_price, Decimal('16200.0'))
        self.assertEqual(offer.currency, 980)

    def test_night_prices_are_broken_down_per_date(self):
        offer = next(o for o in self.offers if o.room_type_id == 1373)
        self.assertEqual(len(offer.night_prices), 2)
        self.assertEqual(offer.night_prices[0].price, Decimal('8100.0'))
        self.assertEqual(offer.night_prices[0].date, '2027-03-10T23:59:00')
        self.assertEqual(offer.price_per_night, Decimal('8100.00'))

    def test_carries_search_criteria(self):
        offer = self.offers[0]
        self.assertEqual(offer.check_in, CHECK_IN)
        self.assertEqual(offer.check_out, CHECK_OUT)
        self.assertEqual(offer.nights, 2)
        self.assertEqual(offer.adults, 2)

    def test_availability_and_images(self):
        offer = next(o for o in self.offers if o.room_type_id == 1373)
        self.assertEqual(offer.free_rooms, 9)
        self.assertEqual(offer.rooms_total, 9)
        self.assertTrue(offer.is_available)
        self.assertTrue(offer.images)
        self.assertTrue(all(url.startswith('http') for url in offer.images))

    def test_sorted_by_price(self):
        prices = [o.total_price for o in self.offers]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(self.offers[-1].total_price, Decimal('47200.0'))

    def test_no_blocking_restrictions_in_the_fixture(self):
        self.assertEqual(
            [o.blocking_restrictions for o in self.offers if o.blocking_restrictions],
            [],
        )


class RestrictionTests(SimpleTestCase):
    """saleRestrictions мають прибирати оффер з продажу."""

    def _one_room_type(self, restrictions, free_rooms=5):
        return [{
            'id': 1, 'apiID': 1, 'name': 'X', 'isVisible': True,
            'freeRoomsCount': free_rooms, 'roomsCount': 5,
            'contractConditions': [{
                'id': 10, 'apiPriceListID': 2, 'name': 'Тариф',
                'totalPrice': 100.0, 'currency': 980, 'isVisible': True,
                'saleRestrictions': restrictions,
            }],
        }]

    def test_closed_to_sale_blocks_the_offer(self):
        data = self._one_room_type({
            'closedToSale': {'hasRestrictions': True, 'message': 'Продаж закритий'},
        })
        offer = parse_room_offers(data, SEARCH_CRITERIA)[0]
        self.assertEqual(offer.blocking_restrictions, ('Продаж закритий',))
        self.assertFalse(offer.is_available)

    def test_closed_to_arrive_blocks_the_offer(self):
        data = self._one_room_type({
            'closedToArrive': {'hasRestrictions': True, 'message': 'Заїзд закритий'},
        })
        self.assertFalse(parse_room_offers(data, SEARCH_CRITERIA)[0].is_available)

    def test_min_stay_longer_than_the_request_blocks_the_offer(self):
        data = self._one_room_type({
            'minStay': {'hasRestrictions': True, 'days': 5, 'message': 'Мінімум 5 ночей'},
        })
        offer = parse_room_offers(data, SEARCH_CRITERIA)[0]
        self.assertEqual(offer.min_stay, 5)
        self.assertEqual(offer.blocking_restrictions, ())  # не блокує саме по собі
        self.assertFalse(offer.is_available)  # але 2 ночі < 5

    def test_max_stay_shorter_than_the_request_blocks_the_offer(self):
        data = self._one_room_type({
            'maxStay': {'hasRestrictions': True, 'days': 1, 'message': 'Максимум 1 ніч'},
        })
        self.assertFalse(parse_room_offers(data, SEARCH_CRITERIA)[0].is_available)

    def test_inactive_restriction_is_ignored(self):
        data = self._one_room_type({
            'minStay': {'hasRestrictions': False, 'days': 5, 'message': ''},
            'closedToSale': {'hasRestrictions': False, 'message': ''},
        })
        offer = parse_room_offers(data, SEARCH_CRITERIA)[0]
        self.assertIsNone(offer.min_stay)
        self.assertTrue(offer.is_available)

    def test_no_free_rooms_means_unavailable(self):
        data = self._one_room_type({}, free_rooms=0)
        self.assertFalse(parse_room_offers(data, SEARCH_CRITERIA)[0].is_available)

    def test_hidden_room_types_and_rates_are_skipped(self):
        data = self._one_room_type({})
        data[0]['isVisible'] = False
        self.assertEqual(parse_room_offers(data, SEARCH_CRITERIA), [])


class SearchRoomsTests(SimpleTestCase):
    def test_request_payload_matches_the_recorded_one(self):
        """Регресія на контракт: те, що ми надсилаємо, має збігатися з тим,
        що надсилає справжній віджет (rooms-request.json)."""
        expected = fixture('rooms-request.json')
        sent = {}

        def handler(request):
            sent.update(json.loads(request.content))
            return envelope([])

        client = make_client(handler)
        client.search_rooms(CHECK_IN, CHECK_OUT, adults=2)

        # companyKey/hotelID у фікстурі — справжні, у тесті підставні
        expected['companyKey'] = COMPANY_KEY
        self.assertEqual(sent, expected)

    def test_required_headers_are_sent(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return envelope([])

        client = make_client(handler, language='en')
        client.search_rooms(CHECK_IN, CHECK_OUT)

        self.assertEqual(seen['x-language'], 'en')
        self.assertEqual(len(seen['x-servio-sessionid']), 32)
        self.assertEqual(seen['accept'], 'application/json')

    def test_real_fixture_end_to_end(self):
        def handler(request):
            return httpx.Response(200, json=fixture('rooms-response.json'))

        offers = make_client(handler).search_rooms(CHECK_IN, CHECK_OUT, adults=2)
        self.assertEqual(len(offers), 11)
        self.assertTrue(all(isinstance(o, RoomOffer) for o in offers))

    def test_empty_availability_is_not_an_error(self):
        def handler(request):
            return httpx.Response(200, json=fixture('rooms-empty-no-availability.json'))

        self.assertEqual(make_client(handler).search_rooms(CHECK_IN, CHECK_OUT), [])

    def test_null_data_is_not_an_error(self):
        def handler(request):
            return envelope(None)

        self.assertEqual(make_client(handler).search_rooms(CHECK_IN, CHECK_OUT), [])

    def test_children_ages_are_passed_through(self):
        sent = {}

        def handler(request):
            sent.update(json.loads(request.content))
            return envelope([])

        make_client(handler).search_rooms(
            CHECK_IN, CHECK_OUT, adults=2, children=2, children_ages=(4, 9),
        )
        self.assertEqual(sent['rooms'][0]['children'], 2)
        self.assertEqual(sent['rooms'][0]['childrenAges'], [4, 9])


class ErrorEnvelopeTests(SimpleTestCase):
    """HTTP завжди 200 — помилка сидить у тілі."""

    def test_invalid_date_raises_api_error_with_servio_message(self):
        def handler(request):
            return httpx.Response(200, json=fixture('rooms-error-invalid-date.json'))

        with self.assertRaises(ServioAPIError) as ctx:
            make_client(handler).search_rooms(CHECK_OUT, CHECK_IN)
        self.assertEqual(ctx.exception.message, 'Невірна дата')
        self.assertEqual(ctx.exception.endpoint, 'rooms')

    def test_unknown_company_raises_api_error(self):
        def handler(request):
            return httpx.Response(
                200, json=fixture('rooms-error-company-not-found.json'),
            )

        with self.assertRaises(ServioAPIError) as ctx:
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)
        self.assertEqual(ctx.exception.message, 'Компанія не знайдена')

    def test_api_error_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return envelope(None, is_error=True, message='Невірна дата')

        with self.assertRaises(ServioAPIError):
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)
        self.assertEqual(len(calls), 1)

    def test_response_without_is_error_is_a_protocol_error(self):
        def handler(request):
            return httpx.Response(200, json={'rooms': []})

        with self.assertRaises(ServioProtocolError):
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)

    def test_non_json_response_is_a_protocol_error(self):
        def handler(request):
            return httpx.Response(200, text='<html>502 from the CDN</html>')

        with self.assertRaises(ServioProtocolError):
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)


class RetryTests(SimpleTestCase):
    def test_transport_error_is_retried_and_can_succeed(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError('boom', request=request)
            return envelope([])

        self.assertEqual(make_client(handler).search_rooms(CHECK_IN, CHECK_OUT), [])
        self.assertEqual(len(calls), 3)

    def test_transport_error_gives_up_after_max_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout('slow', request=request)

        with self.assertRaises(ServioUnavailableError):
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)
        self.assertEqual(len(calls), 3)  # 1 + max_retries

    def test_server_error_is_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, text='unavailable')

        with self.assertRaises(ServioUnavailableError):
            make_client(handler).search_rooms(CHECK_IN, CHECK_OUT)
        self.assertEqual(len(calls), 3)


class BookingPayloadTests(SimpleTestCase):
    def _offer(self):
        return parse_room_offers(
            fixture('rooms-response.json')['data'], SEARCH_CRITERIA,
        )[0]

    def _guest(self):
        return Guest(
            full_name='Тест Тестенко',
            email='test@example.com',
            phone='+380000000000',
            comment='без коментарів',
        )

    def test_payload_shape_matches_the_documented_contract(self):
        client = make_client(lambda r: envelope({}))
        payload = client.build_booking_payload(self._offer(), self._guest())

        self.assertEqual(payload['companyKey'], COMPANY_KEY)
        self.assertEqual(payload['hotelID'], HOTEL_ID)
        self.assertEqual(payload['paidType'], 200)
        self.assertEqual(payload['currency'], 980)
        self.assertEqual(payload['contactFullName'], 'Тест Тестенко')
        self.assertEqual(payload['contactEmail'], 'test@example.com')
        self.assertIsNone(payload['processingPersonalDataConsent'])
        self.assertEqual(payload['additionalServices'], [])

        room = payload['rooms'][0]
        self.assertEqual(room['dateArrival'], '2027-03-10')
        self.assertEqual(room['dateDeparture'], '2027-03-12')
        self.assertEqual(room['timeArrival'], '14:00')
        self.assertEqual(room['adults'], 2)
        self.assertEqual(room['countryOfResidence'], 'ua')
        # трійка, без якої Servio відхилить бронь
        self.assertEqual(room['roomTypeID'], 1373)
        self.assertEqual(room['contractConditionID'], 107)
        self.assertEqual(room['apiPriceListID'], 2)

    def test_car_number_only_travels_when_the_guest_has_a_car(self):
        client = make_client(lambda r: envelope({}))
        guest = Guest(full_name='X', email='x@example.com', phone='1',
                      travels_by_car=False, car_number='AA1234BB')
        self.assertEqual(
            client.build_booking_payload(self._offer(), guest)['carNumber'], '',
        )

        guest = Guest(full_name='X', email='x@example.com', phone='1',
                      travels_by_car=True, car_number='AA1234BB')
        payload = client.build_booking_payload(self._offer(), guest)
        self.assertEqual(payload['carNumber'], 'AA1234BB')
        self.assertTrue(payload['guestTravelsByCar'])

    def test_create_booking_is_never_retried(self):
        """Найважливіший тест модуля: Servio не ідемпотентний, повторна
        спроба створила б другу бронь у живому готелі."""
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError('boom', request=request)

        with self.assertRaises(ServioUnavailableError):
            make_client(handler).create_booking(self._offer(), self._guest())
        self.assertEqual(len(calls), 1)

    def test_create_booking_sends_utm_header(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return envelope({'apiReservationID': 'R-1'})

        make_client(handler).create_booking(self._offer(), self._guest())
        self.assertEqual(seen['utm-marks'], '{}')

    def test_create_booking_requires_dates_on_the_offer(self):
        offer = RoomOffer(
            room_type_id=1, contract_condition_id=1, api_price_list_id=1,
            api_room_type_id=1, name='X', rate_name='Y',
        )
        with self.assertRaises(ValueError):
            make_client(lambda r: envelope({})).create_booking(offer, self._guest())

    def test_raw_payloads_are_kept_on_the_result(self):
        response_body = {
            'isError': False, 'message': 'ok',
            'data': {'apiReservationID': 'R-42', 'hotelID': HOTEL_ID,
                     'totalAmount': 16200.0, 'currency': 980},
        }

        def handler(request):
            return httpx.Response(200, json=response_body)

        result = make_client(handler).create_booking(self._offer(), self._guest())
        self.assertEqual(result.reservation_id, 'R-42')
        self.assertEqual(result.total_amount, Decimal('16200.0'))
        self.assertEqual(result.raw_response, response_body)
        self.assertEqual(result.raw_request['rooms'][0]['roomTypeID'], 1373)


class ParseBookingResultTests(SimpleTestCase):
    def test_parses_rooms_and_totals(self):
        data = {
            'apiReservationID': 'R-7', 'hotelID': 161,
            'totalAmount': 20200.5, 'currency': 980,
            'paymentInfo': {'promocode': None},
            'rooms': [{
                'apiReservationID': 'R-7', 'roomTypeID': 1377,
                'roomTypeApiID': 6, 'contractConditionID': 107,
                'guestFullName': 'Гість', 'adults': 2, 'children': 1,
            }],
        }
        result = parse_booking_result(data, {'a': 1}, {'data': data})
        self.assertEqual(result.reservation_id, 'R-7')
        self.assertEqual(result.total_amount, Decimal('20200.5'))
        self.assertEqual(len(result.rooms), 1)
        self.assertEqual(result.rooms[0].room_type_id, 1377)
        self.assertEqual(result.rooms[0].children, 1)

    def test_falls_back_to_the_room_reservation_id(self):
        data = {'rooms': [{'apiReservationID': 'R-9'}]}
        self.assertEqual(parse_booking_result(data, {}, {}).reservation_id, 'R-9')

    def test_non_object_data_is_a_protocol_error(self):
        with self.assertRaises(ServioProtocolError):
            parse_booking_result([], {}, {})


class ParsePaymentResultTests(SimpleTestCase):
    """Одного «посилання на оплату» не існує — гілка залежить від сервісу."""

    def test_redirect_service_gives_a_url(self):
        result = parse_payment_result(
            {'paymentServiceID': 3, 'url': 'https://pay.example/abc'}, {},
        )
        self.assertEqual(result.redirect_url, 'https://pay.example/abc')
        self.assertFalse(result.requires_widget)

    def test_monobank_gives_a_url(self):
        result = parse_payment_result(
            {'paymentServiceID': 14, 'url': 'https://pay.mono/abc'}, {},
        )
        self.assertEqual(result.redirect_url, 'https://pay.mono/abc')

    def test_upc_needs_a_widget_and_gives_no_redirect(self):
        result = parse_payment_result({
            'paymentServiceID': 2, 'merchantID': 'M', 'terminalID': 'T',
            'signature': 'S', 'checkoutURL': 'https://upc.example/api',
        }, {})
        self.assertTrue(result.requires_widget)
        self.assertEqual(result.redirect_url, '')

    def test_redsys_needs_a_widget(self):
        result = parse_payment_result({'paymentServiceID': 11}, {})
        self.assertTrue(result.requires_widget)

    def test_checkout_url_variant(self):
        result = parse_payment_result(
            {'paymentServiceID': 16, 'checkout_url': 'https://pay.example/x'}, {},
        )
        self.assertEqual(result.redirect_url, 'https://pay.example/x')

    def test_raw_response_is_preserved(self):
        body = {'isError': False, 'data': {'paymentServiceID': 3, 'url': 'u'}}
        self.assertEqual(parse_payment_result(body['data'], body).raw_response, body)


class MakePaymentTests(SimpleTestCase):
    def test_payload_shape(self):
        sent = {}

        def handler(request):
            sent.update(json.loads(request.content))
            return envelope({'paymentServiceID': 3, 'url': 'https://pay/1'})

        result = make_client(handler).make_payment(
            account='R-1', account_name='Гість', email='g@example.com',
            phone='+380', services=[{'customerAccount': 'R-1', 'total': 100}],
            payment_service=3, use_iframe=True,
        )
        self.assertEqual(sent['account'], 'R-1')
        self.assertEqual(sent['hotelID'], HOTEL_ID)
        self.assertEqual(sent['currency'], 980)
        self.assertTrue(sent['useIFrame'])
        self.assertEqual(sent['paymentService'], 3)
        self.assertIsNone(sent['paymentPartsQuantity'])
        self.assertEqual(len(sent['services']), 1)
        self.assertEqual(result.redirect_url, 'https://pay/1')

    def test_make_payment_is_never_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError('boom', request=request)

        with self.assertRaises(ServioUnavailableError):
            make_client(handler).make_payment(
                account='R-1', account_name='X', email='x@example.com',
                phone='1', services=[],
            )
        self.assertEqual(len(calls), 1)


class PaymentInfoTests(SimpleTestCase):
    def test_query_params(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return envelope({'paymentServices': []})

        make_client(handler).get_payment_info('R-1', promo_code='SALE')
        self.assertEqual(seen['companyKey'], COMPANY_KEY)
        self.assertEqual(seen['account'], 'R-1')
        self.assertEqual(seen['currency'], '980')
        self.assertEqual(seen['promocode'], 'SALE')

    def test_bogus_account_raises_api_error(self):
        def handler(request):
            return envelope(
                None, is_error=True,
                message='Виникла помилка при отриманні рахунків',
            )

        with self.assertRaises(ServioAPIError):
            make_client(handler).get_payment_info('0')
