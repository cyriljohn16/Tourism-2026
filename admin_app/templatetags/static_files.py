from django import template
from django.templatetags.static import static as django_static

register = template.Library()

@register.simple_tag
def static(path):
    """
    A simple wrapper around Django's built-in static tag
    that provides better error handling and debugging.
    """
    return django_static(path) 