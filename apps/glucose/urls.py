from django.urls import path

from . import views

app_name = 'glucose'
urlpatterns = [
    path('', views.index, name='index'),
    path('new/', views.create, name='create'),
    path('sources/', views.sources, name='sources'),
    path('import/labs/<uuid:observation_id>/', views.import_lab, name='import_lab'),
    path('import/nursing/<uuid:document_id>/<int:page_number>/', views.import_nursing, name='import_nursing'),
    path('<uuid:glucose_record_id>/', views.detail, name='detail'),
    path('<uuid:glucose_record_id>/edit/', views.edit, name='edit'),
    path('<uuid:glucose_record_id>/delete/', views.delete, name='delete'),
    path('<uuid:glucose_record_id>/undo/', views.undo, name='undo'),
    path('<uuid:glucose_record_id>/recheck/', views.recheck, name='recheck'),
]
