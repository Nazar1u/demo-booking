"""Стан кроків бронювання — у сесії, не в БД.

До останнього кроку в базі нічого не з'являється: інакше вона засмічується
покинутими чернетками (docs/PLAN.md, Фаза 4).
"""

import datetime as dt
from decimal import Decimal

SEARCH_KEY = 'booking.search'
OFFERS_KEY = 'booking.offers'
SELECTED_KEY = 'booking.selected'
GUEST_KEY = 'booking.guest'
CREATED_KEY = 'booking.created_pk'

ALL_KEYS = (SEARCH_KEY, OFFERS_KEY, SELECTED_KEY, GUEST_KEY)


def save_search(request, cleaned):
    request.session[SEARCH_KEY] = {
        'check_in': cleaned['check_in'].isoformat(),
        'check_out': cleaned['check_out'].isoformat(),
        'adults': cleaned['adults'],
        'children': cleaned['children'],
        'children_ages': list(cleaned['children_ages']),
    }
    # Новий пошук скидає все, що вибрано далі.
    for key in (OFFERS_KEY, SELECTED_KEY, CREATED_KEY):
        request.session.pop(key, None)


def get_search(request):
    raw = request.session.get(SEARCH_KEY)
    if not raw:
        return None
    try:
        return {
            'check_in': dt.date.fromisoformat(raw['check_in']),
            'check_out': dt.date.fromisoformat(raw['check_out']),
            'adults': int(raw['adults']),
            'children': int(raw['children']),
            'children_ages': tuple(raw.get('children_ages') or ()),
        }
    except (KeyError, TypeError, ValueError):
        # Формат сесії змінився між релізами — краще почати спочатку.
        request.session.pop(SEARCH_KEY, None)
        return None


def offer_to_dict(offer):
    """Мінімум для відображення. Повний оффер не зберігаємо: на кроці 4 його
    все одно перезапитуємо, щоб перевірити доступність і ціну."""
    return {
        'offer_key': offer.offer_key,
        'room_type_id': offer.room_type_id,
        'contract_condition_id': offer.contract_condition_id,
        'api_price_list_id': offer.api_price_list_id,
        'name': offer.name,
        'rate_name': offer.rate_name,
        'total_price': str(offer.total_price),
        'price_per_night': str(offer.price_per_night),
        'currency': offer.currency,
        'currency_code': offer.currency_code,
        'free_rooms': offer.free_rooms,
        'image': offer.images[0] if offer.images else '',
        'nights': offer.nights,
    }


def save_offers(request, offers):
    request.session[OFFERS_KEY] = {
        offer.offer_key: offer_to_dict(offer) for offer in offers
    }


def select_offer(request, offer_key):
    """Запам'ятати вибір. Повертає dict оффера або None, якщо ключ чужий."""
    offers = request.session.get(OFFERS_KEY) or {}
    chosen = offers.get(offer_key)
    if chosen is None:
        return None
    request.session[SELECTED_KEY] = chosen
    return chosen


def set_selected(request, offer):
    """Перезаписати вибраний оффер свіжими даними з API."""
    chosen = offer_to_dict(offer)
    request.session[SELECTED_KEY] = chosen
    return chosen


def get_selected(request):
    return request.session.get(SELECTED_KEY)


def selected_price(request):
    chosen = get_selected(request)
    return Decimal(chosen['total_price']) if chosen else None


def save_guest(request, cleaned):
    request.session[GUEST_KEY] = {
        'full_name': cleaned['full_name'],
        'email': cleaned['email'],
        'phone': cleaned['phone'],
        'comment': cleaned.get('comment') or '',
        'travels_by_car': bool(cleaned.get('travels_by_car')),
        'car_number': cleaned.get('car_number') or '',
    }


def get_guest(request):
    return request.session.get(GUEST_KEY)


def mark_created(request, pk):
    request.session[CREATED_KEY] = pk


def created_pk(request):
    return request.session.get(CREATED_KEY)


def clear_flow(request):
    for key in ALL_KEYS:
        request.session.pop(key, None)
