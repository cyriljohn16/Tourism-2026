from . import views
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static

app_name = 'accom_app'

urlpatterns = [
    path('other-estab/create/', views.other_estab_create, name='other_estab_create'),
    path('other-estab-create-pt2/', views.other_estab_create_pt2, name='other_estab_create_pt2'),
    path('register_room/', views.register_room, name='register_room'),
    path('add_room_ajax/', views.add_room_ajax, name='add_room_ajax'),
    path('register_room_guest_ajax/', views.register_guest_to_room, name='register_room_guest_ajax'),
    path('delete-room-ajax/', views.delete_room_ajax, name='delete_room_ajax'),
]    
