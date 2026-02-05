from django.contrib import admin
from django.db import models
from .models import Guest, Pending, TourBooking, MapBookmark, BookmarkImage, Friendship

# Check if the model is already registered to avoid re-registering
def register_if_not_registered(model):
    try:
        admin.site.register(model)
    except admin.sites.AlreadyRegistered:
        pass

class BookmarkImageInline(admin.TabularInline):
    model = BookmarkImage
    extra = 1

class MapBookmarkAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'user', 'created_at')
    list_filter = ('category', 'created_at')
    search_fields = ('name', 'details')
    inlines = [BookmarkImageInline]

class BookmarkImageAdmin(admin.ModelAdmin):
    list_display = ('bookmark', 'title', 'upload_date')
    list_filter = ('upload_date',)
    search_fields = ('title', 'description', 'bookmark__name')

# Define admin class for Friendship model
class FriendshipAdmin(admin.ModelAdmin):
    list_display = ('user', 'friend', 'group_name', 'created_at')
    list_filter = ('group_name', 'created_at')
    search_fields = ('user__first_name', 'user__last_name', 'friend__first_name', 'friend__last_name', 'group_name')
    date_hierarchy = 'created_at'
    
    def get_queryset(self, request):
        # Show only one direction of friendship to avoid duplicates
        qs = super().get_queryset(request)
        return qs.filter(user__guest_id__lt=models.F('friend__guest_id'))

# Register models with their admin classes
try:
    admin.site.register(MapBookmark, MapBookmarkAdmin)
except admin.sites.AlreadyRegistered:
    pass

try:
    admin.site.register(BookmarkImage, BookmarkImageAdmin)
except admin.sites.AlreadyRegistered:
    pass

# Register Friendship model
try:
    admin.site.register(Friendship, FriendshipAdmin)
except admin.sites.AlreadyRegistered:
    pass

# Register other models
register_if_not_registered(Guest)
register_if_not_registered(Pending)
register_if_not_registered(TourBooking)
