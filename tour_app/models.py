from django.utils import timezone
from django.db import models
from django.db import transaction


class Tour_Add(models.Model):
    tour_id = models.CharField(max_length=7, primary_key=True, unique=True)
    tour_name = models.CharField(max_length=200)
    description = models.TextField()
    image = models.ImageField(upload_to='tour_images/', null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.tour_id:
            with transaction.atomic():
                last_tour = Tour_Add.objects.order_by('-tour_id').first()
                if last_tour and last_tour.tour_id.isdigit():
                    next_id = int(last_tour.tour_id) + 1
                else:
                    next_id = 1
                self.tour_id = f'{next_id:05d}'

        super().save(*args, **kwargs)

    def __str__(self):
        return self.tour_name


class Tour_Schedule(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    sched_id = models.CharField(max_length=10, primary_key=True, unique=True, blank=True)
    tour = models.ForeignKey('Tour_Add', on_delete=models.CASCADE, related_name='schedules')  # ✅ Fix field name
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    slots_available = models.PositiveIntegerField(default=0)  # Total available slots
    slots_booked = models.PositiveIntegerField(default=0)  # Slots already booked
    duration_days = models.PositiveIntegerField(default=1)  # New field to store duration in days
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    cancellation_reason = models.TextField(null=True, blank=True)
    cancellation_date = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.sched_id:
            last_schedule = Tour_Schedule.objects.order_by('-sched_id').first()
            if last_schedule and last_schedule.sched_id.startswith("Sched"):
                last_id = int(last_schedule.sched_id[5:])
                new_id = f"Sched{last_id + 1:05d}"
            else:
                new_id = "Sched00001"
            self.sched_id = new_id

        # Calculate duration_days if not explicitly set
        if not self.duration_days and self.start_time and self.end_time:
            from datetime import timedelta
            delta = self.end_time - self.start_time
            self.duration_days = max(1, delta.days + (1 if delta.seconds > 0 else 0))
            
        # Auto-update status based on time
        now = timezone.now()
        if self.status != 'cancelled':  # Don't auto-update if cancelled
            if now > self.end_time:
                self.status = 'completed'
            elif now >= self.start_time and now <= self.end_time:
                self.status = 'active'

        print(f"📝 Generating sched_id: {self.sched_id}")  # Debugging
        super().save(*args, **kwargs)

    def slots_remaining(self):
        """Method to calculate remaining slots"""
        return self.slots_available - self.slots_booked

    def cancel_tour(self, reason=None):
        """Cancel this tour schedule"""
        self.status = 'cancelled'
        self.cancellation_reason = reason
        self.cancellation_date = timezone.now()
        self.save()
        
    def calculate_revenue(self):
        """Calculate revenue for this tour schedule based on bookings"""
        # Price per person * number of booked slots
        return self.price * self.slots_booked
        
    def __str__(self):
        return f"{self.tour_id} - {self.sched_id} ({self.start_time.strftime('%Y-%m-%d')}) - {self.slots_remaining()} slots left"
        
    @classmethod
    def get_tour_statistics(cls, period=None, custom_start=None, custom_end=None):
        """
        Get statistics for dashboard display
        
        Args:
            period (str, optional): 'weekly', 'monthly', or 'yearly' for filtering by date
            custom_start (datetime, optional): Custom start date for filtering
            custom_end (datetime, optional): Custom end date for filtering
        """
        from django.db.models import Sum, Count, F
        from django.utils import timezone
        import datetime
        
        # Base queryset
        qs = cls.objects
        
        # Apply date filtering if period is specified or custom dates are provided
        now = timezone.now()
        
        if custom_start and custom_end:
            # Use custom date range if provided
            qs = qs.filter(end_time__gte=custom_start, end_time__lte=custom_end)
        elif period == 'weekly':
            # Last 7 days
            start_date = now - datetime.timedelta(days=7)
            qs = qs.filter(end_time__gte=start_date)
        elif period == 'monthly':
            # Current month
            start_date = datetime.datetime(now.year, now.month, 1, tzinfo=timezone.get_current_timezone())
            qs = qs.filter(end_time__gte=start_date)
        elif period == 'yearly':
            # Current year
            start_date = datetime.datetime(now.year, 1, 1, tzinfo=timezone.get_current_timezone())
            qs = qs.filter(end_time__gte=start_date)
        
        # Count tours by status
        completed = qs.filter(status='completed').count()
        active = qs.filter(status='active').count()
        cancelled = qs.filter(status='cancelled').count()
        
        # Calculate total revenue (price * slots_booked) for completed tours
        total_revenue = qs.filter(status='completed').aggregate(
            revenue=Sum(F('price') * F('slots_booked'))
        )['revenue'] or 0
        
        return {
            'completed_tours': completed,
            'active_tours': active,
            'cancelled_tours': cancelled,
            'total_revenue': total_revenue,
            'period': period or 'all time'
        }


class Tour_Admission(models.Model):
    Admis_id = models.AutoField(primary_key=True)
    sched_id = models.ForeignKey('Tour_Schedule', on_delete=models.CASCADE,
                                 related_name='admissions')  # ✅ Fix field name
    payables = models.ForeignKey('Admission_Rates', on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.payables} - {self.amount}"

class Tour_Event(models.Model):
    # Auto-generated event_ID in the format Event00001, Event00002, etc.
    event_ID = models.CharField(max_length=10, primary_key=True, unique=True, blank=True)
    sched_id = models.ForeignKey('Tour_Schedule', on_delete=models.CASCADE, related_name='events')
    day_number = models.PositiveIntegerField(default=1)  # New field: which day of the tour
    event_time = models.TimeField()
    event_name = models.CharField(max_length=200)
    event_description = models.TextField()
    image = models.ImageField(upload_to='tour_images/', null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.event_ID:
            last_event = Tour_Event.objects.order_by('-event_ID').first()
            if last_event and last_event.event_ID.startswith("Event"):
                last_id = int(last_event.event_ID[5:])
                new_id = f"Event{last_id + 1:05d}"
            else:
                new_id = "Event00001"
            self.event_ID = new_id

        # Validate day_number is within schedule duration
        if self.sched_id and self.day_number > self.sched_id.duration_days:
            self.day_number = self.sched_id.duration_days
            
        print(f"📝 Generating event_ID: {self.event_ID}")  # Debugging
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Day {self.day_number}: {self.event_name}"

    class Meta:
        ordering = ['day_number', 'event_time']  # Order by day first, then time


class Admission_Rates(models.Model):
    rate_id = models.AutoField(primary_key=True)
    tour_id = models.ForeignKey('Tour_Add', on_delete=models.CASCADE, related_name='admission_rates', null=True, blank=True)
    payables = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.payables} - {self.price}"