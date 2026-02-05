from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

urlpatterns = ([
    path('admin/', admin.site.urls),
    path('tour_app/', include('tour_app.urls')),
    path('guest_app/', include('guest_app.urls')),
    path('admin_app/', include('admin_app.urls')),
    path('accom_app/', include('accom_app.urls')),
    path('request_app/', include('request_app.urls')),
     path('', lambda request: redirect('admin_app:login')),  # redirect homepage to login
    ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT))