"""Єдина точка контакту з API HMS Servio.

Контракт описаний у docs/servio-api.md. Три речі, які визначають увесь цей
модуль:

1. **HTTP-статус завжди 200**, навіть на помилках. Помилка — це `isError: true`
   у тілі. Тому ніде не покладаємось на `response.status_code`.
2. **`data: []` — не помилка**, а «немає доступності».
3. **Ідемпотентності немає.** Повторний `/book` створить другу бронь, тому
   retry дозволений тільки на read-only викликах.

Views не повинні знати структуру чужого JSON — назовні віддаємо dataclass'и.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Тип оплати, який надсилає віджет готелю (є серед paidTypes тарифу).
DEFAULT_PAID_TYPE = 200
DEFAULT_CURRENCY = 980  # UAH, ISO-4217 numeric

# paymentServiceID, для яких Servio віддає готове посилання (див.
# docs/servio-api.md): віджет робить document.location.href = data.url
REDIRECT_PAYMENT_SERVICES = frozenset({3, 4, 6, 9, 10, 12, 14, 15, 17, 18})
# Ці вимагають вбудованого віджета або form-post, простим редіректом не обійтись
WIDGET_PAYMENT_SERVICES = frozenset({2, 5, 11})


# --------------------------------------------------------------------------
# Помилки
# --------------------------------------------------------------------------

class ServioError(Exception):
    """Базова помилка інтеграції."""


class ServioAPIError(ServioError):
    """Servio відповів `isError: true`.

    `message` — локалізований текст від Servio, його можна показувати
    користувачу.
    """

    def __init__(self, message: str, *, endpoint: str = '', payload: Any = None):
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.payload = payload


class ServioUnavailableError(ServioError):
    """Мережа, таймаут або 5xx — чужий сервіс недоступний."""


class ServioProtocolError(ServioError):
    """Відповідь не схожа на конверт Servio — контракт змінився."""


# --------------------------------------------------------------------------
# Моделі
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NightPrice:
    date: str
    price: Decimal
    rack_rate_price: Decimal


@dataclass(frozen=True, slots=True)
class RoomOffer:
    """Один варіант «тип номера × тариф» на конкретні дати.

    Несе дати й склад гостей, для яких його знайдено, тому
    `create_booking(offer, guest)` самодостатній.
    """

    # Ідентичність оффера — ця трійка йде в /book незмінною
    room_type_id: int
    contract_condition_id: int
    api_price_list_id: int

    api_room_type_id: int
    name: str
    rate_name: str
    description: str = ''
    images: tuple[str, ...] = ()

    total_price: Decimal = Decimal('0')
    rack_rate_price: Decimal = Decimal('0')
    currency: int = DEFAULT_CURRENCY
    night_prices: tuple[NightPrice, ...] = ()

    free_rooms: int = 0
    rooms_total: int = 0
    main_places: int = 0
    has_bath: bool = False
    has_shower: bool = False

    min_stay: int | None = None
    max_stay: int | None = None
    blocking_restrictions: tuple[str, ...] = ()

    check_in: date | None = None
    check_out: date | None = None
    time_arrival: str = ''
    time_departure: str = ''
    adults: int = 1
    children: int = 0
    children_ages: tuple[int, ...] = ()

    @property
    def offer_key(self) -> str:
        """Стабільний ключ оффера — щоб протягнути вибір через сесію."""
        return f'{self.room_type_id}:{self.contract_condition_id}:{self.api_price_list_id}'

    @property
    def nights(self) -> int:
        if not (self.check_in and self.check_out):
            return 0
        return (self.check_out - self.check_in).days

    @property
    def is_available(self) -> bool:
        if self.free_rooms <= 0 or self.blocking_restrictions:
            return False
        nights = self.nights
        if nights:
            if self.min_stay and nights < self.min_stay:
                return False
            if self.max_stay and nights > self.max_stay:
                return False
        return True

    @property
    def price_per_night(self) -> Decimal:
        nights = self.nights
        if not nights:
            return self.total_price
        return (self.total_price / nights).quantize(Decimal('0.01'))


@dataclass(frozen=True, slots=True)
class Guest:
    full_name: str
    email: str
    phone: str
    country_of_residence: str = 'ua'
    comment: str = ''
    travels_by_car: bool = False
    car_number: str = ''


@dataclass(frozen=True, slots=True)
class ReservedRoom:
    reservation_id: str
    room_type_id: int
    api_room_type_id: int
    contract_condition_id: int
    guest_full_name: str
    adults: int
    children: int


@dataclass(frozen=True, slots=True)
class BookingResult:
    """Результат /book.

    Контракт відповіді відновлений зі статичного розбору бандла і живим
    викликом не перевірений (docs/servio-api.md), тому парсер терпимий до
    відсутніх полів, а `raw_response` зберігається повністю.
    """

    reservation_id: str
    hotel_id: int | None
    total_amount: Decimal
    currency: int
    payment_info: Any = None
    rooms: tuple[ReservedRoom, ...] = ()
    raw_request: dict = field(default_factory=dict)
    raw_response: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Результат /make-payment.

    Єдиного «посилання на оплату» в Servio немає: гілка залежить від
    `payment_service_id`. `redirect_url` заповнений лише для тих сервісів,
    де віджет робить звичайний редірект.
    """

    payment_service_id: int | None
    redirect_url: str = ''
    requires_widget: bool = False
    raw_response: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Парсери
