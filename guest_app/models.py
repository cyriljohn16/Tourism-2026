from django.contrib.auth.models import AbstractUser, UserManager
from django.contrib.auth.hashers import make_password
import random
import string
from django.db import models
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.utils.translation import gettext_lazy as _

class GuestManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(username, email, password, **extra_fields)

class CompanionGroup(models.Model):
    """Model to organize companions into groups (family, friends, etc.)"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    owner = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='owned_groups')
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} (owned by {self.owner.first_name})"

class FriendGroup(models.Model):
    """Model for organizing friends into groups"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    owner = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='owned_friend_groups')
    members = models.ManyToManyField('guest_app.Guest', related_name='friend_groups')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.name} (owned by {self.owner.first_name})"

class Guest(AbstractUser):
    guest_id = models.CharField(max_length=5, primary_key=True)
    first_name = models.CharField(max_length=100)
    middle_initial = models.CharField(max_length=1, blank=True, null=True)
    last_name = models.CharField(max_length=100)
    username = models.CharField(max_length=100, unique=True, blank=False, null=False)
    birthday = models.DateField(null=True, blank=True)  # New field for birthday
    age = models.IntegerField(null=True, blank=True)
    age_label = models.CharField(max_length=20, blank=True, null=True)  # New field for age category
    country_of_origin = models.CharField(max_length=100)
    city = models.CharField(max_length=100, blank=True, null=True)
    phone_number = models.CharField(max_length=15)
    email = models.EmailField(unique=True)
    company_name = models.CharField(max_length=100, blank=True, null=True)  # Optional field
    sex_choices = [('M', 'Male'), ('F', 'Female')]
    sex = models.CharField(max_length=1, choices=sex_choices)
    password = models.CharField(max_length=255, default='')
    has_disability = models.BooleanField(default=False)  # New field for disability checkbox
    disability_type = models.CharField(max_length=255, blank=True, null=True)  # New field for disability type
    picture = models.ImageField(upload_to='profile_pictures/', blank=True, null=True)
    
    # Field to track companion accounts - self-referential relationship
    made_by = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='companions')
    
    # Field to organize companions into groups
    group = models.ForeignKey(CompanionGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='members')
    
    # Required fields from AbstractUser that we keep
    is_active = models.BooleanField(default=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email', 'first_name', 'last_name']  # Email is automatically required
    
    objects = GuestManager()
    
    class Meta:
        verbose_name = 'Guest'
        verbose_name_plural = 'Guests'

    def __str__(self):
        return f'{self.first_name} {self.last_name}'

    def _generate_guest_id(self):
        # Generate a 5-character random string for guest_id
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    
    def save(self, *args, **kwargs):
        if not self.pk:
            self.guest_id = self._generate_guest_id()
            
        # Username is no longer needed, so we remove this method
        # We keep the email as the primary identifier for login
            
        # Only hash password if it's not already hashed
        if self.password and not self.password.startswith(('pbkdf2_sha256$', 'bcrypt$', 'argon2$', 'md5$', 'sha1$')):
            self.password = make_password(self.password)
        
        # Calculate age and age_label if birthday is provided
        if self.birthday:
            from datetime import date
            today = date.today()
            age = today.year - self.birthday.year - ((today.month, today.day) < (self.birthday.month, self.birthday.day))
            self.age = age
            
            # Set age_label based on age
            if age <= 1:
                self.age_label = 'Infant'
            elif 2 <= age <= 4:
                self.age_label = 'Toddler'
            elif 5 <= age <= 12:
                self.age_label = 'Child'
            elif 13 <= age <= 19:
                self.age_label = 'Teen'
            elif 20 <= age <= 39:
                self.age_label = 'Adult'
            elif 40 <= age <= 59:
                self.age_label = 'Middle Adult'
            elif age >= 60:
                self.age_label = 'Senior'
                
        super().save(*args, **kwargs)

# Add a new base class for translatable models
class TranslatableModel(models.Model):
    """
    Abstract base class for models with translatable fields.
    This allows tracking the language of content creation.
    """
    language = models.CharField(max_length=5, default='en', choices=[
        ('en', _('English')),
        ('tl', _('Tagalog')),
        ('ceb', _('Cebuano')),
        ('es', _('Spanish')),
    ])
    
    class Meta:
        abstract = True

class Pending(models.Model):
    guest_id = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='pending_bookings')
    sched_id = models.ForeignKey('tour_app.Tour_Schedule', on_delete=models.CASCADE, related_name='pending_schedules')
    tour_id = models.ForeignKey('tour_app.Tour_Add', on_delete=models.CASCADE, related_name='pending_tours')
    status = models.CharField(max_length=20, default='Pending')
    total_guests = models.IntegerField(default=1)  # New field to store total number of guests

    # Fields for guest information
    your_name = models.CharField(max_length=255, default="Unknown Name")  # Default name for existing rows
    your_email = models.EmailField(default="default@example.com")
    your_phone = models.CharField(max_length=20, default="000-000-0000")  # Make non-nullable, set default phone number
    num_adults = models.IntegerField(default=1)  # Make non-nullable, set default number of adults
    num_children = models.IntegerField(default=0)  # Make non-nullable, set default number of children
    
    # Cancellation information
    cancellation_reason = models.TextField(blank=True, null=True)
    cancellation_date = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Pending Booking: {self.guest_id} - {self.tour_id} ({self.status})"

    def _generate_guest_id(self):
        last_guest = Guest.objects.all().order_by('-guest_id').first()
        if last_guest:
            last_guest_number = int(last_guest.guest_id)
            new_id = last_guest_number + 1
        else:
            new_id = 1
        return f'{new_id:05}'

    def check_password(self, raw_password):
        from django.contrib.auth.hashers import check_password
        return check_password(raw_password, self.password)

