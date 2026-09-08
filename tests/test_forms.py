"""Юніт-тести форм — без HTTP і без БД.

Межі беруться з налаштувань готелю (`hotels.json`): 10 гостей на номер,
вік дитини 0–12, email і телефон обов'язкові.
"""

import datetime as dt

from django.test import SimpleTestCase

from booking.forms import GUEST_LIMIT, MAX_NIGHTS, GuestForm, SearchForm

VALID_SEARCH = {
    'check_in': '2027-03-10',
    'check_out': '2027-03-12',
    'adults': 2,
    'children': 0,
    'children_ages': '',
}
VALID_GUEST = {
    'full_name': 'Тест Тестенко',
    'email': 'test@example.com',
    'phone': '+380 44 123 45 67',
}


def search(**overrides):
    return SearchForm(VALID_SEARCH | overrides)


def guest(**overrides):
    return GuestForm(VALID_GUEST | overrides)


class SearchFormTests(SimpleTestCase):
    def test_valid(self):
        form = search()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['check_in'], dt.date(2027, 3, 10))
        self.assertEqual(form.cleaned_data['children_ages'], [])

    def test_iso_dates_are_accepted_regardless_of_locale(self):
        """Локаль uk не має ISO серед DATE_INPUT_FORMATS, а
        `<input type=date>` надсилає тільки ISO."""
        self.assertTrue(search().is_valid())

    def test_departure_must_be_after_arrival(self):
        form = search(check_out='2027-03-10')
        self.assertFalse(form.is_valid())
        self.assertIn('пізніше заїзду', str(form.errors['check_out']))

    def test_stay_longer_than_the_cap_is_rejected(self):
        form = search(check_in='2027-03-01', check_out='2027-04-05')
        self.assertFalse(form.is_valid())
        self.assertIn(str(MAX_NIGHTS), str(form.errors['check_out']))

    def test_dates_outside_march_2027_are_rejected(self):
        for check_in, check_out in [
            ('2027-02-25', '2027-02-27'),
            ('2027-04-01', '2027-04-03'),
            ('2028-03-10', '2028-03-12'),
        ]:
            with self.subTest(check_in=check_in):
                form = search(check_in=check_in, check_out=check_out)
                self.assertFalse(form.is_valid())
                self.assertIn('березень 2027', str(form.errors['check_in']))

    def test_past_dates_are_rejected(self):
        form = search(check_in='2020-03-10', check_out='2020-03-12')
        self.assertFalse(form.is_valid())
        # спрацьовує і «в минулому», і «тільки березень 2027»
        self.assertTrue(form.errors['check_in'])

    def test_at_least_one_adult(self):
        self.assertFalse(search(adults=0).is_valid())

    def test_guest_limit_counts_children_too(self):
        form = search(adults=GUEST_LIMIT - 1, children=2, children_ages='3, 5')
        self.assertFalse(form.is_valid())
        self.assertIn(str(GUEST_LIMIT), str(form.errors['adults']))

    def test_children_without_ages(self):
        form = search(children=1)
        self.assertFalse(form.is_valid())
        self.assertIn('вік кожної дитини', str(form.errors['children_ages']))

    def test_ages_count_must_match_children(self):
        form = search(children=3, children_ages='4, 9')
        self.assertFalse(form.is_valid())
        self.assertIn('а дітей — 3', str(form.errors['children_ages']))

    def test_ages_accept_several_separators(self):
        for raw in ['4, 9', '4;9', '4 9', ' 4 , 9 ']:
            with self.subTest(raw=raw):
                form = search(children=2, children_ages=raw)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['children_ages'], [4, 9])

    def test_non_numeric_age(self):
        form = search(children=1, children_ages='малий')
        self.assertFalse(form.is_valid())
        self.assertIn('це не вік', str(form.errors['children_ages']))

    def test_age_above_the_hotel_limit(self):
        form = search(children=1, children_ages='13')
        self.assertFalse(form.is_valid())
        self.assertIn('дорослих', str(form.errors['children_ages']))

    def test_zero_year_old_is_allowed(self):
        form = search(children=1, children_ages='0')
        self.assertTrue(form.is_valid(), form.errors)

    def test_ages_are_ignored_when_there_are_no_children(self):
        form = search(children=0, children_ages='4, 9')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['children_ages'], [])

    def test_blank_children_becomes_zero(self):
        data = VALID_SEARCH | {'children': ''}
        form = SearchForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['children'], 0)


class GuestFormTests(SimpleTestCase):
    def test_valid(self):
        form = guest()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['full_name'], 'Тест Тестенко')

    def test_email_and_phone_are_required(self):
        for field in ('email', 'phone', 'full_name'):
            with self.subTest(field=field):
                form = guest(**{field: ''})
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_full_name_needs_two_words(self):
        form = guest(full_name='Тест')
        self.assertFalse(form.is_valid())
        self.assertIn('прізвище', str(form.errors['full_name']))

    def test_full_name_is_trimmed(self):
        form = guest(full_name='  Тест Тестенко  ')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['full_name'], 'Тест Тестенко')

    def test_accepted_phone_formats(self):
        for phone in ['+380441234567', '+380 44 123 45 67',
                      '044 123-45-67', '(044) 123 45 67']:
            with self.subTest(phone=phone):
                self.assertTrue(guest(phone=phone).is_valid())

    def test_rejected_phone_formats(self):
        for phone in ['нема', '12345', 'abc-def-ghij', '+']:
            with self.subTest(phone=phone):
                form = guest(phone=phone)
                self.assertFalse(form.is_valid())
                self.assertIn('phone', form.errors)

    def test_invalid_email(self):
        self.assertFalse(guest(email='not-an-email').is_valid())

    def test_car_number_required_when_travelling_by_car(self):
        form = guest(travels_by_car=True, car_number='   ')
        self.assertFalse(form.is_valid())
        self.assertIn('номер авто', str(form.errors['car_number']))

    def test_car_number_optional_without_a_car(self):
        self.assertTrue(guest(travels_by_car=False, car_number='').is_valid())

    def test_comment_is_optional(self):
        form = guest()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['comment'], '')