# --------------------------------------------------------------------------

def _dec(value: Any) -> Decimal:
    """Гроші приходять float'ами — переводимо через str, без бінарних артефактів."""
    if value is None:
        return Decimal('0')
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal('0')


def _blocking_restrictions(sale_restrictions: dict) -> tuple[str, ...]:
    """Обмеження, які роблять оффер непродаваним.

    minStay/maxStay сюди не входять — вони залежать від кількості ночей і
    перевіряються в `RoomOffer.is_available`.
    """
    blocking = []
    for key in ('closedToSale', 'closedToArrive', 'earlyReservation'):
        rule = sale_restrictions.get(key) or {}
        if rule.get('hasRestrictions'):
            blocking.append(rule.get('message') or key)
    return tuple(blocking)


def _stay_limit(sale_restrictions: dict, key: str) -> int | None:
    rule = sale_restrictions.get(key) or {}
    if not rule.get('hasRestrictions'):
        return None
    days = rule.get('days')
    return int(days) if days else None


def _images(room_type: dict) -> tuple[str, ...]:
    images = sorted(
        room_type.get('images') or [],
        key=lambda i: i.get('position', 0),
    )
    return tuple(
        img.get('urlResized') or img.get('urlCompressed') or img.get('url', '')
        for img in images
        if img.get('urlResized') or img.get('urlCompressed') or img.get('url')
    )


def parse_room_offers(data: list[dict], criteria: dict) -> list[RoomOffer]:
    """Розкласти відповідь /rooms у плоский список офферів.

    Один тип номера може мати кілька тарифів — кожна пара стає окремим
    оффером, бо саме її вибирає користувач.
    """
    offers: list[RoomOffer] = []
    for room_type in data or []:
        if not room_type.get('isVisible', True):
            continue
        images = _images(room_type)
        for cc in room_type.get('contractConditions') or []:
            if not cc.get('isVisible', True):
                continue
            restrictions = cc.get('saleRestrictions') or {}
            night_prices = tuple(
                NightPrice(
                    date=pd.get('date', ''),
                    price=_dec(pd.get('price')),
                    rack_rate_price=_dec(pd.get('rackRatePrice')),
                )
                for service in cc.get('services') or []
                for pd in service.get('priceDates') or []
            )
            offers.append(RoomOffer(
                room_type_id=room_type.get('id'),
                contract_condition_id=cc.get('id'),
                api_price_list_id=cc.get('apiPriceListID'),
                api_room_type_id=room_type.get('apiID'),
                name=room_type.get('name', ''),
                rate_name=cc.get('name', ''),
                description=room_type.get('description', ''),
                images=images,
                total_price=_dec(cc.get('totalPrice')),
                rack_rate_price=_dec(cc.get('totalRackRatePrice')),
                currency=cc.get('currency') or DEFAULT_CURRENCY,
                night_prices=night_prices,
                free_rooms=room_type.get('freeRoomsCount') or 0,
                rooms_total=room_type.get('roomsCount') or 0,
                main_places=room_type.get('mainPlacesCount') or 0,
                has_bath=bool(room_type.get('hasBath')),
                has_shower=bool(room_type.get('hasShower')),
                min_stay=_stay_limit(restrictions, 'minStay'),
                max_stay=_stay_limit(restrictions, 'maxStay'),
                blocking_restrictions=_blocking_restrictions(restrictions),
                **criteria,
            ))
    offers.sort(key=lambda o: (o.total_price, o.name))
    return offers


