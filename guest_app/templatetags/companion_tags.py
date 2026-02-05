from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """
    Allow dictionary access with variable keys in Django templates.
    Usage: {{ mydict|get_item:variable_key }}
    """
    if dictionary is None:
        return None
        
    try:
        return dictionary.get(str(key))
    except (AttributeError, KeyError, TypeError):
        return None 