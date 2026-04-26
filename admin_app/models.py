from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.utils.translation import gettext_lazy as _
from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from django.conf import settings

class EmployeeManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError(_('The Email field is required'))
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)  # hashes the password
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)

class Employee(AbstractBaseUser):
    emp_id = models.AutoField(primary_key=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True, null=True)
    username = models.CharField(max_length=100, unique=True, default='default_username')
    age = models.IntegerField()
    phone_number = models.CharField(max_length=15, unique=True)
    email = models.EmailField(unique=True)
    sex_choices = [('M', 'Male'), ('F', 'Female')]
    sex = models.CharField(max_length=1, choices=sex_choices)
    profile_picture = models.ImageField(upload_to='employee_pictures/', blank=True, null=True)
    role = models.CharField(max_length=50, default='Employee', editable=False)
    status = models.CharField(max_length=50, default='pending', editable=False)
    last_login = models.DateTimeField(null=True, blank=True)  # Track last login time

    # Required by Django permissions
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    objects = EmployeeManager()

    USERNAME_FIELD = 'email'  # we log in by email
    REQUIRED_FIELDS = ['first_name', 'last_name', 'age', 'phone_number', 'sex']

    def __str__(self):
        return f'{self.first_name} {self.last_name}'


class UserActivity(models.Model):
    """Model to track user activity for employees and admins"""
    ACTIVITY_TYPES = [
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('view_page', 'View Page'),
        ('update', 'Update Data'),
        ('create', 'Create Data'),
        ('delete', 'Delete Data'),
        ('approve', 'Approve Request'),
        ('reject', 'Reject Request'),
        ('other', 'Other Action')
    ]
    
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='activities')
    activity_type = models.CharField(max_length=20, choices=ACTIVITY_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)
    page = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True, null=True)
    
    class Meta:
        verbose_name_plural = "User Activities"
        ordering = ['-timestamp']
    
    def __str__(self):
        return f"{self.employee} - {self.get_activity_type_display()} - {self.timestamp}"


class Accomodation(models.Model):
    APPROVAL_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("declined", "Declined"),
    ]

    accom_id = models.AutoField(primary_key=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_accommodations",
        null=True,
        blank=True,
    )
    company_name = models.CharField(max_length=200, db_index=True)
    email_address = models.EmailField(unique=True)
    location = models.CharField(max_length=300, db_index=True)
    company_type = models.CharField(max_length=100, db_index=True)
    description = models.TextField(blank=True, default="")
    accommodation_amenities = models.TextField(blank=True, default="")
    official_booking_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Official external booking page URL for promotional handoff.",
    )
    official_contact_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Official contact/inquiry page URL for this accommodation.",
    )
    password = models.CharField(max_length=128)
    phone_number = models.CharField(max_length=20)
    status = models.CharField(max_length=50, null=True, blank=True, default="Pending")
    approval_status = models.CharField(
        max_length=20,
        choices=APPROVAL_STATUS_CHOICES,
        default="pending",
    )
    profile_picture = models.ImageField(upload_to='accommodation_profiles/', blank=True, null=True)
    rejection_reason = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "admin_app.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_accommodations",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-submitted_at", "company_name"]

    def mark_reviewed(self, *, status_value, reviewer=None, rejection_reason=""):
        normalized = str(status_value or "").strip().lower()
        if normalized not in {"accepted", "declined", "pending"}:
            raise ValueError("Invalid approval status.")
        self.approval_status = normalized
        self.status = normalized
        self.reviewed_at = timezone.now()
        self.reviewed_by = reviewer
        self.rejection_reason = str(rejection_reason or "").strip()

    def save(self, *args, **kwargs):
        # If the password is not already hashed, hash it.
        # Django password hashes usually start with a prefix like 'pbkdf2_'
        if self.password and not self.password.startswith('pbkdf2_'):
            self.password = make_password(self.password)
        # Keep legacy status field in sync with normalized approval_status.
        if self.approval_status:
            self.status = self.approval_status
        elif self.status:
            self.approval_status = self.status.strip().lower()
        if self.approval_status == "accepted" and self.rejection_reason:
            self.rejection_reason = ""
        super().save(*args, **kwargs)

    def __str__(self):
        return self.company_name


