from django.urls import path
from .views import openai_chat

urlpatterns = [
    path("chat/", openai_chat, name="openai_chat"),
]
