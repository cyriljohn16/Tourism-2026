from django.db import models
from django.utils.translation import gettext_lazy as _
from .models import Tour_Add

# Translation models for Tour_Add
class TourAddTranslation(models.Model):
    """
    Model to store translations for Tour_Add entries
    """
    LANGUAGE_CHOICES = [
        ('en', _('English')),
        ('tl', _('Tagalog')),
        ('ceb', _('Cebuano')),
        ('es', _('Spanish')),
    ]
    
    tour = models.ForeignKey(Tour_Add, on_delete=models.CASCADE, related_name='translations')
    language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES)
    tour_name = models.CharField(max_length=200)
    description = models.TextField()
    
    class Meta:
        unique_together = ('tour', 'language')
        verbose_name = _('Tour Translation')
        verbose_name_plural = _('Tour Translations')
    
    def __str__(self):
        return f"{self.tour.tour_name} ({self.get_language_display()})" 