class AccommodationCertification(models.Model):
    """Model to store multiple certification images for accommodations"""
    accommodation = models.ForeignKey(Accomodation, on_delete=models.CASCADE, related_name='certifications')
    image = models.ImageField(upload_to='accommodation_certifications/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Certification for {self.accommodation.company_name} ({self.id})"


class AdminInfo(models.Model):
    username = models.CharField(max_length=255, unique=True)
    password = models.CharField(max_length=255)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    is_staff = models.BooleanField(default=False)  # Only set True for admin users
    last_login = models.DateTimeField(null=True, blank=True)  # Add last_login field

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)

    def __str__(self):
        return self.username


from django.db import models

class Region(models.Model):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

class Country(models.Model):
    name = models.CharField(max_length=255)
    region = models.ForeignKey(Region, related_name="countries", on_delete=models.CASCADE)

    def __str__(self):
        return self.name

class Entry(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    is_hotel = models.BooleanField(default=False)  # Existing field for reference

    def __str__(self):
        return self.title


class TourismInformationQuerySet(models.QuerySet):
    def published(self):
        return self.filter(publication_status="published", is_active=True)


class TourismInformation(models.Model):
    PUBLICATION_STATUS_CHOICES = [
        ("draft", "Draft"),
        ("published", "Published"),
        ("archived", "Archived"),
    ]

    tourism_info_id = models.BigAutoField(primary_key=True)
    spot_name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True, default="")
    location = models.CharField(max_length=300, blank=True, default="")
    contact_information = models.CharField(max_length=255, blank=True, default="")
    operating_hours = models.CharField(max_length=255, blank=True, default="")
    publication_status = models.CharField(
        max_length=20,
        choices=PUBLICATION_STATUS_CHOICES,
        default="draft",
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    image = models.ImageField(upload_to="tourism_information/", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tourism_information",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_tourism_information",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TourismInformationQuerySet.as_manager()

    class Meta:
        ordering = ["spot_name", "-updated_at"]
        verbose_name = "Tourism Information"
        verbose_name_plural = "Tourism Information"

    def __str__(self):
        return self.spot_name

    @property
    def is_published(self):
        return self.publication_status == "published" and self.is_active

class HotelConfirmation(models.Model):
    entry = models.OneToOneField(Entry, on_delete=models.CASCADE)
    confirmed = models.CharField(max_length=3, default="no")  # Will store "yes" if confirmed
    confirmed_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Hotel Confirmation for {self.entry.title}: {self.confirmed}"

class EstablishmentForm(models.Model):
    regions = models.ManyToManyField(Region, related_name="establishment_forms")
    countries = models.ManyToManyField(Country, related_name="establishment_forms")
    entries = models.ManyToManyField(Entry, related_name="establishment_forms")

    def __str__(self):
        return f"Establishment Form"

class Summary(models.Model):
    # Ensure the reference matches the model name exactly
    accom_id = models.ForeignKey('accom_app.Accommodation', on_delete=models.CASCADE)
    month_submitted = models.CharField(max_length=20)
    entry_ans = models.TextField(blank=True, null=True)
    hotel = models.CharField(max_length=1, default="0")  # use "1" for hotel, "0" when not marked as hotel

    def __str__(self):
        return f"Summary for {self.accom_id} on {self.month_submitted}"


class Room(models.Model):
    """Model to store room information for accommodations"""
    ROOM_STATUS_CHOICES = [
        ('AVAILABLE', 'Available'),
        ('OCCUPIED', 'Occupied'),
        ('UNAVAILABLE', 'Unavailable')
    ]
    
    room_id = models.AutoField(primary_key=True)
    accommodation = models.ForeignKey(Accomodation, on_delete=models.CASCADE, related_name='rooms')
    room_name = models.CharField(max_length=100)
    person_limit = models.IntegerField(default=0)
    current_availability = models.IntegerField(null=True, blank=True)
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    room_image = models.ImageField(upload_to='room_images/', blank=True, null=True)
    status = models.CharField(max_length=15, choices=ROOM_STATUS_CHOICES, default='AVAILABLE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        # Add unique constraint for room_name per accommodation
        unique_together = ['accommodation', 'room_name']
    
    def save(self, *args, **kwargs):
        # Set current_availability to person_limit if not specified
        if self.current_availability is None:
            self.current_availability = self.person_limit
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.room_name} (Capacity: {self.person_limit}, Status: {self.get_status_display()})"


class RoomAssignment(models.Model):
    """Model to track guest assignments to rooms"""
    assignment_id = models.AutoField(primary_key=True)
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='assignments')
    guest = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='room_assignments')
    is_owner = models.BooleanField(default=False)
    checked_in = models.DateTimeField(blank=True, null=True)
    checked_out = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.guest} in {self.room}"