class TourBooking(models.Model):
    """
    Complete tracking of tour bookings with status and revenue information.
    This model extends the Pending model with more detailed status 
    and financial tracking.
    """
    BOOKING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active Tour'),
        ('completed', 'Completed Tour'),
        ('cancelled', 'Cancelled Tour'),
    ]
    
    booking_id = models.AutoField(primary_key=True)
    guest = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='tour_bookings')
    tour = models.ForeignKey('tour_app.Tour_Add', on_delete=models.CASCADE, related_name='bookings')
    schedule = models.ForeignKey('tour_app.Tour_Schedule', on_delete=models.CASCADE, related_name='bookings')
    
    # Status and booking info
    status = models.CharField(max_length=20, choices=BOOKING_STATUS_CHOICES, default='pending')
    booking_date = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    
    # Guest information
    total_guests = models.IntegerField(default=1)
    num_adults = models.IntegerField(default=1)
    num_children = models.IntegerField(default=0)
    
    # Financial information
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    additional_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discounts = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=20, default='unpaid', 
                                    choices=[('unpaid', 'Unpaid'), 
                                            ('partial', 'Partially Paid'),
                                            ('paid', 'Paid')])
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Cancellation information
    cancellation_reason = models.TextField(blank=True, null=True)
    cancellation_date = models.DateTimeField(blank=True, null=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    def __str__(self):
        return f"Booking #{self.booking_id}: {self.guest} - {self.tour} ({self.status})"
    
    def save(self, *args, **kwargs):
        # Calculate total amount if not manually set
        if self.total_amount == 0:
            self.total_amount = self.base_price + self.additional_fees - self.discounts
        super().save(*args, **kwargs)
    
    def get_balance_due(self):
        """Calculate remaining balance to be paid"""
        return self.total_amount - self.amount_paid
    
    def mark_as_completed(self):
        """Mark the tour as completed"""
        self.status = 'completed'
        self.save()
    
    def mark_as_cancelled(self, reason=None, refund_amount=0):
        """Mark the tour as cancelled with optional reason and refund"""
        self.status = 'cancelled'
        self.cancellation_reason = reason
        self.cancellation_date = timezone.now()
        self.refund_amount = refund_amount
        self.save()
    
    @classmethod
    def get_tour_statistics(cls):
        """Get statistics for dashboard display"""
        from django.db.models import Count, Sum
        
        completed = cls.objects.filter(status='completed').count()
        active = cls.objects.filter(status='active').count()
        cancelled = cls.objects.filter(status='cancelled').count()
        total_revenue = cls.objects.filter(status__in=['completed', 'active']).aggregate(
            total=Sum('amount_paid'))['total'] or 0
        
        return {
            'completed_tours': completed,
            'active_tours': active,
            'cancelled_tours': cancelled,
            'total_revenue': total_revenue,
        }

class AccommodationBooking(models.Model):
    """
    Guest-facing accommodation booking with pending/confirmed flow.
    """
    BOOKING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('declined', 'Declined'),
        ('cancelled', 'Cancelled'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),
        ('partial', 'Partially Paid'),
        ('paid', 'Paid'),
    ]

    booking_id = models.AutoField(primary_key=True)
    guest = models.ForeignKey('guest_app.Guest', on_delete=models.CASCADE, related_name='accommodation_bookings')
    accommodation = models.ForeignKey('admin_app.Accomodation', on_delete=models.CASCADE, related_name='guest_bookings')
    room = models.ForeignKey('admin_app.Room', on_delete=models.SET_NULL, null=True, blank=True, related_name='guest_bookings')

    check_in = models.DateField()
    check_out = models.DateField()
    num_guests = models.IntegerField(default=1)

    status = models.CharField(max_length=20, choices=BOOKING_STATUS_CHOICES, default='pending')
    booking_date = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid')
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    cancellation_reason = models.TextField(blank=True, null=True)
    cancellation_date = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Accommodation Booking #{self.booking_id}: {self.guest} - {self.accommodation}"

    def nights(self):
        delta = (self.check_out - self.check_in).days
        return max(delta, 1)

    def get_balance_due(self):
        return self.total_amount - self.amount_paid

