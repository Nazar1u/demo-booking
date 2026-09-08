import json

from django.contrib import admin
from django.utils.html import format_html

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    """Read-only журнал броней.

    Броні створює тільки сайт, а редагувати їх тут нічого не дасть — у HMS
    зміни не поїдуть. Тому додавання/видалення/редагування вимкнені, а
    детальна сторінка відкривається у режимі перегляду.
    """

    list_display = (
        'created_at',
        'guest_name',
        'room_name',
        'stay',
        'guests_display',
        'total',
        'status',
        'pay_link',
    )
    list_filter = ('status', 'check_in', 'created_at', 'room_name')
    search_fields = (
        'guest_name', 'guest_email', 'guest_phone', 'servio_booking_id',
    )
    date_hierarchy = 'created_at'
    list_per_page = 50

    fieldsets = (
        ('Проживання', {
            'fields': (
                ('check_in', 'check_out'), ('time_arrival', 'time_departure'),
                ('adults', 'children'), 'children_ages',
            ),
        }),
        ('Номер і тариф', {
            'fields': (
                'room_name', 'total',
                ('room_type_id', 'contract_condition_id', 'api_price_list_id'),
            ),
        }),
        ('Гість', {
            'fields': (
                'guest_name', 'guest_email', 'guest_phone',
                'country_of_residence', 'comment',
            ),
        }),
        ('Servio', {
            'fields': ('status', 'servio_booking_id', 'pay_link', 'deadline'),
        }),
        ('Сирі payload’и', {
            'classes': ('collapse',),
            'description': 'Як є, для розбору інцидентів з чужим API.',
            'fields': ('raw_request_pretty', 'raw_response_pretty'),
        }),
        ('Службове', {
            'fields': (('created_at', 'updated_at'),),
        }),
    )

    @admin.display(description='Період', ordering='check_in')
    def stay(self, obj):
        return f'{obj.check_in:%d.%m.%Y} → {obj.check_out:%d.%m.%Y} ({obj.nights} н.)'

    @admin.display(description='Гостей')
    def guests_display(self, obj):
        if obj.children:
            return f'{obj.adults} + {obj.children} діт.'
        return str(obj.adults)

    @admin.display(description='Сума', ordering='price')
    def total(self, obj):
        return f'{obj.price:,.2f} {obj.currency_code}'.replace(',', ' ')

    @admin.display(description='Оплата')
    def pay_link(self, obj):
        if not obj.payment_url:
            return '—'
        if not obj.is_payable:
            # Посилання лишається в записі для розбору, але клікати його
            # після дедлайну сенсу немає.
            return format_html('<span title="{}">неактуальне</span>',
                               obj.payment_url)
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">Перейти</a>',
            obj.payment_url,
        )

    @admin.display(description='Дедлайн оплати')
    def deadline(self, obj):
        if obj.status != Booking.Status.PENDING:
            return '—'
        left = obj.seconds_left
        return f'{obj.payment_deadline:%H:%M} (лишилось {left // 60} хв)'

    @admin.display(description='raw_request')
    def raw_request_pretty(self, obj):
        return self._pretty(obj.raw_request)

    @admin.display(description='raw_response')
    def raw_response_pretty(self, obj):
        return self._pretty(obj.raw_response)

    @staticmethod
    def _pretty(value):
        if not value:
            return '—'
        return format_html(
            '<pre style="max-height:24em;overflow:auto;margin:0">{}</pre>',
            json.dumps(value, ensure_ascii=False, indent=2),
        )

    def get_readonly_fields(self, request, obj=None):
        own = {'stay', 'guests_display', 'total', 'pay_link', 'deadline',
               'raw_request_pretty', 'raw_response_pretty'}
        return [f.name for f in self.model._meta.fields] + sorted(own)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # False + наявний view-доступ = детальна сторінка без кнопки «Зберегти».
        return False

    def has_delete_permission(self, request, obj=None):
        return False
