from django.urls import path
from . import views

app_name = 'cloud_imaging'
urlpatterns = [
    path('records/<uuid:document_id>/cloud-imaging/', views.document, name='document'),
    path('cloud-imaging/<uuid:source_id>/', views.source, name='source'),
]
