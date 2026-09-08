"""Чотири кроки бронювання.

Крок 1–3 живуть у сесії, у БД пишемо тільки на кроці 4 — і тільки після того,
як Servio прийняв бронь. Порядок кроку 4:

    /rooms (перевірка доступності) → /book → /payment-info → /make-payment

Бронь зберігається в БД **до** спроби оплати: якщо оплата зірветься, номер у
готелі вже зайнятий, і слід про це мусить залишитись.
"""

import logging

from django.contrib import messages
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from . import session
from .forms import DEFAULT_CHECK_IN, DEFAULT_CHECK_OUT, GuestForm, SearchForm
from .models import Booking
from .services.servio import (
    Guest,
    ServioAPIError,
    ServioClient,
    ServioError,
    ServioProtocolError,
    ServioUnavailableError,
)

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = (
    'Сервіс бронювання готелю зараз недоступний. Спробуйте, будь ласка, '
    'за кілька хвилин.'
)
PROTOCOL_MESSAGE = (
    'Сервіс бронювання відповів у невідомому форматі. Ми вже дивимось, '
    'що сталося.'
)


def make_client(request):
    """Клієнт Servio для поточної сесії.

    `X-Servio-SessionId` беремо від сесії Django, щоб виклики одного
    користувача корелювались на стороні Servio.
    """
    if not request.session.session_key:
        request.session.save()
    return ServioClient(session_id=request.session.session_key)


def _report(request, exc):
    """Показати користувачу зрозумілу причину, у лог — деталі."""
    if isinstance(exc, ServioAPIError):
        messages.error(request, exc.message)
    elif isinstance(exc, ServioUnavailableError):
        messages.error(request, UNAVAILABLE_MESSAGE)
    elif isinstance(exc, ServioProtocolError):
        logger.error('servio protocol error: %s', exc)
        messages.error(request, PROTOCOL_MESSAGE)
    else:  # pragma: no cover - на випадок нового підтипу ServioError
        logger.exception('unexpected servio error')
        messages.error(request, UNAVAILABLE_MESSAGE)


# --------------------------------------------------------------------------
# Крок 1 — дати й гості
# --------------------------------------------------------------------------

def search(request):
    if request.method == 'POST':
        form = SearchForm(request.POST)
        if form.is_valid():
            session.save_search(request, form.cleaned_data)
            return redirect('booking:rooms')
    else:
        current = session.get_search(request)
        form = SearchForm(initial=current or {
            'check_in': DEFAULT_CHECK_IN,
            'check_out': DEFAULT_CHECK_OUT,
            'adults': 2,
            'children': 0,
        })
    return render(request, 'booking/search.html', {'form': form, 'step': 1})


# --------------------------------------------------------------------------
# Крок 2 — вибір номера
# --------------------------------------------------------------------------

def rooms(request):
    criteria = session.get_search(request)
    if not criteria:
        return redirect('booking:search')

    if request.method == 'POST':
        chosen = session.select_offer(request, request.POST.get('offer_key', ''))
        if chosen is None:
            messages.error(request, 'Цей номер уже неактуальний, виберіть інший.')
            return redirect('booking:rooms')
        return redirect('booking:guest')

    offers = []
    try:
        with make_client(request) as client:
            offers = client.search_rooms(**criteria)
    except ServioError as exc:
        _report(request, exc)

    session.save_offers(request, offers)
    available = [offer for offer in offers if offer.is_available]
    return render(request, 'booking/rooms.html', {
        'step': 2,
        'criteria': criteria,
        'nights': (criteria['check_out'] - criteria['check_in']).days,
        'offers': available,
        'unavailable': [offer for offer in offers if not offer.is_available],
    })


# --------------------------------------------------------------------------
# Крок 3 — дані гостя, і крок 4 на POST
# --------------------------------------------------------------------------

def guest(request):
    criteria = session.get_search(request)
    selected = session.get_selected(request)
    if not criteria:
        return redirect('booking:search')
    if not selected:
        return redirect('booking:rooms')

    if request.method == 'POST':
        form = GuestForm(request.POST)
        if form.is_valid():
            session.save_guest(request, form.cleaned_data)
            return _create_booking(request, criteria, selected, form.cleaned_data)
    else:
        form = GuestForm(initial=session.get_guest(request) or {})

    return render(request, 'booking/guest.html', {
        'step': 3,
        'form': form,
        'criteria': criteria,
        'selected': selected,
    })


# --------------------------------------------------------------------------
# Крок 4 — створення броні та перехід до оплати
# --------------------------------------------------------------------------

