from django.urls import path

from . import views


app_name = 'cancer_ordering'
urlpatterns = [
    path('', views.index, name='index'),
    path('collect/', views.collect, name='collect'),
    path('undo/', views.undo, name='undo'),
    path('candidates/<uuid:cancer_candidate_id>/', views.detail, name='detail'),
]
