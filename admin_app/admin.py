from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Region, Country, Entry, EstablishmentForm

admin.site.register(Region)
admin.site.register(Country)
admin.site.register(Entry)
admin.site.register(EstablishmentForm)