def _create_booking(request, criteria, selected, guest_data):
    # Захист від подвійного сабміту: у живому готелі це була б друга бронь.
    existing_pk = session.created_pk(request)
    if existing_pk and Booking.objects.filter(pk=existing_pk).exists():
        logger.warning('duplicate submit blocked for booking pk=%s', existing_pk)
        return redirect('booking:done', pk=existing_pk)

    try:
        with make_client(request) as client:
            offer, fallback = _revalidate(request, client, criteria, selected)
            if offer is None:
                return redirect(fallback)

            result = client.create_booking(offer, Guest(
                full_name=guest_data['full_name'],
                email=guest_data['email'],
                phone=guest_data['phone'],
                comment=guest_data.get('comment') or '',
                travels_by_car=bool(guest_data.get('travels_by_car')),
                car_number=guest_data.get('car_number') or '',
            ))

            booking = _store(offer, guest_data, result)
            session.mark_created(request, booking.pk)
            logger.info(
                'booking stored pk=%s servio_id=%s', booking.pk,
                booking.servio_booking_id,
            )

            redirect_url = _initiate_payment(request, client, booking, result)

        if redirect_url:
            # Через власний ендпоінт, а не прямо на Servio: так перевірка
            # дедлайну лишається єдиною точкою контролю.
            return redirect('booking:pay', pk=booking.pk)
        return redirect('booking:done', pk=booking.pk)

    except ServioError as exc:
        _report(request, exc)
        return redirect('booking:guest')


def _revalidate(request, client, criteria, selected):
    """Перезапитати доступність перед створенням броні.

    Між кроком 2 і кроком 4 номер могли розібрати або змінити ціну — питаємо
    Servio заново і працюємо тільки з актуальним оффером.

    Повертає `(offer, куди_редіректити_якщо_None)`.
    """
    offers = client.search_rooms(**criteria)
    offer = next(
        (o for o in offers if o.offer_key == selected['offer_key']), None,
    )
    if offer is None or not offer.is_available:
        messages.error(
            request,
            f'Номер «{selected["name"]}» уже недоступний на ці дати. '
            f'Виберіть, будь ласка, інший.',
        )
        return None, 'booking:rooms'

    if offer.total_price != session.selected_price(request):
        session.set_selected(request, offer)
        messages.warning(
            request,
            f'Ціна змінилась: тепер {offer.total_price} '
            f'{offer.currency_code} за весь період. Перевірте й підтвердіть ще раз.',
        )
        return None, 'booking:guest'

    return offer, ''


def _store(offer, guest_data, result):
    """Записати бронь. Статус — `pending`: чи оплачено, ми за ТЗ не моніторимо."""
    return Booking.objects.create(
        check_in=offer.check_in,
        check_out=offer.check_out,
        time_arrival=offer.time_arrival,
        time_departure=offer.time_departure,
        adults=offer.adults,
        children=offer.children,
        children_ages=list(offer.children_ages),
        room_type_id=offer.room_type_id,
        contract_condition_id=offer.contract_condition_id,
        api_price_list_id=offer.api_price_list_id,
        room_name=offer.name,
        price=result.total_amount or offer.total_price,
        currency=offer.currency,
        guest_name=guest_data['full_name'],
        guest_email=guest_data['email'],
        guest_phone=guest_data['phone'],
        comment=guest_data.get('comment') or '',
        servio_booking_id=result.reservation_id,
        status=Booking.Status.PENDING,
        raw_request=result.raw_request,
        raw_response=result.raw_response,
    )


def _payment_services(payment_info):
    """`services` з `paymentInfo` — рахунки, які треба оплатити."""
    if isinstance(payment_info, dict):
        services = payment_info.get('services')
        if isinstance(services, list):
            return services
    logger.warning(
        'paymentInfo без services (тип %s) — надсилаю порожній список',
        type(payment_info).__name__,
    )
    return []


def _payment_service_id(payment_info):
    """Який платіжний сервіс обрати.

    Готель віддає перелік у `paymentInfo.paymentServices`; у Riverwood там
    один елемент. Віджет так само надсилає конкретний сервіс, а не `null`.
    """
    if isinstance(payment_info, dict):
        services = payment_info.get('paymentServices')
        if isinstance(services, list) and services:
            first = services[0]
            if isinstance(first, dict):
                return first.get('paymentService')
    logger.warning('paymentInfo без paymentServices — надсилаю null')
    return None


