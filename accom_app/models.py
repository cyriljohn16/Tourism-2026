from django.db import models
from admin_app.models import Accomodation, Country, Entry, Region
from guest_app.models import Guest  # Import Guest model from guest_app
import datetime
from datetime import timedelta


class Other_Estab(models.Model):
    estab_id = models.AutoField(primary_key=True)
    accom_id = models.ForeignKey(Accomodation, on_delete=models.CASCADE)
    month = models.CharField(max_length=20)  # Month the document is to be sent
    intended_month = models.CharField(max_length=20, default=datetime.datetime.now().strftime('%B'))  # Default to current month
    date = models.DateField(null=True, blank=True)  # Date the report is created
    region = models.CharField(max_length=50)
    country = models.CharField(max_length=50)
    local = models.CharField(max_length=100, null=True, blank=True)
    residences = models.IntegerField(null=True, blank=True)
    total_foreign_travelers = models.IntegerField(null=True, blank=True)
    overseas = models.IntegerField(null=True, blank=True)
    domestic = models.IntegerField(null=True, blank=True)

    dynamic_fields = models.JSONField(null=True, blank=True, default=dict)

    def __str__(self):
        return f"Establishment {self.estab_id} - {self.region}, {self.country}"



class Summary(models.Model):
    accom_id = models.ForeignKey('admin_app.Accomodation', on_delete=models.CASCADE)
    month_submitted = models.DateField()
    month_actual = models.DateField()
    country_id = models.ForeignKey('admin_app.Country', to_field='id', on_delete=models.CASCADE, null=True, blank=True)
    region_id = models.ForeignKey('admin_app.Region', to_field='id', on_delete=models.CASCADE, null=True, blank=True)
    overall_total = models.IntegerField(null=True, blank=True)
    entry_ans = models.TextField(null=True, blank=True)
    guest_num = models.IntegerField(null=True, blank=True)
    sub_total = models.IntegerField(null=True, blank=True)
    entry_id = models.ForeignKey('admin_app.Entry', to_field='id', on_delete=models.CASCADE, null=True, blank=True)
    hotel = models.CharField(max_length=3, null=True, blank=True)  # New field to record "yes" if from hotel account

    def __str__(self):
        return f"Summary {self.accom_id} - {self.month_submitted}"

  # Assuming the 'accom_id' references the Accom model from admin_app

class Room(models.Model):
    """Model to store room information for accommodations"""
    ROOM_STATUS_CHOICES = [
        ('AVAILABLE', 'Available'),
        ('OCCUPIED', 'Occupied'),
        ('MAINTENANCE', 'Maintenance')
    ]

    room_id = models.AutoField(primary_key=True)
    accom_id = models.ForeignKey('admin_app.Accomodation', on_delete=models.CASCADE, related_name='accom_rooms')
    room_name = models.CharField(max_length=100, null=True, blank=True)
    person_limit = models.IntegerField(default=0)  # Maximum number of people allowed in the room
    current_availability = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=ROOM_STATUS_CHOICES, default='AVAILABLE')
    last_check_in = models.DateTimeField(null=True, blank=True)
    last_check_out = models.DateTimeField(null=True, blank=True)
    total_occupied_time = models.DurationField(default=timedelta(0))
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    def save(self, *args, **kwargs):
        # Set current_availability to person_limit if not specified
        if self.current_availability is None:
            self.current_availability = self.person_limit
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Room {self.room_id} ({self.room_name}) for Accomodation {self.accom_id}"

# This is just to maintain database compatibility during migration
class HotelRooms(Room):
    class Meta:
        proxy = True

class RoomsGuestAdd(models.Model):
    room_id = models.ForeignKey(Room, on_delete=models.CASCADE)
    accom_id = models.ForeignKey('admin_app.Accomodation', on_delete=models.CASCADE)
    checked_in = models.DateField()
    checked_out = models.DateField()
    no_of_nights = models.IntegerField()
    month = models.CharField(max_length=20)
    num_guests = models.IntegerField(default=1)  # New field for number of guests

    def __str__(self):
        return f"RoomsGuestAdd for Room {self.room_id.room_id} - Accomodation {self.accom_id}"

class RoomAssignment(models.Model):
    """Model to track guest assignments to rooms"""
    assignment_id = models.AutoField(primary_key=True)
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='assignments')
    guest = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='accom_room_assignments')
    is_owner = models.BooleanField(default=False)
    checked_in = models.DateTimeField(blank=True, null=True)
    checked_out = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Assignment: {self.guest} in {self.room} ({'Owner' if self.is_owner else 'Member'})"

class Accommodation(models.Model):
    company_name = models.CharField(max_length=255)
    # ... other fields ...

    def __str__(self):
        return self.company_name

class mies_table(models.Model):
    event_id = models.AutoField(primary_key=True)
    accom_id = models.ForeignKey(Accomodation, on_delete=models.CASCADE)
    time_start = models.DateTimeField(null=True, blank=True)
    time_end = models.DateTimeField(null=True, blank=True)
    company = models.CharField(max_length=255)
    representative = models.CharField(max_length=255)
    subtotal = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    grandtotal = models.IntegerField(default=0)
    event_name = models.CharField(max_length=255)
    meeting_place = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return f"Event {self.event_id} - {self.event_name} for {self.accom_id.company_name}"


