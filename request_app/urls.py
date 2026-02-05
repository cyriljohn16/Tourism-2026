from django.urls import path
from . import views
from django.conf.urls.static import static
from django.conf import settings

app_name = 'request_app'

urlpatterns = [
    path('main_admin/', views.main_admin, name='main_admin'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