def _initiate_payment(request, client, booking, result):
    """Ініціювати оплату. Повертає URL для редіректу або порожній рядок.

    Помилка тут не скасовує бронь — вона вже створена в готелі. Тому все
    загорнуто в try: користувач має побачити свою бронь навіть якщо оплата
    не піднялась.
    """
    if not result.reservation_id:
        logger.error('booking pk=%s без reservation_id — оплату не ініціюю', booking.pk)
        booking.status = Booking.Status.FAILED
        booking.save(update_fields=['status', 'updated_at'])
        messages.error(
            request,
            'Бронь створена, але Servio не повернув її номер. Готель '
            'зв\'яжеться з вами для оплати.',
        )
        return ''

    try:
        # /book уже віддає paymentInfo — зайвий раз API не питаємо.
        # Окремий GET лишається запасним варіантом, якщо готель колись
        # перестане вкладати paymentInfo у відповідь.
        payment_info = result.payment_info
        if not _payment_services(payment_info):
            payment_info = client.get_payment_info(
                result.reservation_id, currency=booking.currency,
            )

        payment = client.make_payment(
            account=result.reservation_id,
            account_name=booking.guest_name,
            email=booking.guest_email,
            phone=booking.guest_phone,
            services=_payment_services(payment_info),
            payment_service=_payment_service_id(payment_info),
            use_iframe=bool(payment_info.get('useIFrame'))
            if isinstance(payment_info, dict) else False,
            currency=booking.currency,
        )
    except ServioError as exc:
        logger.warning('payment init failed for booking pk=%s: %s', booking.pk, exc)
        booking.status = Booking.Status.FAILED
        booking.raw_response = {
            'book': booking.raw_response,
            'payment_error': str(exc),
        }
        booking.save(update_fields=['status', 'raw_response', 'updated_at'])
        messages.warning(
            request,
            'Бронь створена, але перейти до оплати не вдалося. Готель '
            'надішле посилання на оплату окремо.',
        )
        return ''

    booking.payment_url = payment.redirect_url
    booking.raw_response = {
        'book': booking.raw_response,
        'payment': payment.raw_response,
    }
    booking.save(update_fields=['payment_url', 'raw_response', 'updated_at'])

    if payment.requires_widget:
        # UPC/Redsys не редіректяться: там потрібен вбудований віджет із
        # підписом мерчанта. Показуємо сторінку броні з даними платежу.
        messages.info(
            request,
            'Оплата цього готелю проходить у платіжному віджеті — дані нижче.',
        )
        return ''

    return payment.redirect_url


def healthz(request):
    """Health-check для контейнера і для деплою.

    Перевіряє БД навмисно: застосунок, який піднявся, але не бачить Postgres,
    для нас не «живий». Servio тут не чіпаємо — недоступність чужого API не
    має валити наш деплой.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except DatabaseError as exc:
        logger.error('healthz: database unreachable: %s', exc)
        return JsonResponse({'status': 'error', 'database': 'unreachable'}, status=503)
    return JsonResponse({'status': 'ok', 'database': 'ok'})


def _own_booking_or_none(request, pk):
    """Бронь доступна лише тому, хто її створив у цій сесії."""
    booking = get_object_or_404(Booking, pk=pk)
    if session.created_pk(request) != booking.pk:
        return None
    # Ліниве протермінування: сторінка має показувати правду навіть якщо
    # планувальник саме зараз не працює.
    if booking.expire():
        logger.info('booking pk=%s протермінована при відкритті', booking.pk)
    return booking


def done(request, pk):
    booking = _own_booking_or_none(request, pk)
    if booking is None:
        messages.error(request, 'Ця бронь не з цієї сесії.')
        return redirect('booking:search')
    session.clear_flow(request)
    return render(request, 'booking/done.html', {'step': 4, 'booking': booking})


def pay(request, pk):
    """Перехід до оплати — через нас, а не прямим посиланням на Servio.

    Сам URL платежу відкликати ми не можемо (він живе на боці Servio), тому
    єдине, що в наших руках, — не віддавати його після дедлайну.
    """
    booking = _own_booking_or_none(request, pk)
    if booking is None:
        messages.error(request, 'Ця бронь не з цієї сесії.')
        return redirect('booking:search')

    if not booking.is_payable:
        if booking.status == Booking.Status.EXPIRED:
            messages.error(
                request,
                'Час на оплату вийшов — бронь відхилена. Щоб забронювати '
                'знову, почніть спочатку.',
            )
        elif not booking.payment_url:
            messages.error(request, 'Для цієї броні немає посилання на оплату.')
        else:
            messages.error(request, 'Ця бронь уже не очікує оплати.')
        return redirect('booking:done', pk=booking.pk)

    logger.info('booking pk=%s → перехід до оплати', booking.pk)
    return redirect(booking.payment_url)