def parse_booking_result(data: Any, raw_request: dict, raw_response: dict) -> BookingResult:
    if not isinstance(data, dict):
        raise ServioProtocolError(
            f'/book повернув {type(data).__name__}, очікували обʼєкт'
        )
    rooms = tuple(
        ReservedRoom(
            reservation_id=str(room.get('apiReservationID') or ''),
            room_type_id=room.get('roomTypeID'),
            api_room_type_id=room.get('roomTypeApiID'),
            contract_condition_id=room.get('contractConditionID'),
            guest_full_name=room.get('guestFullName') or '',
            adults=room.get('adults') or 0,
            children=room.get('children') or 0,
        )
        for room in data.get('rooms') or []
    )
    reservation_id = str(data.get('apiReservationID') or '')
    if not reservation_id and rooms:
        reservation_id = rooms[0].reservation_id
    return BookingResult(
        reservation_id=reservation_id,
        hotel_id=data.get('hotelID'),
        total_amount=_dec(data.get('totalAmount')),
        currency=data.get('currency') or DEFAULT_CURRENCY,
        payment_info=data.get('paymentInfo'),
        rooms=rooms,
        raw_request=raw_request,
        raw_response=raw_response,
    )


def parse_payment_result(data: Any, raw_response: dict) -> PaymentResult:
    if not isinstance(data, dict):
        raise ServioProtocolError(
            f'/make-payment повернув {type(data).__name__}, очікували обʼєкт'
        )
    service_id = data.get('paymentServiceID')
    url = data.get('url') or data.get('checkout_url') or ''
    requires_widget = service_id in WIDGET_PAYMENT_SERVICES
    if service_id not in REDIRECT_PAYMENT_SERVICES:
        # Для UPC/Redsys `checkoutURL` — це не сторінка для користувача,
        # а endpoint платіжного віджета. Редіректом його віддавати не можна.
        url = '' if requires_widget else url
    return PaymentResult(
        payment_service_id=service_id,
        redirect_url=url,
        requires_widget=requires_widget,
        raw_response=raw_response,
    )


# --------------------------------------------------------------------------
# Клієнт
# --------------------------------------------------------------------------