class MapBookmark(TranslatableModel):
    CATEGORY_CHOICES = [
        ('restaurant', _('Food & Restaurant')),
        ('hotel', _('Hotel & Accommodation')),
        ('public', _('Public Service')),
        ('shopping', _('Shopping')),
        ('landmark', _('Landmark')),
        ('custom', _('Custom Place')),
    ]
    
    name = models.CharField(max_length=100)
    name_tl = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Name (Tagalog)'))
    name_ceb = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Name (Cebuano)'))
    name_es = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Name (Spanish)'))
    
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    latitude = models.FloatField()
    longitude = models.FloatField()
    
    details = models.TextField(blank=True, null=True)
    details_tl = models.TextField(blank=True, null=True, verbose_name=_('Details (Tagalog)'))
    details_ceb = models.TextField(blank=True, null=True, verbose_name=_('Details (Cebuano)'))
    details_es = models.TextField(blank=True, null=True, verbose_name=_('Details (Spanish)'))
    
    user = models.ForeignKey(Guest, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Optional primary image field
    primary_image = models.ImageField(upload_to='bookmark_primary_images/', blank=True, null=True)
    
    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"
    
    def get_name(self, lang='en'):
        """Get the name in the specified language"""
        if lang == 'tl' and self.name_tl:
            return self.name_tl
        elif lang == 'ceb' and self.name_ceb:
            return self.name_ceb
        elif lang == 'es' and self.name_es:
            return self.name_es
        return self.name
        
    def get_details(self, lang='en'):
        """Get the details in the specified language"""
        if lang == 'tl' and self.details_tl:
            return self.details_tl
        elif lang == 'ceb' and self.details_ceb:
            return self.details_ceb
        elif lang == 'es' and self.details_es:
            return self.details_es
        return self.details


class BookmarkImage(models.Model):
    """Model to store multiple images for each bookmark with descriptions"""
    bookmark = models.ForeignKey(MapBookmark, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='bookmark_images/')
    
    title = models.CharField(max_length=100, blank=True)
    title_tl = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Title (Tagalog)'))
    title_ceb = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Title (Cebuano)'))
    title_es = models.CharField(max_length=100, blank=True, null=True, verbose_name=_('Title (Spanish)'))
    
    description = models.TextField(blank=True)
    description_tl = models.TextField(blank=True, null=True, verbose_name=_('Description (Tagalog)'))
    description_ceb = models.TextField(blank=True, null=True, verbose_name=_('Description (Cebuano)'))
    description_es = models.TextField(blank=True, null=True, verbose_name=_('Description (Spanish)'))
    
    upload_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Image for {self.bookmark.name}: {self.title}"
    
    def get_title(self, lang='en'):
        """Get the title in the specified language"""
        if lang == 'tl' and self.title_tl:
            return self.title_tl
        elif lang == 'ceb' and self.title_ceb:
            return self.title_ceb
        elif lang == 'es' and self.title_es:
            return self.title_es
        return self.title
        
    def get_description(self, lang='en'):
        """Get the description in the specified language"""
        if lang == 'tl' and self.description_tl:
            return self.description_tl
        elif lang == 'ceb' and self.description_ceb:
            return self.description_ceb
        elif lang == 'es' and self.description_es:
            return self.description_es
        return self.description

# Profile update functions have been moved to views.py

# Add these models after the Guest model but before any other model

class GuestCredential(models.Model):
    """Model to store multiple credential images for each guest (ID cards, passports, etc.)"""
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='credentials')
    document = models.ImageField(upload_to='guest_credentials/')
    document_type = models.CharField(max_length=100, blank=True, null=True)  # Optional type field
    upload_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        doc_type = f" ({self.document_type})" if self.document_type else ""
        return f"Credential for {self.guest.first_name} {self.guest.last_name}{doc_type}"

