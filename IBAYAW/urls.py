from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('guest_app.urls')),
    path('tours/', include('tour_app.urls')),
    path('adminapp/', include('admin_app.urls', namespace='admin_app')),
    path('accomapp/', include('accom_app.urls', namespace='accom_app')),
    path('request_app/', include('request_app.urls')),
]

# Add media URL configuration for development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) 