class TourAssignment(models.Model):
    """Model to assign tours to employees"""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='tour_assignments')
    schedule = models.ForeignKey('tour_app.Tour_Schedule', on_delete=models.CASCADE, related_name='employee_assignments')
    assigned_date = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['employee', 'schedule']
    
    def __str__(self):
        return f"{self.employee} assigned to {self.schedule}"


class InAppNotification(models.Model):
    NOTIFICATION_TYPE_CHOICES = [
        ("booking", "Booking"),
        ("approval", "Approval"),
        ("assignment", "Assignment"),
        ("billing", "Billing"),
        ("system", "System"),
    ]

    recipient_guest = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="in_app_notifications",
        null=True,
        blank=True,
    )
    recipient_employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="in_app_notifications",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=140)
    message = models.TextField()
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPE_CHOICES,
        default="system",
        db_index=True,
    )
    related_object_id = models.CharField(max_length=40, blank=True, default="")
    url = models.CharField(max_length=255, blank=True, default="")
    dedupe_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient_guest", "is_read", "created_at"]),
            models.Index(fields=["recipient_employee", "is_read", "created_at"]),
        ]

    def __str__(self):
        recipient = self.recipient_guest or self.recipient_employee
        return f"{self.title} -> {recipient}"


class OwnerMonthlyReport(models.Model):
    REPORT_STATUS_CHOICES = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("returned", "Returned for Revision"),
        ("reviewed", "Reviewed"),
    ]

    report_id = models.BigAutoField(primary_key=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owner_monthly_reports",
    )
    accommodation = models.ForeignKey(
        Accomodation,
        on_delete=models.CASCADE,
        related_name="monthly_reports",
    )
    reporting_period = models.DateField(
        help_text="Use first day of month to represent reporting period (e.g., 2026-02-01)."
    )
    guests_checked_in = models.PositiveIntegerField(default=0)
    guests_checked_out = models.PositiveIntegerField(default=0)
    rooms_used = models.PositiveIntegerField(default=0)
    room_usage_notes = models.TextField(blank=True, default="")
    nationality_breakdown = models.TextField(
        blank=True,
        default="",
        help_text="Owner-submitted nationality summary (e.g., Filipino: 34, Foreign: 12).",
    )
    additional_remarks = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=REPORT_STATUS_CHOICES, default="submitted")
    reviewed_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_owner_monthly_reports",
    )
    review_notes = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-reporting_period", "-submitted_at"]
        unique_together = [("accommodation", "reporting_period")]

    def __str__(self):
        return f"{self.accommodation.company_name} | {self.reporting_period:%b %Y} | {self.status}"


class MonthlyReportRoomUsage(models.Model):
    usage_id = models.BigAutoField(primary_key=True)
    monthly_report = models.ForeignKey(
        OwnerMonthlyReport,
        on_delete=models.CASCADE,
        related_name="room_usage_rows",
    )
    room = models.ForeignKey(
        Room,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="monthly_report_usage_rows",
    )
    room_name_snapshot = models.CharField(max_length=120, default="")
    check_ins = models.PositiveIntegerField(default=0)
    check_outs = models.PositiveIntegerField(default=0)
    guests_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["room_name_snapshot", "usage_id"]
        unique_together = [("monthly_report", "room")]

    def __str__(self):
        report_period = self.monthly_report.reporting_period if self.monthly_report else None
        period_text = report_period.strftime("%b %Y") if report_period else "N/A"
        return f"{self.room_name_snapshot} | {period_text}"
