from django import template
import hashlib

register = template.Library()

@register.filter
def hash_color(value):
    """
    Generate a consistent color based on a string value by hashing it.
    This ensures the same input always gives the same color.
    """
    if not value:
        return "#875a34"  # Default brown color
        
    # Generate a hash from the string
    hash_object = hashlib.md5(str(value).encode())
    hash_hex = hash_object.hexdigest()
    
    # Use the first 6 characters of the hash as a hex color
    # Pre-defined colors for better aesthetics
    colors = [
        "#4285F4",  # Blue
        "#EA4335",  # Red
        "#FBBC05",  # Yellow
        "#34A853",  # Green
        "#875a34",  # Brown
        "#8E44AD",  # Purple
        "#16A085",  # Teal
        "#D35400",  # Orange
        "#2C3E50",  # Navy
        "#E74C3C",  # Crimson
    ]
    
    # Use the hash to select from our predefined colors
    index = int(hash_hex, 16) % len(colors)
    return colors[index] 