"""Форми кроків бронювання.

Межі взяті з налаштувань готелю (`tests/fixtures/servio/hotels.json`):
`guestLimit: 10`, `minChildAge: 0`, `maxChildAge: 12`, email і телефон
обов'язкові, час заїзду/виїзду — 14:00/12:00.
"""

import datetime as dt
import re

from django import forms
from django.utils import timezone

GUEST_LIMIT = 10
MIN_CHILD_AGE = 0
MAX_CHILD_AGE = 12
MAX_NIGHTS = 30

# За умовами тестового завдання бронювати можна тільки на березень 2027,
# щоб не займати реальні номери готелю на найближчі дати.
TEST_WINDOW_START = dt.date(2027, 3, 1)
TEST_WINDOW_END = dt.date(2027, 3, 31)
DEFAULT_CHECK_IN = dt.date(2027, 3, 10)
DEFAULT_CHECK_OUT = dt.date(2027, 3, 12)

# `<input type="date">` завжди надсилає ISO, а локаль uk такого формату серед
# DATE_INPUT_FORMATS не має — тому формат задано явно.
ISO_DATE = '%Y-%m-%d'

PHONE_RE = re.compile(r'^\+?[\d\s().-]{9,20}$')


class DateInput(forms.DateInput):
    input_type = 'date'


class SearchForm(forms.Form):
    """Крок 1 — дати й склад гостей."""

    check_in = forms.DateField(
        label='Заїзд', input_formats=[ISO_DATE],
        widget=DateInput(format=ISO_DATE),
    )
    check_out = forms.DateField(
        label='Виїзд', input_formats=[ISO_DATE],
        widget=DateInput(format=ISO_DATE),
    )
    adults = forms.IntegerField(
        label='Дорослих', min_value=1, max_value=GUEST_LIMIT, initial=2,
    )
    children = forms.IntegerField(
        label='Дітей', min_value=0, max_value=GUEST_LIMIT - 1,
        initial=0, required=False,
    )
    children_ages = forms.CharField(
        label='Вік дітей', required=False,
        help_text=f'Через кому, від {MIN_CHILD_AGE} до {MAX_CHILD_AGE} років',
        widget=forms.TextInput(attrs={'placeholder': '4, 9'}),
    )

    def clean_children(self):
        return self.cleaned_data.get('children') or 0

    def clean(self):
        cleaned = super().clean()
        check_in = cleaned.get('check_in')
        check_out = cleaned.get('check_out')
        adults = cleaned.get('adults') or 0
        children = cleaned.get('children') or 0

        if check_in and check_out:
            if check_out <= check_in:
                self.add_error('check_out', 'Виїзд має бути пізніше заїзду.')
            elif (check_out - check_in).days > MAX_NIGHTS:
                self.add_error(
                    'check_out', f'Максимум {MAX_NIGHTS} ночей за одну бронь.',
                )

        if check_in and check_in < timezone.localdate():
            self.add_error('check_in', 'Дата заїзду вже в минулому.')

        if check_in and not (TEST_WINDOW_START <= check_in <= TEST_WINDOW_END):
            self.add_error(
                'check_in',
                'Демо-сайт бронює тільки на березень 2027 — щоб не займати '
                'реальні номери готелю на найближчі дати.',
            )

        if adults + children > GUEST_LIMIT:
            self.add_error(
                'adults', f'Максимум {GUEST_LIMIT} гостей на номер.',
            )

        cleaned['children_ages'] = self._clean_ages(
            cleaned.get('children_ages') or '', children,
        )
        return cleaned

    def _clean_ages(self, raw, children):
        if not children:
            return []
        if not raw.strip():
            self.add_error(
                'children_ages',
                'Вкажіть вік кожної дитини — Servio вимагає це для розрахунку.',
            )
            return []

        ages = []
        for chunk in re.split(r'[,;\s]+', raw.strip()):
            if not chunk:
                continue
            try:
                ages.append(int(chunk))
            except ValueError:
                self.add_error('children_ages', f'«{chunk}» — це не вік.')
                return []

        if len(ages) != children:
            self.add_error(
                'children_ages',
                f'Вказано {len(ages)} вік(и), а дітей — {children}.',
            )
            return []

        if any(not (MIN_CHILD_AGE <= age <= MAX_CHILD_AGE) for age in ages):
            self.add_error(
                'children_ages',
                f'Вік дитини — від {MIN_CHILD_AGE} до {MAX_CHILD_AGE} років. '
                f'Старших оформлюйте як дорослих.',
            )
            return []

        return ages


class GuestForm(forms.Form):
    """Крок 3 — дані гостя. Email і телефон обов'язкові за налаштуваннями готелю."""

    full_name = forms.CharField(label="Ім'я та прізвище", max_length=255)
    email = forms.EmailField(label='Email')
    phone = forms.CharField(label='Телефон', max_length=32)
    comment = forms.CharField(
        label='Коментар для готелю', required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    travels_by_car = forms.BooleanField(label='Приїду автомобілем', required=False)
    car_number = forms.CharField(label='Номер авто', max_length=32, required=False)

    def clean_full_name(self):
        name = self.cleaned_data['full_name'].strip()
        if len(name.split()) < 2:
            raise forms.ValidationError("Вкажіть ім'я та прізвище.")
        return name

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if not PHONE_RE.match(phone):
            raise forms.ValidationError(
                'Схоже, це не телефон. Приклад: +380 44 123 45 67.'
            )
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('travels_by_car') and not (cleaned.get('car_number') or '').strip():
            self.add_error('car_number', 'Вкажіть номер авто або приберіть галочку.')
        return cleaned
