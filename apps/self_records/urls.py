from django.urls import path

from . import views


app_name = 'self_records'
urlpatterns = [
    path('', views.index, name='index'),
    path('new/', views.create, name='create'),
    path('<uuid:record_id>/', views.detail, name='detail'),
    path('<uuid:record_id>/edit/', views.edit, name='edit'),
    path('<uuid:record_id>/delete/', views.delete, name='delete'),
    path('<uuid:record_id>/undo/', views.undo, name='undo'),
]
