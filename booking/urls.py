from django.urls import path

from . import views

app_name = 'booking'

urlpatterns = [
    path('', views.search, name='search'),
    path('rooms/', views.rooms, name='rooms'),
    path('guest/', views.guest, name='guest'),
    path('done/<int:pk>/', views.done, name='done'),
    path('healthz', views.healthz, name='healthz'),
]