class DisabilityDocument(models.Model):
    """Model to store disability verification documents for guests with disabilities"""
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='disability_documents')
    document = models.FileField(upload_to='disability_documents/')
    description = models.CharField(max_length=255, blank=True, null=True)  # Optional description field
    upload_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Disability Document for {self.guest.first_name} {self.guest.last_name}"

class CompanionRequest(models.Model):
    """Model to store companion account requests between users"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined')
    ]
    
    sender = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='sent_companion_requests')
    recipient = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='received_companion_requests')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    message = models.TextField(blank=True, null=True, help_text="Optional message to send with the request")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Group to assign the companion to if request is accepted
    group = models.ForeignKey(CompanionGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='companion_requests')
    
    class Meta:
        unique_together = ('sender', 'recipient')
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Companion Request: {self.sender} -> {self.recipient} ({self.status})"
    
    def accept(self):
        """Accept the companion request"""
        if self.status == 'pending':
            self.status = 'accepted'
            self.save()
            return True
        return False
    
    def decline(self):
        """Decline the companion request"""
        if self.status == 'pending':
            self.status = 'declined'
            self.save()
            return True
        return False

class BookingCompanion(models.Model):
    """Model to track which companions are included in a booking"""
    booking = models.ForeignKey('Pending', on_delete=models.CASCADE, related_name='companions')
    companion = models.ForeignKey('Guest', on_delete=models.CASCADE, related_name='bookings')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('booking', 'companion')
    
    def __str__(self):
        return f"{self.companion.first_name} {self.companion.last_name} for booking {self.booking.id}"

class Friendship(models.Model):
    """Model to track direct connections between guests for easier companion management"""
    user = models.ForeignKey('Guest', on_delete=models.CASCADE, related_name='friendships')
    friend = models.ForeignKey('Guest', on_delete=models.CASCADE, related_name='friend_of')
    group_name = models.CharField(max_length=100, default='Friends')  # Category of relationship
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('user', 'friend')
        
    def __str__(self):
        return f"{self.user.first_name} → {self.friend.first_name} ({self.group_name})"
    
    @classmethod
    def make_friendship(cls, user, friend, group_name='Friends'):
        """Create a bidirectional friendship between two users"""
        # Create friendship in both directions
        cls.objects.get_or_create(user=user, friend=friend, defaults={'group_name': group_name})
        cls.objects.get_or_create(user=friend, friend=user, defaults={'group_name': group_name})
        return True
    
    @classmethod
    def end_friendship(cls, user, friend):
        """Remove friendship connection in both directions"""
        cls.objects.filter(user=user, friend=friend).delete()
        cls.objects.filter(user=friend, friend=user).delete()
        return True

