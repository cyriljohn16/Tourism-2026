from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.views.generic import RedirectView
from django.http import HttpResponse
from admin_app import views as admin_views

urlpatterns = ([
    path('admin/', admin.site.urls),
    path('tour_app/', include('tour_app.urls')),
    path('guest_app/', include('guest_app.urls')),
    path('owner/accommodations/register/', admin_views.accommodation_register, name='owner_accommodation_register'),
    path('admin_app/', include('admin_app.urls')),
    path('accom_app/', include('accom_app.urls')),
    path('request_app/', include('request_app.urls')),
    path("api/", include("ai_chatbot.urls")),
    path("accounts/", include("allauth.urls")),
    path("favicon.ico", lambda request: HttpResponse(status=204)),
     path('', lambda request: redirect('admin_app:login')),  # redirect homepage to login
    ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT))