class ServioClient:
    """HTTP-клієнт до Servio.

    Усі виклики йдуть з бекенда, не з браузера: ключів у Servio немає, CORS
    відкритий, але через проксі ми маємо логування і не залежимо від чужої
    CORS-політики.

    `http_client` можна підмінити (`httpx.MockTransport`) — так тести
    працюють без мережі.
    """

    #: скільки додаткових спроб робити для read-only викликів
    max_retries = 2
    #: пауза між спробами, секунди
    retry_backoff = 0.5

    def __init__(
        self,
        *,
        base_url: str | None = None,
        company_key: str | None = None,
        hotel_id: int | None = None,
        timeout: float | None = None,
        language: str = 'uk',
        session_id: str | None = None,
        http_client: httpx.Client | None = None,
    ):
        self.base_url = (base_url or settings.SERVIO_API_BASE).rstrip('/')
        self.company_key = company_key or settings.SERVIO_COMPANY_KEY
        self.hotel_id = hotel_id if hotel_id is not None else settings.SERVIO_HOTEL_ID
        self.timeout = timeout if timeout is not None else settings.SERVIO_TIMEOUT
        self.language = language
        # Не токен автентифікації, а кореляційний id сесії. Одне значення на
        # клієнта; можна передати id сесії користувача.
        self.session_id = session_id or uuid.uuid4().hex
        self._client = http_client
        self._owns_client = http_client is None

    # -- інфраструктура ---------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.timeout))
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> ServioClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            'Accept': 'application/json',
            'X-Language': self.language,
            'X-Servio-SessionId': self.session_id,
        }
        if extra:
            headers.update(extra)
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        idempotent: bool,
    ) -> tuple[Any, dict]:
        """Виконати виклик і розібрати конверт Servio.

        Повертає `(data, повне тіло відповіді)`. `idempotent=False` вимикає
        retry — саме так викликаються /book і /make-payment.
        """
        url = f'{self.base_url}/{path.lstrip("/")}'
        attempts = 1 if not idempotent else self.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self.client.request(
                    method, url, json=json, params=params,
                    headers=self._headers(headers),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning(
                    'servio call=%s attempt=%d/%d outcome=transport_error error=%s',
                    path, attempt, attempts, exc.__class__.__name__,
                )
                if attempt < attempts:
                    time.sleep(self.retry_backoff * attempt)
                    continue
                raise ServioUnavailableError(
                    f'Servio недоступний: {exc.__class__.__name__}'
                ) from exc

            elapsed_ms = (time.monotonic() - started) * 1000

            if response.status_code >= 500:
                last_error = ServioUnavailableError(
                    f'Servio відповів {response.status_code}'
                )
                logger.warning(
                    'servio call=%s attempt=%d/%d status=%d outcome=server_error',
                    path, attempt, attempts, response.status_code,
                )
                if attempt < attempts:
                    time.sleep(self.retry_backoff * attempt)
                    continue
                raise last_error

            try:
                body = response.json()
            except ValueError as exc:
                logger.error(
                    'servio call=%s status=%d outcome=not_json',
                    path, response.status_code,
                )
                raise ServioProtocolError(
                    f'{path}: відповідь не JSON (HTTP {response.status_code})'
                ) from exc

            if not isinstance(body, dict) or 'isError' not in body:
                logger.error('servio call=%s outcome=bad_envelope', path)
                raise ServioProtocolError(
                    f'{path}: у відповіді немає поля isError — контракт змінився'
                )

            if body.get('isError'):
                message = body.get('message') or 'Servio повернув помилку'
                logger.warning(
                    'servio call=%s status=%d ms=%.0f outcome=api_error message=%r',
                    path, response.status_code, elapsed_ms, message,
                )
                raise ServioAPIError(message, endpoint=path, payload=json)

            data = body.get('data')
            logger.info(
                'servio call=%s status=%d ms=%.0f outcome=ok items=%s',
                path, response.status_code, elapsed_ms,
                len(data) if isinstance(data, (list, dict)) else '-',
            )
            return data, body

        raise ServioUnavailableError(str(last_error))  # pragma: no cover

    # -- публічний API ----------------------------------------------------

    def search_rooms(
        self,
        check_in: date,
        check_out: date,
        adults: int = 1,
        children: int = 0,
        *,
        children_ages: tuple[int, ...] = (),
        time_arrival: str = '14:00',
        time_departure: str = '12:00',
        promo_code: str | None = None,
    ) -> list[RoomOffer]:
        """Знайти доступні оффери. Порожній список — немає доступності."""
        payload = {
            'companyKey': self.company_key,
            'hotelID': self.hotel_id,
            'promoCode': promo_code,
            'dateArrival': check_in.isoformat(),
            'dateDeparture': check_out.isoformat(),
            'timeArrival': time_arrival,
            'timeDeparture': time_departure,
            'rooms': [{
                'adults': adults,
                'children': children,
                'childrenAges': list(children_ages),
                'index': 0,
            }],
            'currency': DEFAULT_CURRENCY,
        }
        data, _ = self._call('POST', 'rooms', json=payload, idempotent=True)
        if not data:
            return []
        return parse_room_offers(data, {
            'check_in': check_in,
            'check_out': check_out,
            'time_arrival': time_arrival,
            'time_departure': time_departure,
            'adults': adults,
            'children': children,
            'children_ages': tuple(children_ages),
        })

    def build_booking_payload(self, offer: RoomOffer, guest: Guest) -> dict:
        """Payload для /book. Виділено окремо, щоб його можна було зберегти
        в `Booking.raw_request` і перевірити в тестах без мережі."""
        return {
            'companyKey': self.company_key,
            'hotelID': self.hotel_id,
            'promoCode': None,
            'paidType': DEFAULT_PAID_TYPE,
            'currency': offer.currency,

            'contactFullName': guest.full_name,
            'contactEmail': guest.email,
            'contactPhone': guest.phone,
            'contactInfo': '',
            'clientInfo': '',
            'comment': guest.comment,

            'roomNightsToApply': 0,
            'loyaltyMagneticCardID': 0,
            'loyaltyMagneticCardNumber': '',
            'guestTravelsByCar': guest.travels_by_car,
            'carNumber': guest.car_number if guest.travels_by_car else '',

            'rooms': [{
                'roomTypeID': offer.room_type_id,
                'dateArrival': offer.check_in.isoformat(),
                'dateDeparture': offer.check_out.isoformat(),
                'timeArrival': offer.time_arrival,
                'timeDeparture': offer.time_departure,
                'adults': offer.adults,
                'children': offer.children,
                'childrenAges': list(offer.children_ages),
                'apiPriceListID': offer.api_price_list_id,
                'contractConditionID': offer.contract_condition_id,
                'guestFullName': guest.full_name,
                'countryOfResidence': guest.country_of_residence,
            }],

            # Усі agreements.* у налаштуваннях готелю вимкнені → null,
            # як і надсилає віджет.
            'processingPersonalDataConsent': None,
            'hotelRulesConsent': None,
            'publicOfferConsent': None,
            'additionalServices': [],
            'requestConsultation': False,
        }

    def create_booking(self, offer: RoomOffer, guest: Guest) -> BookingResult:
        """Створити бронь.

        **Без retry.** Servio не ідемпотентний: повторна спроба створить
        другу бронь у живому готелі.
        """
        if not (offer.check_in and offer.check_out):
            raise ValueError('offer має нести дати заїзду/виїзду')
        payload = self.build_booking_payload(offer, guest)
        data, body = self._call(
            'POST', 'book', json=payload,
            headers={'UTM-Marks': '{}'},
            idempotent=False,
        )
        result = parse_booking_result(data, payload, body)
        logger.info(
            'servio booking created reservation_id=%s amount=%s currency=%s',
            result.reservation_id, result.total_amount, result.currency,
        )
        return result

    def get_payment_info(
        self, account: str, currency: int = DEFAULT_CURRENCY,
        promo_code: str | None = None,
    ) -> Any:
        """Рахунки й доступні платіжні сервіси по броні.

        Викликається між /book і /make-payment: саме звідси беруться
        `services` і `paymentServices` для платежу.
        """
        params = {
            'companyKey': self.company_key,
            'account': account,
            'currency': currency,
        }
        if promo_code:
            params['promocode'] = promo_code
        data, _ = self._call('GET', 'payment-info', params=params, idempotent=True)
        return data

    def make_payment(
        self,
        *,
        account: str,
        account_name: str,
        email: str,
        phone: str,
        services: list[dict],
        currency: int = DEFAULT_CURRENCY,
        payment_service: int | None = None,
        use_iframe: bool = False,
        promo_code: str | None = None,
    ) -> PaymentResult:
        """Ініціювати оплату. `services` беруться з `get_payment_info`.

        **Без retry** — це теж не ідемпотентний виклик.
        """
        payload = {
            'companyKey': self.company_key,
            'account': account,
            'hotelID': self.hotel_id,
            'currency': currency,
            'accountName': account_name,
            'email': email,
            'phone': phone,
            'services': services,
            'paymentPartsQuantity': None,
            'useIFrame': use_iframe,
            'paymentService': payment_service,
            'promocode': promo_code,
        }
        data, body = self._call(
            'POST', 'make-payment', json=payload,
            headers={'UTM-Marks': '{}'},
            idempotent=False,
        )
        result = parse_payment_result(data, body)
        logger.info(
            'servio payment initiated account=%s service_id=%s redirect=%s widget=%s',
            account, result.payment_service_id, bool(result.redirect_url),
            result.requires_widget,
        )
        return result
