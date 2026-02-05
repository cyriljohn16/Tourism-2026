from django.contrib import admin
from .models import Tour_Add, Tour_Schedule
from .translation_models import TourAddTranslation

# Check if the model is already registered to avoid re-registering it
if not admin.site.is_registered(Tour_Add):
    class TourAddAdmin(admin.ModelAdmin):
        # Fields to be displayed in the admin list view
        list_display = ('tour_id', 'tour_name', 'description', 'get_tour_start_time', 'get_tour_end_time', 'get_price')

        # Method to get the start time of the first tour schedule
        def get_tour_start_time(self, obj):
            schedule = Tour_Schedule.objects.filter(tour=obj).first()  # Assuming 'tour' is the ForeignKey in Tour_Schedule
            return schedule.start_time.strftime('%Y-%m-%d %H:%M:%S') if schedule else 'No start time'
        get_tour_start_time.short_description = 'Tour Start Time'

        # Method to get the end time of the first tour schedule
        def get_tour_end_time(self, obj):
            schedule = Tour_Schedule.objects.filter(tour=obj).first()  # Assuming 'tour' is the ForeignKey in Tour_Schedule
            return schedule.end_time.strftime('%Y-%m-%d %H:%M:%S') if schedule else 'No end time'
        get_tour_end_time.short_description = 'Tour End Time'

        # Method to get the price of the first tour schedule
        def get_price(self, obj):
            schedule = Tour_Schedule.objects.filter(tour=obj).first()  # Assuming 'tour' is the ForeignKey in Tour_Schedule
            return schedule.price if schedule else 'No price available'
        get_price.short_description = 'Price'

    # Register the custom admin class for Tour_Add model
    admin.site.register(Tour_Add, TourAddAdmin)

    # Create a custom admin class for Tour_Schedule to include the new fields
    class TourScheduleAdmin(admin.ModelAdmin):
        # Fields to display in the admin list view for Tour_Schedule
        list_display = ('sched_id', 'tour_id', 'start_time', 'end_time', 'price', 'slots_available', 'slots_booked', 'slots_remaining')

        # Make slots_available and slots_booked editable in the list view
        list_editable = ('slots_available', 'slots_booked')

        # Readonly field for slots_remaining since it should be auto-calculated
        readonly_fields = ('slots_remaining',)

        # Method to calculate the number of remaining slots
        def slots_remaining(self, obj):
            return obj.slots_available - obj.slots_booked if obj.slots_available is not None and obj.slots_booked is not None else 0
        slots_remaining.short_description = 'Slots Remaining'

        # Optional: You can add search fields or filters if necessary
        search_fields = ('sched_id', 'tour_id__tour_name')
        list_filter = ('tour_id',)

    # Register the custom admin class for Tour_Schedule
    admin.site.register(Tour_Schedule, TourScheduleAdmin)

# Register translation models
class TourAddTranslationAdmin(admin.ModelAdmin):
    list_display = ('tour', 'language', 'tour_name')
    list_filter = ('language',)
    search_fields = ('tour__tour_name', 'tour_name')

admin.site.register(TourAddTranslation, TourAddTranslationAdmin)