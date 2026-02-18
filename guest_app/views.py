from django.contrib.auth import logout, authenticate, login as auth_login
from django.db import IntegrityError
from tour_app.models import Tour_Event
from .forms import GuestRegistrationForm
import calendar
from datetime import datetime, timedelta
from django.contrib import messages
from tour_app.models import Tour_Schedule, Tour_Add, Tour_Admission, Admission_Rates, Tour_Event
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import Pending, Guest, GuestCredential, DisabilityDocument, BookingCompanion  # Add BookingCompanion here
from .forms import BookingForm  # Assuming this is your form for booking
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from guest_app.models import Pending, Guest
from tour_app.models import Tour_Schedule, Tour_Add
from .models import MapBookmark, BookmarkImage
import json
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.utils.decorators import method_decorator
import base64
from django.core.files.base import ContentFile
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.views.decorators.http import require_http_methods
from .utils import translate, get_translations_json, set_language, get_current_language, LANGUAGE_SESSION_KEY
from django.shortcuts import render, get_object_or_404, redirect
from .models import TourBooking
from .forms import ProfileUpdateForm
from django.utils import timezone
import requests  # Add this import
from django.db import models
from .models import FriendGroup, Friendship
import pytz  # Add this import
import qrcode
from io import BytesIO
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image
from admin_app.models import Accomodation, Room as AdminRoom
from .models import AccommodationBooking
from ai_chatbot.recommenders import recommend_accommodations, calculate_accommodation_billing

@ensure_csrf_cookie
def main_page(request):
    """Main page view with language support"""
    # Helper function to ensure datetime objects are properly converted
    def ensure_timezone_aware(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except ValueError:
                try:
                    dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        if not timezone.is_aware(dt):
            dt = timezone.make_aware(dt)
        return dt
    
    # Get the current language preference
    current_language = get_current_language(request)
    
    # Get all tours
    tours = Tour_Add.objects.all()
    
    # For each tour, translate translatable fields and calculate min/max duration
    translated_tours = []
    for tour in tours:
        # Calculate min and max duration days for each tour's schedules
        schedules = Tour_Schedule.objects.filter(tour_id=tour.tour_id)
        min_duration = None
        max_duration = None
        
        if schedules.exists():
            durations = [s.duration_days for s in schedules if s.duration_days]
            if durations:
                min_duration = min(durations)
                max_duration = max(durations)
        
        # Attach duration info to the tour object for easy access in template
        tour.min_duration = min_duration
        tour.max_duration = max_duration
        tour.has_duration_range = schedules.count() > 1 and min_duration != max_duration
        
        # Append tour and its translatable fields as context 
        # (assuming Tour_Add has translatable fields like name_tl, description_tl, etc.)
        tour_data = {
            'tour': tour,
            'translatable': {
                'tour_name': getattr(tour, f'tour_name_{current_language}', tour.tour_name),
                'description': getattr(tour, f'description_{current_language}', tour.description),
                # Add other translatable fields as needed
            }
        }
        translated_tours.append(tour_data)
    
    # Get user bookings if authenticated
    upcoming_tours = []
    current_tours = []
    past_tours = []
    
    if request.user.is_authenticated:
        # Get current time for comparison - remove any time zone issues by using UTC
        now = timezone.now()
        print(f"Current server time (UTC): {now}")
        
        # Reset the lists to ensure they're empty
        upcoming_tours = []
        current_tours = []
        past_tours = []
        
        # First, try to get TourBooking records
        tour_bookings = TourBooking.objects.filter(
            guest=request.user
        ).select_related('tour', 'schedule').order_by('schedule__start_time')
        
        print(f"Found {len(tour_bookings)} TourBooking records")
        
        # Also check for Pending bookings
        pending_bookings = Pending.objects.filter(
            guest_id=request.user
        ).select_related('tour_id', 'sched_id').order_by('sched_id__start_time')
        
        print(f"Found {len(pending_bookings)} Pending records")
        
        print(f"Total bookings to process: {len(tour_bookings) + len(pending_bookings)}")
        
        # Process all bookings and categorize them based ONLY on time, not status
        
        # Process TourBooking records first
        for booking in tour_bookings:
            try:
                # Only skip if explicitly cancelled
                if booking.status == 'cancelled':
                    print(f"TourBooking {booking.booking_id} is cancelled, adding to past_tours")
                    past_tours.append(booking)
                    continue
                
                # Get schedule times directly
                start_time = booking.schedule.start_time
                end_time = booking.schedule.end_time
                
                # Ensure timezone awareness for proper comparison
                if not timezone.is_aware(start_time):
                    start_time = timezone.make_aware(start_time)
                if not timezone.is_aware(end_time):
                    end_time = timezone.make_aware(end_time)
                
                # Debug output
                print(f"TourBooking {booking.booking_id}: {start_time} to {end_time}, now is {now}")
                
                # Simple date comparison
                if start_time > now:
                    print(f"TourBooking {booking.booking_id} is UPCOMING")
                    # Update status to 'pending' if it's not already set
                    if booking.status not in ['pending', 'cancelled', 'active', 'completed']:
                        booking.status = 'pending'
                        booking.save()
                    upcoming_tours.append(booking)
                elif start_time <= now and end_time >= now:
                    print(f"TourBooking {booking.booking_id} is CURRENT")
                    # Update status to 'active' if it's not already set
                    if booking.status not in ['active', 'cancelled', 'completed']:
                        booking.status = 'active'
                        booking.save()
                    current_tours.append(booking)
                else:
                    print(f"TourBooking {booking.booking_id} is PAST")
                    # Update status to 'completed' if it's not already set
                    if booking.status not in ['completed', 'cancelled']:
                        booking.status = 'completed'
                        booking.save()
                    past_tours.append(booking)
            except Exception as e:
                print(f"Error categorizing TourBooking {booking.booking_id}: {str(e)}")
                past_tours.append(booking)  # Default to past if error occurs
        
        # Now process Pending bookings
        for booking in pending_bookings:
            try:
                # Only skip if explicitly cancelled
                if booking.status.lower() == 'cancelled':
                    print(f"Pending {booking.id} has status Cancelled, adding to past_tours")
                    past_tours.append(booking)
                    continue
                
                # Get schedule times directly
                start_time = booking.sched_id.start_time
                end_time = booking.sched_id.end_time
                
                # Ensure timezone awareness for proper comparison
                if not timezone.is_aware(start_time):
                    start_time = timezone.make_aware(start_time)
                if not timezone.is_aware(end_time):
                    end_time = timezone.make_aware(end_time)
                
                # Debug output
                print(f"Pending {booking.id}: {start_time} to {end_time}, now is {now}")
                
                # Simple date comparison - ignore the 'Pending' status and use only timing
                if start_time > now:
                    print(f"Pending {booking.id} is UPCOMING")
                    # Keep as Pending (no status change needed)
                    upcoming_tours.append(booking)
                elif start_time <= now and end_time >= now:
                    print(f"Pending {booking.id} is CURRENT")
                    # Update to Active if it's not already
                    if booking.status.lower() == 'pending':
                        booking.status = 'Active'
                        booking.save()
                    current_tours.append(booking)
                else:
                    print(f"Pending {booking.id} is PAST")
                    # Update to Completed if not cancelled
                    if booking.status.lower() == 'pending':
                        booking.status = 'Completed'
                        booking.save()
                    past_tours.append(booking)
            except Exception as e:
                print(f"Error categorizing Pending {booking.id}: {str(e)}")
                past_tours.append(booking)  # Default to past if error occurs
        
        print(f"Final categorization: {len(upcoming_tours)} upcoming, {len(current_tours)} current, {len(past_tours)} past")
    
    context = {
        'tours': tours,  # Keep the original queryset for Django template usage
        'translated_tours': translated_tours,  # Add translated data
        'user': request.user,
        'upcoming_tours': upcoming_tours,
        'current_tours': current_tours,
        'past_tours': past_tours,
        'current_language': current_language,
        'translations_json': get_translations_json(current_language)  # Add translations for JavaScript
    }
    
    return render(request, 'mainpage.html', context)


def user_is_allowed(user):
    # Implement your custom logic to check if the user is allowed
    # Example: Check if the user is authenticated or has specific permissions
    return user.is_authenticated  # or any other condition you need

def register(request):
    if request.method == 'POST':
        print("Files in request:", request.FILES)
        print("POST data:", request.POST)
        form = GuestRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                guest = form.save(commit=False)

                # Handle optional company_name field
                company_name = form.cleaned_data.get('company_name')
                if company_name:
                    guest.company_name = company_name
                else:
                    guest.company_name = None

                # Save the guest to create the instance with an ID
                guest.save()

                messages.success(request, 'Registration successful! You can now log in.')

                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': 'Registration successful! Logging you in...'
                    })
                return redirect('login')
            except IntegrityError as e:
                error_msg = str(e).lower()
                if "email" in error_msg:
                    form.add_error('email', 'This email is already registered. Please use another or login.')
                    messages.error(request, 'This email is already registered. Please use another or login.')
                else:
                    messages.error(request, 'An error occurred during registration.')
        else:
            # Form is invalid, so we don't change the generic message
            messages.error(request, 'Please correct the errors below.')
        
        # For AJAX requests with errors, return JSON response
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'Please correct the errors below.',
                'errors': {field: errors[0] for field, errors in form.errors.items()}
            })
    else:
        form = GuestRegistrationForm()
    return render(request, 'register.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('main-page')

    if request.method == 'POST':
        email = request.POST['email']
        password = request.POST['password']
        
        # Try to find a user with the given email
        try:
            user = Guest.objects.get(email=email)
            # Authenticate with the username field (email) and password
            user = authenticate(request, username=email, password=password)
            
            if user is not None:
                auth_login(request, user)
                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'first_name': user.first_name,
                        'message': 'Login successful'
                    })
                return redirect(request.GET.get('next', 'main-page'))
            else:
                messages.error(request, "Invalid email or password")
                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False, 
                        'message': 'Invalid email or password'
                    })
        except Guest.DoesNotExist:
            messages.error(request, "Invalid email or password")
            # For AJAX requests, return JSON response
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False, 
                    'message': 'Invalid email or password'
                })
        
        return redirect('main-page')

    # Keep this URL for login POST handling, but use main-page as the UI entry point.
    return redirect('main-page')


def logout_view(request):
    if request.user.is_authenticated:
        logout(request)  # Logs out the user
        request.session.flush()  # Completely removes session data
        request.session.clear()  # Ensures session dictionary is emptied (optional)

    messages.success(request, "You have been logged out successfully.")
    
    # For AJAX requests, return JSON response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': 'You have been logged out successfully.'
        })
        
    return redirect('main-page')  # Redirect to main page instead of login


def tour_schedule_detail(request, sched_id):
    # Fetch the schedule details
    schedule = get_object_or_404(Tour_Schedule, id=sched_id)

    context = {
        'schedule': schedule,
    }

    return render(request, 'guest_book.html', context)


# API to fetch tour schedules dynamically
def get_tour_schedules(request, tour_id):
    tour_schedules = Tour_Schedule.objects.filter(tour_id=tour_id)
    schedules = []

    for schedule in tour_schedules:
        schedules.append({
            'start': schedule.start_time.strftime('%Y-%m-%dT%H:%M:%S'),
            'end': schedule.end_time.strftime('%Y-%m-%dT%H:%M:%S'),
        })

    return JsonResponse({'schedules': schedules})




def book_tour(request):
    if request.method == "POST":
        try:
            # Verify reCAPTCHA
            recaptcha_response = request.POST.get('g_recaptcha_response')
            if not recaptcha_response:
                return JsonResponse({'error': 'Please complete the reCAPTCHA verification.'}, status=400)
                
            url = 'https://www.google.com/recaptcha/api/siteverify'
            data = {
                'secret': '6LeyhAYrAAAAAJdVcBnugINI6kChp_pbtBNIqkyk',
                'response': recaptcha_response
            }
            recaptcha_result = requests.post(url, data=data).json()
            
            # If reCAPTCHA fails
            if not recaptcha_result.get('success', False):
                error_message = recaptcha_result.get('error-codes', ['Unknown error'])[0]
                return JsonResponse({
                    'error': f'reCAPTCHA verification failed: {error_message}. Please try again.'
                }, status=400)
                
            # Continue with your existing booking code
            guest_id = request.POST.get('guest_id')
            sched_id = request.POST.get('sched_id')
            price = float(request.POST.get('price', 0))
            total_guests = int(request.POST.get('total_guests', 1))
            
            # Get the selected companions
            selected_companions_json = request.POST.get('selected_companions', '[]')
            selected_companions = json.loads(selected_companions_json)
            
            # Get the guest; adjust field name if needed.
            guest = get_object_or_404(Guest, guest_id=guest_id)
            # Get the schedule object from Tour_Schedule
            schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
            # Get the tour from the schedule (assuming FK is set up correctly)
            tour = schedule.tour_id

            # Create the pending booking record
            pending_booking = Pending.objects.create(
                guest_id=guest,
                sched_id=schedule,
                tour_id=tour,
                status="Pending",
                total_guests=total_guests,
                your_name=f"{guest.first_name} {guest.last_name}",
                your_email=guest.email,
                your_phone=guest.phone_number,
                num_adults=total_guests,
                num_children=0
            )
            
            # Fetch companion details for email
            companion_names = []
            
            # Save the selected companions for this booking
            if selected_companions:
                # Fetch companion objects
                companions = Guest.objects.filter(guest_id__in=selected_companions)
                
                # Create BookingCompanion records
                for companion in companions:
                    BookingCompanion.objects.create(
                        booking=pending_booking,
                        companion=companion
                    )
                    companion_names.append(f"{companion.first_name} {companion.last_name}")

            # Ensure that there are enough available slots
            if schedule.slots_available < total_guests:
                return JsonResponse({'error': 'Not enough available slots.'}, status=400)

            schedule.slots_booked += total_guests
            schedule.slots_available -= total_guests
            schedule.save()

            # Calculate price information for the response
            total_amount = total_guests * price

            # Send a more detailed booking acknowledgment email
            try:
                subject = f"Booking Request Received for {tour.tour_name}"
                
                # Format schedule in user-friendly way
                start_time = timezone.localtime(schedule.start_time)
                end_time = timezone.localtime(schedule.end_time)
                
                # Format dates in a user-friendly way (e.g., "Monday 12 March 2024 at 8:00 AM")
                start_formatted = start_time.strftime("%A %d %B %Y at %I:%M %p")
                end_formatted = end_time.strftime("%A %d %B %Y at %I:%M %p")
                
                # Get current time in Philippine timezone (Asia/Manila)
                # Without pytz, we'll use a simplified approach for Philippines time
                # Note: this is an approximation, as Manila is UTC+8
                from datetime import timedelta
                current_ph_time = timezone.now() + timedelta(hours=8)  # Approximate Manila time
                ph_time_formatted = current_ph_time.strftime("%A %d %B %Y at %I:%M %p")
                
                # For guest's country time, we'll include a note about timezone
                guest_country_formatted = f"(Please check local time in {guest.country_of_origin})"
                
                # Create a formatted message with companion details
                companions_list = "\n".join([f"- {name}" for name in companion_names])
                if not companions_list:
                    companions_list = "None"
                    
                message = f"""Dear {guest.first_name},

Your booking request for {tour.tour_name} has been received and is pending approval.

Booking Details:
- Tour: {tour.tour_name}
- Schedule: {start_formatted} to {end_formatted}
- Total Guests: {total_guests}
- Total Amount: ₱{total_amount:.2f}

Companions included:
{companions_list}

Time Information:
- Current Philippine Time: {ph_time_formatted}
- Your Country ({guest.country_of_origin}): {guest_country_formatted}

We will notify you once your booking is confirmed or if we need additional information.

Thank you for choosing our tours!

Best regards,
The Tour Team"""
                
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[guest.email],
                    fail_silently=False,
                )
                print(f"Booking acknowledgment email sent to {guest.email}")
            except Exception as email_error:
                print(f"Email sending failed: {str(email_error)}")
                # Continue even if email fails

            return JsonResponse({
                'success': 'Booking request submitted! You will receive a confirmation email when your booking is approved.',
                'total_payment': total_amount
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Invalid request'}, status=400)

@login_required
def guest_book(request, tour_id):
    """View for displaying tour booking page with language support"""
    # Get current language
    current_language = get_current_language(request)
    
    # Retrieve the tour based on the provided tour_id from the URL.
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)

    # Retrieve all schedules associated with this tour.
    # Assuming your Tour_Schedule model's foreign key to Tour_Add is named "tour_id".
    schedules = Tour_Schedule.objects.filter(tour_id=tour)
    
    # Prepare translated tour data
    tour_data = {
        'id': tour.tour_id,
        'name': getattr(tour, f'tour_name_{current_language}', tour.tour_name),
        'description': getattr(tour, f'description_{current_language}', tour.description),
        # Add other translatable fields
    }
    
    # Add translations for this specific tour to the translations dictionary
    tour_translations = {
        f'tour_{tour.tour_id}_name': tour_data['name'],
        f'tour_{tour.tour_id}_description': tour_data['description'],
    }
    
    # Get generic translations and add tour-specific ones
    translations = json.loads(get_translations_json(current_language))
    translations.update(tour_translations)
    
    # Add translations for booking-related terms
    booking_translations = {
        'schedule_id': translate('schedule_id', current_language),
        'start_time': translate('start_time', current_language),
        'end_time': translate('end_time', current_language),
        'price': translate('price', current_language),
        'available_slots': translate('available_slots', current_language),
        'booked_slots': translate('booked_slots', current_language),
        'book_this_schedule': translate('book_this_schedule', current_language),
        'no_more_slots': translate('no_more_slots', current_language),
        'no_schedules': translate('no_schedules', current_language),
        'back_to_main': translate('back_to_main', current_language),
    }
    translations.update(booking_translations)
    
    context = {
        'tour': tour,
        'schedules': schedules,
        'current_language': current_language,
        'translations_json': json.dumps(translations),
        'tour_data': tour_data,
    }
    
    return render(request, 'guest_book.html', context)

def map_view(request):
    """View for displaying the interactive Bayawan City map with language support"""
    # Get current language
    current_language = get_current_language(request)
    
    # Get bookmarks for the current user
    if request.user.is_authenticated:
        bookmarks = MapBookmark.objects.filter(user=request.user)
    else:
        bookmarks = MapBookmark.objects.filter(user=None)
    
    # Translate bookmarks
    translated_bookmarks = []
    for bookmark in bookmarks:
        bookmark_data = {
            'id': bookmark.id,
            'name': bookmark.get_name(current_language),
            'category': bookmark.category,
            'lat': bookmark.latitude,
            'lng': bookmark.longitude,
            'details': bookmark.get_details(current_language) or '',
            'images': []
        }
        
        # Get translated images
        for image in bookmark.images.all():
            image_data = {
                'id': image.id,
                'title': image.get_title(current_language),
                'description': image.get_description(current_language),
                'url': request.build_absolute_uri(image.image.url) if image.image else None,
            }
            bookmark_data['images'].append(image_data)
        
        translated_bookmarks.append(bookmark_data)
    
    return render(request, 'map.html', {
        'bookmarks': bookmarks,  # Original queryset for Django templates
        'translated_bookmarks': translated_bookmarks,  # Translated data
        'current_language': current_language,
        'translations_json': get_translations_json(current_language)
    })

# API endpoints for map bookmarks
def bookmark_list(request):
    """API endpoint to list all bookmarks with language support"""
    print("Bookmark list requested")
    
    # Get current language
    current_language = get_current_language(request)
    
    if request.user.is_authenticated:
        bookmarks = MapBookmark.objects.filter(user=request.user)
    else:
        # For anonymous users, get bookmarks with no user
        bookmarks = MapBookmark.objects.filter(user=None)
    
    data = []
    for bookmark in bookmarks:
        # Get translated name and details
        name = bookmark.get_name(current_language)
        details = bookmark.get_details(current_language)
        
        # Get images for this bookmark with translations
        images = []
        for image in bookmark.images.all():
            image_data = {
                'id': image.id,
                'title': image.get_title(current_language),
                'description': image.get_description(current_language),
                'url': request.build_absolute_uri(image.image.url) if image.image else None,
            }
            images.append(image_data)
        
        # Add bookmark data with images
        data.append({
            'id': bookmark.id,
            'name': name,
            'category': bookmark.category,
            'lat': bookmark.latitude,
            'lng': bookmark.longitude,
            'details': details or '',
            'images': images
        })
    
    return JsonResponse({'bookmarks': data})

@csrf_exempt
def bookmark_create(request):
    """API endpoint to create a new bookmark"""
    print("Bookmark create requested")
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            print("Received bookmark data:", data)
            
            bookmark = MapBookmark(
                name=data.get('name'),
                category=data.get('category', 'custom'),
                latitude=data.get('lat'),
                longitude=data.get('lng'),
                details=data.get('details', '')
            )
            
            if request.user.is_authenticated:
                bookmark.user = request.user
                
            bookmark.save()
            print("Bookmark created with ID:", bookmark.id)
            
            return JsonResponse({
                'success': True,
                'id': bookmark.id,
                'message': 'Bookmark created successfully'
            })
        except Exception as e:
            print("Error creating bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_update(request, bookmark_id):
    """API endpoint to update a bookmark"""
    print(f"Bookmark update requested for ID: {bookmark_id}")
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            print("Update data:", data)
            
            # Get the bookmark, checking for ownership
            if request.user.is_authenticated:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=request.user)
            else:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=None)
            
            # Update fields
            if 'name' in data:
                bookmark.name = data['name']
            if 'category' in data:
                bookmark.category = data['category']
            if 'lat' in data:
                bookmark.latitude = data['lat']
            if 'lng' in data:
                bookmark.longitude = data['lng']
            if 'details' in data:
                bookmark.details = data['details']
            
            bookmark.save()
            print("Bookmark updated successfully")
            
            return JsonResponse({
                'success': True,
                'message': 'Bookmark updated successfully'
            })
        except MapBookmark.DoesNotExist:
            print("Bookmark not found")
            return JsonResponse({
                'success': False,
                'message': 'Bookmark not found or access denied'
            }, status=404)
        except Exception as e:
            print("Error updating bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_delete(request, bookmark_id):
    """API endpoint to delete a bookmark"""
    print(f"Bookmark delete requested for ID: {bookmark_id}")
    if request.method == 'POST':
        try:
            # Get the bookmark, checking for ownership
            if request.user.is_authenticated:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=request.user)
            else:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=None)
            
            bookmark.delete()
            print("Bookmark deleted successfully")
            
            return JsonResponse({
                'success': True,
                'message': 'Bookmark deleted successfully'
            })
        except MapBookmark.DoesNotExist:
            print("Bookmark not found")
            return JsonResponse({
                'success': False,
                'message': 'Bookmark not found or access denied'
            }, status=404)
        except Exception as e:
            print("Error deleting bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

def bookmark_debug(request):
    """Debug view for bookmark API"""
    # Get all bookmarks
    all_bookmarks = MapBookmark.objects.all()
    
    # Prepare response data
    debug_info = {
        'total_bookmarks': all_bookmarks.count(),
        'bookmarks': [{
            'id': b.id,
            'name': b.name,
            'category': b.category,
            'latitude': b.latitude,
            'longitude': b.longitude,
            'user': b.user.username if b.user else None,
            'created_at': b.created_at.isoformat() if b.created_at else None
        } for b in all_bookmarks],
        'user': {
            'is_authenticated': request.user.is_authenticated,
            'username': request.user.username if request.user.is_authenticated else None
        },
        'csrf_token': request.META.get('CSRF_COOKIE', 'Not set')
    }
    
    # Return formatted JSON response
    response = HttpResponse(
        json.dumps(debug_info, indent=2),
        content_type='application/json'
    )
    return response

@csrf_exempt
def bookmark_add_image(request, bookmark_id):
    """API endpoint to add an image to a bookmark"""
    if request.method == 'POST':
        try:
            # Get the bookmark
            bookmark = get_object_or_404(MapBookmark, id=bookmark_id)
            
            # Check if user owns the bookmark or it's a public bookmark
            if request.user.is_authenticated:
                if bookmark.user and bookmark.user != request.user:
                    return JsonResponse({
                        'success': False,
                        'message': 'You do not have permission to add images to this bookmark'
                    }, status=403)
            elif bookmark.user is not None:
                return JsonResponse({
                    'success': False,
                    'message': 'You must be logged in to add images to this bookmark'
                }, status=401)
            
            # Process the image data
            data = json.loads(request.body)
            image_data = data.get('image')  # Base64 encoded image
            title = data.get('title', '')
            description = data.get('description', '')
            
            print(f"Received image upload request for bookmark {bookmark_id}")
            print(f"Image data length: {len(image_data) if image_data else 'None'}")
            
            # Convert base64 to image file
            if image_data:
                try:
                    # Handle data URI format (data:image/jpeg;base64,...)
                    if ',' in image_data:
                        format_info, image_data = image_data.split(',', 1)
                        print(f"Image format: {format_info}")
                    
                    # Decode the base64 data
                    image_content = ContentFile(base64.b64decode(image_data))
                    
                    # Create the bookmark image
                    bookmark_image = BookmarkImage(
                        bookmark=bookmark,
                        title=title,
                        description=description
                    )
                    
                    # Save the image file with a unique name
                    import uuid
                    file_name = f"bookmark_{bookmark.id}_{uuid.uuid4().hex}.jpg"
                    bookmark_image.image.save(file_name, image_content, save=True)
                    
                    # Make sure image URL is absolute
                    image_url = request.build_absolute_uri(bookmark_image.image.url)
                    
                    return JsonResponse({
                        'success': True,
                        'id': bookmark_image.id,
                        'message': 'Image added successfully',
                        'image_url': image_url
                    })
                except Exception as e:
                    print(f"Error processing image: {str(e)}")
                    return JsonResponse({
                        'success': False,
                        'message': f'Error processing image: {str(e)}'
                    }, status=400)
            else:
                return JsonResponse({
                    'success': False,
                    'message': 'No image data provided'
                }, status=400)
                
        except Exception as e:
            import traceback
            print(f"Error adding image: {str(e)}")
            print(traceback.format_exc())
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_delete_image(request, image_id):
    """API endpoint to delete a bookmark image"""
    if request.method == 'POST':
        try:
            # Get the image
            image = get_object_or_404(BookmarkImage, id=image_id)
            bookmark = image.bookmark
            
            # Check if user owns the bookmark or it's a public bookmark
            if request.user.is_authenticated:
                if bookmark.user and bookmark.user != request.user:
                    return JsonResponse({
                        'success': False,
                        'message': 'You do not have permission to delete this image'
                    }, status=403)
            elif bookmark.user is not None:
                return JsonResponse({
                    'success': False,
                    'message': 'You must be logged in to delete this image'
                }, status=401)
            
            # Delete the image file and record
            image.image.delete()
            image.delete()
            
            return JsonResponse({
                'success': True,
                'message': 'Image deleted successfully'
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_get_images(request, bookmark_id):
    """API endpoint to get all images for a bookmark"""
    if request.method == 'GET':
        try:
            # Get the bookmark
            bookmark = get_object_or_404(MapBookmark, id=bookmark_id)
            
            # Get all images for the bookmark
            images = bookmark.images.all()
            
            # Prepare the response data
            data = [{
                'id': image.id,
                'title': image.title,
                'description': image.description,
                'url': request.build_absolute_uri(image.image.url),
                'upload_date': image.upload_date.isoformat()
            } for image in images]
            
            return JsonResponse({
                'success': True,
                'images': data
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

# Profile update functions
@require_http_methods(["GET"])
def get_profile_data(request):
    if request.user.is_authenticated:
        user = request.user
        return JsonResponse({
            'success': True,
            'user': {
                'first_name': user.first_name,
                'middle_initial': user.middle_initial,
                'last_name': user.last_name,
                'country_of_origin': user.country_of_origin,
                'city': user.city,
                'phone_number': user.phone_number,
                'age': user.age,
                'company_name': user.company_name,
                'sex': user.sex,
            }
        })
    return JsonResponse({'success': False, 'message': 'User not authenticated'})

@require_http_methods(["POST"])
def update_profile(request):
    if request.user.is_authenticated:
        user = request.user
        errors = {}
        
        # Basic validation
        if not request.POST.get('first_name'):
            errors['first_name'] = 'First name is required'
        
        if not request.POST.get('last_name'):
            errors['last_name'] = 'Last name is required'
        
        if not request.POST.get('country_of_origin'):
            errors['country_of_origin'] = 'Country of origin is required'
        
        if not request.POST.get('city'):
            errors['city'] = 'City is required'
        
        if not request.POST.get('phone_number'):
            errors['phone_number'] = 'Phone number is required'
        
        if not request.POST.get('sex'):
            errors['sex'] = 'Please select your sex'
        
        # If we have errors, return them
        if errors:
            return JsonResponse({
                'success': False,
                'errors': errors
            })
        
        # If validation passes, update the user
        try:
            user.first_name = request.POST.get('first_name')
            user.middle_initial = request.POST.get('middle_initial')
            user.last_name = request.POST.get('last_name')
            user.country_of_origin = request.POST.get('country_of_origin')
            user.city = request.POST.get('city')
            user.phone_number = request.POST.get('phone_number')
            
            # Handle optional fields
            age = request.POST.get('age')
            if age:
                user.age = int(age)
            
            # Handle company_name as optional
            company_name = request.POST.get('company_name')
            if company_name:
                user.company_name = company_name
            else:
                user.company_name = None
                
            user.sex = request.POST.get('sex')
            
            if 'picture' in request.FILES:
                user.picture = request.FILES['picture']
                
            user.save()
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            })
    
    return JsonResponse({'success': False, 'message': 'User not authenticated'})

# Language-related views
def set_language_view(request, lang_code):
    """View to set language preference"""
    if lang_code not in ['en', 'tl', 'ceb', 'es']:
        lang_code = 'en'
        
    # Set language in session
    set_language(request, lang_code)
    
    # Return JSON response for AJAX calls
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'language': lang_code})
        
    # Otherwise redirect to referer or home
    referer = request.META.get('HTTP_REFERER', '/')
    return redirect(referer)

@require_http_methods(["GET"])
def get_translations_view(request, lang_code):
    """API endpoint to get all translations for a language as JSON"""
    if lang_code not in ['en', 'tl', 'ceb', 'es']:
        lang_code = 'en'
    
    # Return all translations as JSON
    return JsonResponse({
        'success': True,
        'language': lang_code,
        'translations': json.loads(get_translations_json(lang_code))
    })

@login_required
def cancel_booking(request):
    """Handle booking cancellation via AJAX"""
    if request.method == 'POST':
        booking_id = request.POST.get('booking_id')
        booking_type = request.POST.get('booking_type', 'tour')
        cancellation_reason = request.POST.get('cancellation_reason', '')
        
        # Validate input
        if not booking_id:
            return JsonResponse({'success': False, 'message': 'Booking ID is required'})
        
        try:
            # Handle different booking types
            if booking_type == 'pending':
                # Handle Pending model bookings
                booking = get_object_or_404(Pending, id=booking_id, guest_id=request.user)
                booking.status = 'Cancelled'
                booking.cancellation_reason = cancellation_reason
                booking.save()
            else:
                # Handle TourBooking model bookings
                booking = get_object_or_404(TourBooking, booking_id=booking_id, guest=request.user)
                booking.status = 'cancelled'
                booking.cancellation_reason = cancellation_reason
                booking.save()
            
            # You could also add email notification to staff here
            
            return JsonResponse({
                'success': True, 
                'message': 'Booking cancelled successfully'
            })
            
        except Exception as e:
            return JsonResponse({
                'success': False, 
                'message': f'Error cancelling booking: {str(e)}'
            })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})

# Placeholder functions for tour details endpoints
# Uncomment these when you uncomment the URLs in urls.py

# @login_required
# def get_tour_payables(request):
#     """Get detailed payable information for a tour"""
#     tour_id = request.GET.get('tour_id')
#     sched_id = request.GET.get('sched_id')
#     
#     if not tour_id or not sched_id:
#         return JsonResponse({
#             'success': False, 
#             'message': 'Missing tour_id or sched_id parameter'
#         })
#     
#     try:
#         # Get the tour and schedule
#         tour = get_object_or_404(Tour_Add, tour_id=tour_id)
#         schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
#         
#         # Get admission rates for this tour
#         admission_rates = Admission_Rates.objects.filter(
#             tour_id=tour
#         ).order_by('age_group')
#         
#         # Format the admission rates for display
#         rates = []
#         for rate in admission_rates:
#             rates.append({
#                 'age_group': rate.age_group,
#                 'rate': rate.rate,
#                 'description': rate.description
#             })
#         
#         # Get any other payable items for this tour
#         # Add tour add-ons or additional costs here
#         
#         return JsonResponse({
#             'success': True,
#             'tour_name': tour.tour_name,
#             'base_price': schedule.price,
#             'admission_rates': rates,
#             # Add other payables here
#         })
#         
#     except Exception as e:
#         return JsonResponse({
#             'success': False,
#             'message': str(e)
#         })
# 
# @login_required
# def get_tour_itinerary(request):
#     """Get detailed itinerary information for a tour"""
#     tour_id = request.GET.get('tour_id')
#     sched_id = request.GET.get('sched_id')
#     
#     if not tour_id or not sched_id:
#         return JsonResponse({
#             'success': False, 
#             'message': 'Missing tour_id or sched_id parameter'
#         })
#     
#     try:
#         # Get the tour and schedule
#         tour = get_object_or_404(Tour_Add, tour_id=tour_id)
#         schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
#         
#         # Get tour events for this schedule
#         tour_events = Tour_Event.objects.filter(
#             tour_id=tour,
#             sched_id=schedule
#         ).order_by('day_number', 'start_time')
#         
#         # Format the itinerary for display
#         days = {}
#         for event in tour_events:
#             day_number = event.day_number
#             if day_number not in days:
#                 days[day_number] = []
#             
#             days[day_number].append({
#                 'title': event.title,
#                 'description': event.description,
#                 'start_time': event.start_time.strftime('%I:%M %p') if event.start_time else None,
#                 'end_time': event.end_time.strftime('%I:%M %p') if event.end_time else None,
#                 'location': event.location,
#                 'notes': event.notes
#             })
#         
#         # Build HTML for the itinerary
#         itinerary_html = ''
#         for day_number in sorted(days.keys()):
#             itinerary_html += f'<div class="itinerary-day"><h4>Day {day_number}</h4><ul>'
#             for event in days[day_number]:
#                 time_display = ''
#                 if event['start_time']:
#                     time_display = event['start_time']
#                     if event['end_time']:
#                         time_display += f' - {event["end_time"]}'
#                         
#                 itinerary_html += f'<li><strong>{time_display}</strong> - {event["title"]}'
#                 if event['description']:
#                     itinerary_html += f'<br><span class="event-description">{event["description"]}</span>'
#                 itinerary_html += '</li>'
#             itinerary_html += '</ul></div>'
#         
#         if not itinerary_html:
#             itinerary_html = '<p>No detailed itinerary available for this tour.</p>'
#         
#         return JsonResponse({
#             'success': True,
#             'tour_name': tour.tour_name,
#             'itinerary_html': itinerary_html
#         })
#         
#     except Exception as e:
#         return JsonResponse({
#             'success': False,
#             'message': str(e)
#         })

@login_required
def companion_view(request):
    """View for managing companions"""
    # Get current language
    current_language = get_current_language(request)
    from .forms import CompanionForm
    from .models import Guest, GuestCredential, DisabilityDocument, CompanionGroup, CompanionRequest
    
    # Get existing companions for this user
    companions = Guest.objects.filter(made_by=request.user).select_related('group')
    
    # Get user's companion groups
    groups = CompanionGroup.objects.filter(owner=request.user)
    
    # Organize companions by group for better display
    organized_companions = {
        'no_group': [],
        'by_group': {}
    }
    
    # Initialize groups in the organized structure
    for group in groups:
        organized_companions['by_group'][group.id] = {
            'group': group,
            'companions': []
        }
    
    # Organize companions into their groups
    for companion in companions:
        if companion.group:
            # Add to appropriate group
            group_id = companion.group.id
            if group_id in organized_companions['by_group']:
                organized_companions['by_group'][group_id]['companions'].append(companion)
        else:
            # Add to "no group" list
            organized_companions['no_group'].append(companion)
    
    # Get all group members counts for display in a format that can be directly used in templates
    group_counts = {}
    for group in groups:
        # Use string keys for the dictionary to ensure it works in the template
        group_counts[str(group.id)] = companions.filter(group=group).count()
    
    # Get friend connections (users with accepted companion requests)
    sent_friend_requests = CompanionRequest.objects.filter(
        sender=request.user, 
        status='accepted'
    ).select_related('recipient', 'group')
    
    received_friend_requests = CompanionRequest.objects.filter(
        recipient=request.user, 
        status='accepted'
    ).select_related('sender', 'group')
    
    # Create a list of friend connections
    friends = []
    
    # Add recipients of accepted sent requests
    for req in sent_friend_requests:
        friends.append({
            'user': req.recipient,
            'request_id': req.id,
            'created_at': req.created_at,
            'direction': 'sent',
            'group': req.group
        })
    
    # Add senders of accepted received requests
    for req in received_friend_requests:
        friends.append({
            'user': req.sender,
            'request_id': req.id,
            'created_at': req.created_at,
            'direction': 'received',
            'group': req.group
        })
    
    # Sort friends by name
    friends.sort(key=lambda x: f"{x['user'].first_name} {x['user'].last_name}")
    
    # Create dictionary to track all groups, including those from connections
    all_groups = {}
    for group in groups:
        all_groups[group.id] = group
    
    # Collect any groups from friend connections that aren't user's own groups
    for friend in friends:
        if friend['group'] and friend['group'].id not in all_groups:
            all_groups[friend['group'].id] = friend['group']
            print(f"Added external group from connections: {friend['group'].name} (ID: {friend['group'].id})")
    
    # Debug the groups
    print(f"Found {len(all_groups)} total groups for organizing friends")
    for group_id, group in all_groups.items():
        print(f"Group: {group.name} (ID: {group_id})")
    
    # Organize friends by group for better display
    organized_friends = {
        'no_group': [],
        'by_group': {}
    }
    
    # Initialize all groups in the organized structure for friends
    for group_id, group in all_groups.items():
        organized_friends['by_group'][group_id] = {
            'group': group,
            'friends': []
        }
    
    # Organize friends into their groups
    for friend in friends:
        print(f"Processing friend: {friend['user'].first_name} with group: {friend['group'].name if friend['group'] else 'None'}")
        if friend['group']:
            # Add to appropriate group
            group_id = friend['group'].id
            if group_id in organized_friends['by_group']:
                organized_friends['by_group'][group_id]['friends'].append(friend)
                print(f"Added to group: {friend['group'].name}")
            else:
                # Create entry for this group if it doesn't exist
                organized_friends['by_group'][group_id] = {
                    'group': friend['group'],
                    'friends': [friend]
                }
                print(f"Created new group entry for: {friend['group'].name}")
        else:
            # Add to "no group" list
            organized_friends['no_group'].append(friend)
            print(f"Added to no_group list")
    
    # Handle group creation
    if request.method == 'POST' and 'create_group' in request.POST:
        group_name = request.POST.get('group_name')
        group_description = request.POST.get('group_description')
        
        if group_name:
            new_group = CompanionGroup.objects.create(
                name=group_name,
                description=group_description,
                owner=request.user
            )
            messages.success(request, f'Group "{group_name}" created successfully!')
            return redirect('companion')
    
    # Handle companion form submission
    elif request.method == 'POST':
        form = CompanionForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                # Save the companion with the current user as made_by
                companion = form.save(commit=False)
                
                # Convert MM/DD/YY format to a proper date object
                birthday_str = form.cleaned_data.get('birthday')
                if birthday_str and isinstance(birthday_str, str) and '/' in birthday_str:
                    try:
                        # Parse MM/DD/YY format
                        from datetime import datetime
                        month, day, year = birthday_str.split('/')
                        # Assuming YY format, convert to 4-digit year (assuming 20xx for years less than 50)
                        if len(year) == 2:
                            year = f"20{year}" if int(year) < 50 else f"19{year}"
                        companion.birthday = datetime.strptime(f"{month}/{day}/{year}", "%m/%d/%Y").date()
                    except (ValueError, IndexError) as e:
                        # If parsing fails, try using the original value
                        print(f"Error parsing birthday: {e}")
                        companion.birthday = form.cleaned_data.get('birthday')
                else:
                    # Use the original value if not in MM/DD/YY format
                    companion.birthday = form.cleaned_data.get('birthday')
                
                # Handle disability fields
                companion.has_disability = form.cleaned_data.get('has_disability', False)
                if companion.has_disability:
                    companion.disability_type = form.cleaned_data.get('disability_type', '')
                
                # Set made_by field to current user
                companion.made_by = request.user
                
                # Assign to group if specified
                group_id = request.POST.get('companion_group')
                if group_id and group_id != 'none':
                    try:
                        group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                        companion.group = group
                    except CompanionGroup.DoesNotExist:
                        pass  # Ignore if group doesn't exist or doesn't belong to user
                else:
                    companion.group = None
                
                # Save the companion to create the instance with an ID
                companion.save()
                
                # Process and save credentials (multiple files)
                credentials = request.FILES.getlist('credentials')
                for credential_file in credentials:
                    GuestCredential.objects.create(
                        guest=companion,
                        document=credential_file
                    )
                
                # Process and save disability documents if has_disability is checked
                if companion.has_disability:
                    disability_documents = form.cleaned_data.get('disability_documents')
                    if disability_documents:
                        # Handle both single file and list of files
                        if not isinstance(disability_documents, list):
                            disability_documents = [disability_documents]
                        
                        for doc_file in disability_documents:
                            DisabilityDocument.objects.create(
                                guest=companion,
                                document=doc_file
                            )
                
                messages.success(request, 'Companion added successfully!')
                
                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': 'Companion added successfully!'
                    })
                return redirect('companion')
            except Exception as e:
                messages.error(request, f'Error adding companion: {str(e)}')
        else:
            # Form is invalid
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CompanionForm()
    
    # Get pending request count
    pending_request_count = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).count()
    
    context = {
        'user': request.user,
        'companions': companions,  # Keep the original queryset for backward compatibility
        'organized_companions': organized_companions,  # New organized structure
        'groups': groups,
        'group_counts': group_counts,
        'friends': friends,
        'organized_friends': organized_friends,  # New organized friends structure
        'form': form,
        'pending_request_count': pending_request_count,
        'current_language': current_language,
        'translations_json': get_translations_json(current_language)
    }
    
    return render(request, 'companion.html', context)

@login_required
def edit_companion(request, companion_id):
    """View for editing a companion's information"""
    from .forms import CompanionForm
    from .models import Guest, GuestCredential, DisabilityDocument, CompanionGroup
    
    # Get the companion
    try:
        companion = Guest.objects.get(guest_id=companion_id)
        
        # Check if the user is the owner of this companion
        if companion.made_by != request.user:
            messages.error(request, "You don't have permission to edit this companion.")
            return redirect('companion')
        
        # Get user's companion groups
        groups = CompanionGroup.objects.filter(owner=request.user)
            
        # Handle form submission
        if request.method == 'POST':
            form = CompanionForm(request.POST, request.FILES, instance=companion)
            if form.is_valid():
                try:
                    # Save the updated companion
                    updated_companion = form.save(commit=False)
                    
                    # Ensure made_by field remains the same
                    updated_companion.made_by = request.user
                    
                    # Convert MM/DD/YY format to a proper date object
                    birthday_str = form.cleaned_data.get('birthday')
                    if birthday_str and isinstance(birthday_str, str) and '/' in birthday_str:
                        try:
                            # Parse MM/DD/YY format
                            from datetime import datetime
                            month, day, year = birthday_str.split('/')
                            # Assuming YY format, convert to 4-digit year (assuming 20xx for years less than 50)
                            if len(year) == 2:
                                year = f"20{year}" if int(year) < 50 else f"19{year}"
                            updated_companion.birthday = datetime.strptime(f"{month}/{day}/{year}", "%m/%d/%Y").date()
                        except (ValueError, IndexError) as e:
                            # If parsing fails, try using the original value
                            print(f"Error parsing birthday: {e}")
                            updated_companion.birthday = form.cleaned_data.get('birthday')
                    else:
                        # Use the original value if not in MM/DD/YY format
                        updated_companion.birthday = form.cleaned_data.get('birthday')
                    
                    updated_companion.has_disability = form.cleaned_data.get('has_disability', False)
                    if updated_companion.has_disability:
                        updated_companion.disability_type = form.cleaned_data.get('disability_type', '')
                    
                    # Update group assignment if specified
                    group_id = request.POST.get('companion_group')
                    if group_id == 'none':
                        updated_companion.group = None
                    elif group_id:
                        try:
                            group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                            updated_companion.group = group
                        except CompanionGroup.DoesNotExist:
                            pass  # Ignore if group doesn't exist or doesn't belong to user
                    
                    # Save the updated companion
                    updated_companion.save()
                    
                    # Process new credentials if provided
                    credentials = request.FILES.getlist('credentials')
                    if credentials:
                        for credential_file in credentials:
                            GuestCredential.objects.create(
                                guest=updated_companion,
                                document=credential_file
                            )
                    
                    # Process new disability documents if provided
                    if updated_companion.has_disability:
                        disability_documents = form.cleaned_data.get('disability_documents')
                        if disability_documents:
                            if not isinstance(disability_documents, list):
                                disability_documents = [disability_documents]
                            
                            for doc_file in disability_documents:
                                DisabilityDocument.objects.create(
                                    guest=updated_companion,
                                    document=doc_file
                                )
                    
                    messages.success(request, 'Companion updated successfully!')
                    return redirect('companion')
                except Exception as e:
                    messages.error(request, f'Error updating companion: {str(e)}')
            else:
                messages.error(request, 'Please correct the errors below.')
        else:
            # Pre-fill the form with companion data
            form = CompanionForm(instance=companion)
        
        context = {
            'form': form,
            'companion': companion,
            'groups': groups,
            'editing': True,
            'current_language': get_current_language(request),
            'translations_json': get_translations_json(get_current_language(request))
        }
        
        return render(request, 'companion_edit.html', context)
        
    except Guest.DoesNotExist:
        messages.error(request, "Companion not found.")
        return redirect('companion')

@login_required
def manage_companion_groups(request):
    """View for managing companion groups"""
    from .models import CompanionGroup, Guest
    
    # Get user's groups
    groups = CompanionGroup.objects.filter(owner=request.user)
    
    if request.method == 'POST':
        # Handle group creation
        if 'create_group' in request.POST:
            group_name = request.POST.get('group_name')
            group_description = request.POST.get('group_description')
            
            if group_name:
                new_group = CompanionGroup.objects.create(
                    name=group_name,
                    description=group_description,
                    owner=request.user
                )
                messages.success(request, f'Group "{group_name}" created successfully!')
                
        # Handle group deletion
        elif 'delete_group' in request.POST:
            group_id = request.POST.get('group_id')
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                group_name = group.name
                group.delete()
                messages.success(request, f'Group "{group_name}" deleted successfully!')
            except CompanionGroup.DoesNotExist:
                messages.error(request, "Group not found or you don't have permission to delete it.")
        
        # Handle group editing
        elif 'edit_group' in request.POST:
            group_id = request.POST.get('group_id')
            group_name = request.POST.get('group_name')
            group_description = request.POST.get('group_description')
            
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                if group_name:
                    group.name = group_name
                if group_description is not None:  # Allow empty description
                    group.description = group_description
                group.save()
                messages.success(request, f'Group "{group_name}" updated successfully!')
            except CompanionGroup.DoesNotExist:
                messages.error(request, "Group not found or you don't have permission to edit it.")
        
        return redirect('manage_companion_groups')
    
    context = {
        'groups': groups,
        'companions_count': {
            group.id: Guest.objects.filter(group=group).count() 
            for group in groups
        },
        'current_language': get_current_language(request),
        'translations_json': get_translations_json(get_current_language(request))
    }
    
    return render(request, 'manage_companion_groups.html', context)

@login_required
@require_http_methods(["POST"])
def delete_companion(request, companion_id):
    """Handle companion deletion"""
    try:
        # Get the companion
        companion = get_object_or_404(Guest, guest_id=companion_id)
        
        # Check if the user is the owner of this companion
        if companion.made_by != request.user:
            return JsonResponse({
                'success': False,
                'message': "You don't have permission to delete this companion."
            }, status=403)
        
        # Delete the companion
        companion_name = f"{companion.first_name} {companion.last_name}"
        companion.delete()
        
        # Return success response
        return JsonResponse({
            'success': True,
            'message': f'Companion {companion_name} has been deleted successfully.'
        })
        
    except Guest.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Companion not found.'
        }, status=404)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f'Error deleting companion: {str(e)}'
        }, status=400)

# Companion Request Views
@login_required
def search_users(request):
    """Search for users by email to send companion requests"""
    email = request.GET.get('email', '').strip()
    
    if not email:
        return JsonResponse({
            'success': False,
            'message': 'Please enter an email to search.'
        })
    
    try:
        # Find user by exact email match (for security reasons)
        # Ensure we only find regular users (not companions)
        user = Guest.objects.filter(
            email=email, 
            made_by__isnull=True  # This ensures we only get regular users, not companions
        ).first()
        
        if not user:
            return JsonResponse({
                'success': False,
                'message': 'No registered user found with this email address.'
            })
        
        # Don't allow searching for yourself
        if user == request.user:
            return JsonResponse({
                'success': False,
                'message': 'You cannot send a companion request to yourself.'
            })
            
        # Check if there's already a request between these users
        from .models import CompanionRequest
        
        # Check more specifically for the relationship direction
        # Only check if there's a sent request from current user to found user
        existing_sent_request = CompanionRequest.objects.filter(
            sender=request.user, recipient=user
        ).first()
        
        # Check if there's a received request from found user to current user
        existing_received_request = CompanionRequest.objects.filter(
            sender=user, recipient=request.user
        ).first()
        
        # Handle case of existing sent request
        if existing_sent_request:
            if existing_sent_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'You have already sent a request to this user. Please wait for their response.'
                })
            elif existing_sent_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            # If declined, we'll allow them to send a new request
        
        # Handle case of existing received request
        if existing_received_request:
            if existing_received_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'This user has already sent you a request. Please check your companion requests.'
                })
            elif existing_received_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            # If declined, we'll allow them to receive a new request
            
        # Check if the user is already a companion of the current user
        # More explicitly check for companion relationship with matching first/last name (not just email)
        is_companion = Guest.objects.filter(
            made_by=request.user, 
            email=email,
            first_name=user.first_name,
            last_name=user.last_name
        ).exists()
        
        if is_companion:
            return JsonResponse({
                'success': False,
                'message': 'This user is already in your companions list.'
            })
            
        # Return user info for confirmation
        picture_url = user.picture.url if user.picture else None
        return JsonResponse({
            'success': True,
            'user': {
                'guest_id': user.guest_id,
                'name': f"{user.first_name} {user.last_name}",
                'first_name': user.first_name,
                'last_name': user.last_name,
                'email': user.email,
                'picture': picture_url
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f'Error searching for user: {str(e)}'
        })

@login_required
def debug_companion_requests(request):
    """Debug view to help troubleshoot companion request issues"""
    if not request.user.is_staff:
        messages.error(request, "You don't have permission to access this page.")
        return redirect('companion')
    
    email = request.GET.get('email', '').strip()
    results = {}
    
    if email:
        from .models import CompanionRequest
        try:
            # Find the user
            user = Guest.objects.filter(email=email).first()
            if user:
                results['user_found'] = {
                    'guest_id': user.guest_id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email,
                    'is_companion': user.made_by is not None
                }
                
                # Check for sent requests
                sent_requests = CompanionRequest.objects.filter(
                    sender=request.user, recipient=user
                )
                results['sent_requests'] = [{
                    'id': req.id,
                    'status': req.status,
                    'created_at': req.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'updated_at': req.updated_at.strftime('%Y-%m-%d %H:%M:%S')
                } for req in sent_requests]
                
                # Check for received requests
                received_requests = CompanionRequest.objects.filter(
                    sender=user, recipient=request.user
                )
                results['received_requests'] = [{
                    'id': req.id,
                    'status': req.status,
                    'created_at': req.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'updated_at': req.updated_at.strftime('%Y-%m-%d %H:%M:%S')
                } for req in received_requests]
                
                # Check if the user is a companion of current user
                companion = Guest.objects.filter(
                    made_by=request.user,
                    email=email
                ).first()
                if companion:
                    results['is_companion'] = {
                        'guest_id': companion.guest_id,
                        'name': f"{companion.first_name} {companion.last_name}",
                        'email': companion.email
                    }
                else:
                    results['is_companion'] = False
            else:
                results['user_found'] = False
        except Exception as e:
            results['error'] = str(e)
    
    return JsonResponse(results)

@login_required
def send_companion_request(request):
    """Send a companion request to another user"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method.'})
    
    recipient_id = request.POST.get('recipient_id')
    message = request.POST.get('message', '')
    group_id = request.POST.get('group_id', '')
    
    print(f"Received companion request - recipient: {recipient_id}, group_id: {group_id}, message length: {len(message)}")
    
    if not recipient_id:
        return JsonResponse({'success': False, 'message': 'Recipient ID is required.'})
    
    try:
        from .models import CompanionRequest, CompanionGroup
        
        # Get the recipient
        recipient = get_object_or_404(Guest, guest_id=recipient_id)
        
        # Get the group if provided
        group = None
        if group_id:
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                print(f"Found group for request: {group.name} (ID: {group.id})")
            except CompanionGroup.DoesNotExist:
                print(f"Group not found with ID: {group_id}")
                # Continue without group rather than failing
        
        # Don't allow sending requests to yourself
        if recipient == request.user:
            return JsonResponse({
                'success': False,
                'message': 'You cannot send a companion request to yourself.'
            })
        
        # Check for existing requests
        existing_request = CompanionRequest.objects.filter(
            sender=request.user, recipient=recipient
        ).first()
        
        if existing_request:
            if existing_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'You have already sent a request to this user. Please wait for their response.'
                })
            elif existing_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            else:  # declined
                # Allow sending a new request if the previous one was declined
                existing_request.status = 'pending'
                existing_request.message = message
                existing_request.group = group
                existing_request.save()
                
                group_msg = f" (will be added to group '{group.name}')" if group else ""
                return JsonResponse({
                    'success': True,
                    'message': f'Your companion request to {recipient.first_name} has been sent{group_msg}.'
                })
        
        # Create new request
        new_request = CompanionRequest.objects.create(
            sender=request.user,
            recipient=recipient,
            message=message,
            group=group
        )
        
        print(f"Created new companion request: ID {new_request.id} with group: {group.name if group else 'None'}")
        
        group_msg = f" (will be added to group '{group.name}')" if group else ""
        return JsonResponse({
            'success': True,
            'message': f'Your companion request to {recipient.first_name} has been sent{group_msg}.'
        })
        
    except Exception as e:
        import traceback
        print(f"Error sending companion request: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({
            'success': False,
            'message': f'Error sending companion request: {str(e)}'
        })

@login_required
def list_companion_requests(request):
    """List all pending companion requests for the current user"""
    from .models import CompanionRequest
    
    # Get received requests
    received_requests = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).select_related('sender')
    
    # Get sent requests
    sent_requests = CompanionRequest.objects.filter(
        sender=request.user, status='pending'
    ).select_related('recipient')
    
    context = {
        'received_requests': received_requests,
        'sent_requests': sent_requests,
        'current_language': get_current_language(request),
        'translations_json': get_translations_json(get_current_language(request))
    }
    
    return render(request, 'companion_requests.html', context)

@login_required
def companion_request_count(request):
    """Get count of pending companion requests for the current user"""
    from .models import CompanionRequest
    
    count = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).count()
    
    return JsonResponse({
        'success': True,
        'count': count
    })

@login_required
def accept_companion_request(request, request_id):
    """Accept a companion request"""
    try:
        from .models import CompanionRequest, Guest, CompanionGroup
        
        # Get the request
        companion_request = get_object_or_404(CompanionRequest, id=request_id, recipient=request.user)
        
        # Check if request is pending
        if companion_request.status != 'pending':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': "This request has already been processed."
                })
            messages.error(request, "This request has already been processed.")
            return redirect('list_companion_requests')
        
        # Get the group if specified in the request
        group = None
        print(f"Request {request_id} has group: {companion_request.group}")
        if companion_request.group:
            try:
                # First try to get a group with the same name owned by the recipient
                matching_groups = CompanionGroup.objects.filter(
                    owner=request.user, 
                    name=companion_request.group.name
                )
                
                if matching_groups.exists():
                    # Use existing group with same name if found
                    group = matching_groups.first()
                    print(f"Using existing group with matching name: {group.name} (ID: {group.id})")
                else:
                    # If no matching group found, look up by ID
                    group = CompanionGroup.objects.get(id=companion_request.group.id, owner=request.user)
                    print(f"Found group by ID: {group.name} (ID: {group.id})")
            except CompanionGroup.DoesNotExist:
                print(f"Group not found with ID: {companion_request.group.id}")
                
                # Create a new group with the same name if it doesn't exist
                sender_group = companion_request.group
                if sender_group:
                    group = CompanionGroup.objects.create(
                        name=f"{sender_group.name} (from {companion_request.sender.first_name})",
                        description=f"Group created from connection with {companion_request.sender.first_name} {companion_request.sender.last_name}",
                        owner=request.user
                    )
                    print(f"Created new group: {group.name} (ID: {group.id})")
        
        # Accept the request
        companion_request.accept()
        print(f"Accepted companion request {request_id}")
        
        # Create companion relationship
        sender = companion_request.sender
        recipient = request.user
        
        # Check if companion already exists with this email
        existing_companion = Guest.objects.filter(
            made_by=request.user,
            email=sender.email
        ).first()
        
        if not existing_companion:
            # Create a new companion record only if one doesn't exist
            # Generate a unique email for the companion to avoid duplicate entry errors
            import uuid
            unique_suffix = uuid.uuid4().hex[:8]
            companion_email = f"{sender.email.split('@')[0]}+companion{unique_suffix}@{sender.email.split('@')[1]}"
            
            new_companion = Guest(
                first_name=sender.first_name,
                last_name=sender.last_name,
                email=companion_email,  # Use the unique email
                phone_number=sender.phone_number if hasattr(sender, 'phone_number') else '',
                made_by=request.user,
                group=group  # Assign the group directly
            )
            new_companion.save()
            
            # Double-check that the group was assigned
            if group:
                print(f"Created new companion with group: {group.name}")
                # Explicitly update the group relation in case it wasn't set properly
                new_companion.group = group
                new_companion.save(update_fields=['group'])
                print(f"Verified companion group assignment: {new_companion.group and new_companion.group.name}")
            
            companion = new_companion
        elif group:
            # If companion already exists but a group was specified in the request, update their group
            existing_companion.group = group
            existing_companion.save(update_fields=['group'])
            print(f"Updated existing companion with group: {group.name}")
            companion = existing_companion
        else:
            companion = existing_companion
            
        # *** NEW CODE - IMPORTANT: Update the sender's side to show the recipient in the correct group ***
        # Check if the recipient already exists as a companion in the sender's list
        sender_companion = Guest.objects.filter(
            made_by=sender,
            email=recipient.email
        ).first()
        
        # Get the original group that was specified in the request (from sender's side)
        original_group = companion_request.group
        
        if not sender_companion:
            # Create a new companion record for the recipient in the sender's account
            # This ensures the sender sees the recipient as a companion
            unique_suffix = uuid.uuid4().hex[:8]
            recipient_email = f"{recipient.email.split('@')[0]}+companion{unique_suffix}@{recipient.email.split('@')[1]}"
            
            new_sender_companion = Guest(
                first_name=recipient.first_name,
                last_name=recipient.last_name,
                email=recipient_email,
                phone_number=recipient.phone_number if hasattr(recipient, 'phone_number') else '',
                made_by=sender,
                group=original_group  # Use the ORIGINAL group from the request
            )
            new_sender_companion.save()
            print(f"Created recipient companion in sender's list with group: {original_group.name if original_group else 'None'}")
        elif original_group:
            # If recipient already exists in sender's companions but the group needs updating
            sender_companion.group = original_group
            sender_companion.save(update_fields=['group'])
            print(f"Updated recipient in sender's companions with original group: {original_group.name}")
        
        # Update the CompanionRequest to keep the group association for both sides
        if group and companion_request.group != group:
            # Keep the original group in the request
            print(f"Keeping original group in the request: {companion_request.group.name if companion_request.group else 'None'}")
        
        # If this is an AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            group_message = f" and added to group '{group.name}'" if group else ""
            return JsonResponse({
                'success': True,
                'message': f"You are now connected with {sender.first_name} {sender.last_name}{group_message}.",
                'companion_id': companion.guest_id,
                'group_id': group.id if group else None,
                'group_name': group.name if group else None
            })
            
        group_message = f" and added to group '{group.name}'" if group else ""
        messages.success(request, f"You are now connected with {sender.first_name} {sender.last_name}{group_message}.")
        return redirect('list_companion_requests')
        
    except Exception as e:
        import traceback
        print(f"Error accepting companion request: {str(e)}")
        print(traceback.format_exc())
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f"Error accepting companion request: {str(e)}"
            })
        messages.error(request, f"Error accepting companion request: {str(e)}")
        return redirect('list_companion_requests')

@login_required
def decline_companion_request(request, request_id):
    """Decline a companion request"""
    from .models import CompanionRequest
    
    try:
        companion_request = get_object_or_404(
            CompanionRequest, id=request_id, recipient=request.user, status='pending'
        )
        
        sender_name = companion_request.sender.first_name
        companion_request.decline()
        
        # If this is an AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': f'You have declined the companion request from {sender_name}.'
            })
        
        messages.success(request, f'You have declined the companion request from {sender_name}.')
        return redirect('list_companion_requests')
    
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f'Error declining companion request: {str(e)}'
            })
        
        messages.error(request, f'Error declining companion request: {str(e)}')
        return redirect('list_companion_requests')

@login_required
def fix_companion_request(request):
    """Admin function to fix problematic companion requests or remove friend connections"""
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Invalid request method.'
        }, status=405)
    
    # Get parameters
    email = request.POST.get('email', '').strip()
    action = request.POST.get('action', '')
    request_id = request.POST.get('request_id', None)
    delete_type = request.POST.get('delete_type', 'all')
    
    try:
        from .models import CompanionRequest, Guest
        
        # Allow normal users to delete specific requests (their own connections)
        if action == 'delete' and request_id and delete_type == 'specific':
            try:
                # Find the specific request
                request_obj = CompanionRequest.objects.get(id=request_id)
                
                # Only allow if user is a participant in this request
                if request.user == request_obj.sender or request.user == request_obj.recipient:
                    request_obj.delete()
                    return JsonResponse({
                        'success': True,
                        'message': 'Connection has been removed successfully.'
                    })
                else:
                    return JsonResponse({
                        'success': False,
                        'message': "You don't have permission to delete this connection."
                    }, status=403)
            except CompanionRequest.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'message': 'Connection not found.'
                }, status=404)
        
        # All other actions require staff permissions
        if not request.user.is_staff:
            return JsonResponse({
                'success': False,
                'message': "You don't have permission to access this feature."
            }, status=403)
        
        if not email or not action:
            return JsonResponse({
                'success': False,
                'message': 'Missing required parameters.'
            }, status=400)
        
        # Find the user
        user = Guest.objects.filter(email=email, made_by__isnull=True).first()
        if not user:
            return JsonResponse({
                'success': False,
                'message': 'User not found or is already a companion account.'
            }, status=404)
        
        # Handle different actions
        if action == 'delete':
            # Delete companion requests between these users
            if request_id:
                # Delete specific request
                request_obj = get_object_or_404(CompanionRequest, id=request_id)
                request_obj.delete()
                message = f"Companion request #{request_id} deleted."
            else:
                # Delete all requests between the users
                sent_count = CompanionRequest.objects.filter(
                    sender=request.user, recipient=user
                ).delete()[0]
                
                received_count = CompanionRequest.objects.filter(
                    sender=user, recipient=request.user
                ).delete()[0]
                
                message = f"Deleted {sent_count + received_count} companion requests."
            
        elif action == 'reset':
            # Reset companion request status to 'pending'
            if request_id:
                # Reset specific request
                request_obj = get_object_or_404(CompanionRequest, id=request_id)
                request_obj.status = 'pending'
                request_obj.save()
                message = f"Companion request #{request_id} reset to 'pending'."
            else:
                # Reset all requests between the users to pending
                sent_updated = 0
                for req in CompanionRequest.objects.filter(sender=request.user, recipient=user):
                    req.status = 'pending'
                    req.save()
                    sent_updated += 1
                
                received_updated = 0
                for req in CompanionRequest.objects.filter(sender=user, recipient=request.user):
                    req.status = 'pending'
                    req.save()
                    received_updated += 1
                
                message = f"Reset {sent_updated + received_updated} companion requests to 'pending'."
            
        elif action == 'create-companion':
            # Create a companion relationship directly
            # Check if companion already exists
            existing_companion = Guest.objects.filter(
                made_by=request.user,
                email=user.email
            ).first()
            
            if existing_companion:
                message = f"Companion already exists for {user.first_name} {user.last_name}."
            else:
                # Create new companion
                companion = Guest.objects.create(
                    first_name=user.first_name,
                    middle_initial=user.middle_initial,
                    last_name=user.last_name,
                    email=user.email,
                    phone_number=user.phone_number,
                    country_of_origin=user.country_of_origin,
                    city=user.city,
                    company_name=user.company_name,
                    sex=user.sex,
                    has_disability=user.has_disability,
                    disability_type=user.disability_type,
                    picture=user.picture,
                    made_by=request.user,
                    birthday=user.birthday
                )
                
                message = f"Created companion for {user.first_name} {user.last_name}."
        else:
            return JsonResponse({
                'success': False,
                'message': f"Unknown action: {action}"
            }, status=400)
        
        return JsonResponse({
            'success': True,
            'message': message
        })
    
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f"Error: {str(e)}"
        }, status=500)

@login_required
def companion_group_debug(request):
    """Debug view for companion group relationships"""
    from .models import Guest, CompanionGroup, CompanionRequest, FriendGroup
    
    # Get data to debug
    user = request.user
    guest = Guest.objects.get(guest_id=user.guest_id)
    
    # Owned groups
    owned_groups = CompanionGroup.objects.filter(owner=guest)
    
    # Member of groups
    member_groups = CompanionGroup.objects.filter(members=guest)
    
    # Friend groups
    friend_groups = FriendGroup.objects.filter(members=guest)
    
    # Companion requests
    sent_requests = CompanionRequest.objects.filter(sender=guest)
    received_requests = CompanionRequest.objects.filter(recipient=guest)
    
    # Direct companions
    direct_companions = Guest.objects.filter(made_by=guest)
    
    context = {
        'user': user,
        'guest': guest,
        'owned_groups': owned_groups,
        'member_groups': member_groups,
        'friend_groups': friend_groups,
        'sent_requests': sent_requests,
        'received_requests': received_requests,
        'direct_companions': direct_companions,
    }
    
    return render(request, 'companion_group_debug.html', context)


@login_required
def friendship_debug(request):
    """Debug view to see friendship connections for the current user"""
    user = request.user
    
    # Get friendships for the current user
    try:
        friendships = Friendship.objects.filter(user=user).select_related('friend')
        friendship_data = []
        
        for friendship in friendships:
            friend = friendship.friend
            friendship_data.append({
                'friend_id': friend.guest_id,
                'friend_name': f"{friend.first_name} {friend.last_name}",
                'group': friendship.group_name,
                'created': friendship.created_at.strftime('%Y-%m-%d'),
            })
        
        # Get friendships data
        friend_count = len(friendship_data)
        group_counts = {}
        for item in friendship_data:
            group = item['group']
            if group not in group_counts:
                group_counts[group] = 0
            group_counts[group] += 1
        
        # Get data from legacy methods for comparison
        legacy_companions = get_companions_legacy(user)
        legacy_count = len(legacy_companions)
        
        # Create diagnostic result
        result = {
            'success': True,
            'user': f"{user.first_name} {user.last_name}",
            'friendship_count': friend_count,
            'groups': group_counts,
            'friendships': friendship_data,
            'legacy_count': legacy_count,
        }
        
        # Option to repopulate friendships
        if request.GET.get('repopulate') == 'true':
            from .utils import populate_friendships
            new_count = populate_friendships()
            result['repopulated'] = True
            result['new_friendship_count'] = new_count
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        result = {
            'success': False,
            'error': str(e)
        }
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse(result)
    else:
        # Render a debug template
        return render(request, 'friendship_debug.html', {
            'result': result,
            'result_json': json.dumps(result, indent=2),
        })

@login_required
def get_companions(request):
    """API endpoint to get companions for the current user"""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            user = request.user
            if not hasattr(user, 'guest_id'):
                return JsonResponse({'success': False, 'message': 'User is not associated with a guest profile'}, status=400)
            
            guest = Guest.objects.get(guest_id=user.guest_id)
            all_companions = []
            
            # Simple direct query using the new Friendship model
            try:
                # Get all friendships for this user
                friendships = Friendship.objects.filter(user=guest).select_related('friend')
                
                # Group by relationship type
                friendship_groups = {}
                for friendship in friendships:
                    group_name = friendship.group_name
                    if group_name not in friendship_groups:
                        friendship_groups[group_name] = []
                    
                    friend = friendship.friend
                    friendship_groups[group_name].append({
                        'guest_id': friend.guest_id,
                        'first_name': friend.first_name,
                        'last_name': friend.last_name,
                        'age': friend.age,
                        'age_label': friend.age_label,
                        'group_name': group_name,
                        'picture_url': friend.picture.url if friend.picture else None
                    })
                
                # Combine all groups into a single list
                for group_name, companions in friendship_groups.items():
                    all_companions.extend(companions)
                
                print(f"Found {len(all_companions)} companions using Friendship model for guest {guest.guest_id}")
                
                # If no companions found in Friendship model, fall back to legacy methods
                if not all_companions:
                    print("No companions found in Friendship model, attempting to populate...")
                    from .utils import populate_friendships
                    populate_friendships()
                    return get_companions(request)  # Try again after populating
            
            except Exception as e:
                import traceback
                print(f"Error using Friendship model: {e}")
                traceback.print_exc()
                # If Friendship approach fails, fall back to legacy method
                all_companions = get_companions_legacy(guest)
            
            return JsonResponse({
                'success': True,
                'companions': all_companions
            })
        except Exception as e:
            import traceback
            print(f"Error in get_companions: {e}")
            traceback.print_exc()
            return JsonResponse({'success': False, 'message': str(e)}, status=500)
    
    return JsonResponse({'success': False, 'message': 'Invalid request'}, status=400)

def get_companions_legacy(guest):
    """Legacy method to get companions from various relationship sources"""
    print(f"Using legacy companion fetching for guest {guest.guest_id}")
    all_companions = []
    
    # Get companions added directly by the user
    try:
        direct_companions = Guest.objects.filter(made_by=guest).select_related('group')
        for companion in direct_companions:
            all_companions.append({
                'guest_id': companion.guest_id,
                'first_name': companion.first_name,
                'last_name': companion.last_name,
                'age': companion.age,
                'age_label': companion.age_label,
                'group_name': 'Personal Companions',
                'picture_url': companion.picture.url if companion.picture else None
            })
    except Exception as e:
        print(f"Error fetching direct companions: {e}")
    
    # Get companions from family group
    try:
        if hasattr(guest, 'family') and guest.family:
            family_members = Guest.objects.filter(family=guest.family).exclude(guest_id=guest.guest_id)
            for member in family_members:
                all_companions.append({
                    'guest_id': member.guest_id,
                    'first_name': member.first_name,
                    'last_name': member.last_name,
                    'age': member.age,
                    'age_label': member.age_label,
                    'group_name': 'Family',
                    'picture_url': member.picture.url if member.picture else None
                })
    except Exception as e:
        print(f"Error fetching family companions: {e}")
    
    # Get companions from friend groups
    try:
        friend_groups = FriendGroup.objects.filter(members=guest)
        for group in friend_groups:
            members = group.members.all().exclude(guest_id=guest.guest_id)
            for member in members:
                all_companions.append({
                    'guest_id': member.guest_id,
                    'first_name': member.first_name,
                    'last_name': member.last_name,
                    'age': member.age,
                    'age_label': member.age_label,
                    'group_name': group.name,
                    'picture_url': member.picture.url if member.picture else None
                })
    except Exception as e:
        print(f"Error fetching friend group companions: {e}")
    
    print(f"Found {len(all_companions)} companions using legacy method")
    return all_companions

# Add a new URL mapping in urls.py:
# path('get_companions/', views.get_companions, name='get_companions'),

@login_required
@require_http_methods(["POST"])
def send_companion_qr_code(request):
    """
    Generate a QR code with user and companion information, and send it to the user's email.
    """
    try:
        # Parse request data
        try:
            data = json.loads(request.body)
            include_companions = data.get('include_companions', True)
            debug_mode = data.get('debug_mode', False)
            refresh_data = data.get('refresh_data', False)
        except json.JSONDecodeError as e:
            return JsonResponse({
                'success': False,
                'error': f"Invalid JSON in request: {str(e)}"
            }, status=400)
        
        # Get current user data
        user = request.user
        
        # Safely extract user data based on what kind of object it is
        if hasattr(user, 'guest_id'):
            # User is a Guest object
            user_data = {
                'id': user.guest_id,
                'email': user.email if hasattr(user, 'email') else '',
                'first_name': user.first_name if hasattr(user, 'first_name') else '',
                'last_name': user.last_name if hasattr(user, 'last_name') else '',
                'phone_number': user.phone_number if hasattr(user, 'phone_number') else '',
            }
            if hasattr(user, 'username'):
                user_data['username'] = user.username
        else:
            # User is a standard Django User object
            user_data = {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone_number': getattr(user, 'phone_number', ''),
            }
        
        # Include companion data if requested
        companion_data = []
        if include_companions:
            try:
                # Get all companions for this user
                companions = Guest.objects.filter(made_by=user)
                
                if debug_mode:
                    print(f"Found {companions.count()} companions for user {user.username}")
                
                for companion in companions:
                    try:
                        # Safely extract basic companion information
                        companion_info = {}
                        
                        # Check each attribute exists before accessing
                        if hasattr(companion, 'guest_id'):
                            companion_info['id'] = companion.guest_id
                        else:
                            # Fall back to primary key if guest_id doesn't exist
                            companion_info['id'] = companion.pk
                            
                        # Extract other basic fields
                        for field in ['first_name', 'last_name', 'email', 'phone_number']:
                            if hasattr(companion, field):
                                companion_info[field] = getattr(companion, field)
                            else:
                                companion_info[field] = f"No {field}"
                        
                        # Add group information - carefully handle the relationship
                        try:
                            if hasattr(companion, 'group'):
                                group = getattr(companion, 'group')
                                if group is not None and hasattr(group, 'name'):
                                    companion_info['group'] = group.name
                                else:
                                    companion_info['group'] = 'No Group'
                            else:
                                companion_info['group'] = 'No Group'
                        except Exception as ge:
                            companion_info['group'] = 'No Group'
                            if debug_mode:
                                print(f"Error getting group: {str(ge)}")
                        
                        companion_data.append(companion_info)
                        
                        if debug_mode:
                            print(f"Processed companion: {companion_info}")
                            
                    except Exception as ce:
                        if debug_mode:
                            print(f"Error processing individual companion: {str(ce)}")
                            print(f"Companion object: {companion}")
                            print(f"Available attributes: {dir(companion)}")
            except Exception as ce:
                return JsonResponse({
                    'success': False,
                    'error': f"Error processing companions: {str(ce)}"
                }, status=500)
                
        user_data['companions'] = companion_data
        
        # Convert the data to JSON string
        try:
            json_data = json.dumps(user_data, indent=2)
        except Exception as je:
            return JsonResponse({
                'success': False,
                'error': f"Error converting data to JSON: {str(je)}"
            }, status=500)
        
        # Generate QR code
        try:
            qr = qrcode.QRCode(
                version=2,  # Lower version for simpler code
                error_correction=qrcode.constants.ERROR_CORRECT_L,  # Low error correction for simplicity
                box_size=10,  # Smaller box size for a more compact code
                border=4,   # Standard border
            )
            qr.add_data(json_data)
            qr.make(fit=True)
            
            # Create simple black and white QR code
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Save the QR code to a BytesIO object
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            buffer.seek(0)
            
        except Exception as qe:
            return JsonResponse({
                'success': False,
                'error': f"Error generating QR code: {str(qe)}"
            }, status=500)
        
        # Create and send email
        try:
            from django.core.mail import EmailMessage
            from django.template.loader import render_to_string
            
            # Email subject and message
            subject = "Your Companion Management QR Code"
            html_message = render_to_string('email/qr_code_email.html', {
                'user': user,
                'companion_count': len(companion_data),
                'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            # Create and send email
            email = EmailMessage(
                subject=subject,
                body=html_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            email.content_subtype = "html"  # Set the email to be HTML
            
            # Attach the QR code as a file
            email.attach('companion_qr_code.png', buffer.getvalue(), 'image/png')
            
            email.send()
        except Exception as ee:
            return JsonResponse({
                'success': False,
                'error': f"Error sending email: {str(ee)}"
            }, status=500)
        
        return JsonResponse({
            'success': True,
            'message': 'QR code has been sent to your email address.'
        })
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"Error in send_companion_qr_code: {str(e)}")
        print(error_traceback)
        return JsonResponse({
            'success': False,
            'error': f"Unexpected error: {str(e)}"
        }, status=500)

# Add this to urls.py: path('companion/qrcode/', views.send_companion_qr_code, name='companion_qr_code'),

@login_required
def debug_guest_model(request):
    """
    Debug view to inspect the Guest model structure.
    """
    try:
        user = request.user
        companions = Guest.objects.filter(made_by=user)
        
        debug_info = {
            'guest_model_fields': [],
            'guest_instances': []
        }
        
        # Get model fields
        if companions.exists():
            first_companion = companions.first()
            debug_info['guest_model_fields'] = [field.name for field in first_companion._meta.fields]
            
            # Get instance data for some companions
            for companion in companions[:5]:  # Limit to 5 to avoid overwhelming output
                companion_data = {
                    'repr': str(companion),
                    'attributes': {}
                }
                
                # Get all available attributes
                for field in first_companion._meta.fields:
                    field_name = field.name
                    try:
                        value = getattr(companion, field_name)
                        companion_data['attributes'][field_name] = str(value)
                    except Exception as e:
                        companion_data['attributes'][field_name] = f"Error: {str(e)}"
                
                debug_info['guest_instances'].append(companion_data)
        
        return JsonResponse({
            'success': True,
            'debug_info': debug_info
        })
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"Error in debug_guest_model: {str(e)}")
        print(error_traceback)
    return JsonResponse({
        'success': False,
        'error': str(e),
        'traceback': error_traceback
    }, status=500)

# Add to urls.py: path('debug/guest_model/', views.debug_guest_model, name='debug_guest_model'),


@login_required
def accommodation_page(request):
    rooms = (
        AdminRoom.objects.select_related("accommodation")
        .filter(status="AVAILABLE")
        .order_by("accommodation__company_name", "room_name")
    )
    return render(request, "accommodation_book.html", {
        "rooms": rooms,
    })


@login_required
@require_http_methods(["POST"])
def accommodation_recommend(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = request.POST

    params = {
        "guests": payload.get("guests"),
        "budget": payload.get("budget"),
        "location": payload.get("location"),
        "company_type": payload.get("company_type"),
    }

    results = recommend_accommodations(params, limit=5)
    data = [
        {
            "title": item.title,
            "subtitle": item.subtitle,
            "score": item.score,
            "meta": item.meta,
        }
        for item in results
    ]
    return JsonResponse({"success": True, "results": data})


@login_required
@require_http_methods(["POST"])
def accommodation_billing(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = request.POST

    room_id = payload.get("room_id")
    check_in = payload.get("check_in")
    check_out = payload.get("check_out")
    nights = payload.get("nights")

    room = AdminRoom.objects.select_related("accommodation").filter(room_id=room_id).first()
    if room is None:
        return JsonResponse({"success": False, "message": "Room not found."}, status=404)

    try:
        if check_in and check_out:
            check_in_dt = datetime.strptime(check_in, "%Y-%m-%d").date()
            check_out_dt = datetime.strptime(check_out, "%Y-%m-%d").date()
            total = calculate_accommodation_billing(room, check_in_dt, check_out_dt)
            nights = max((check_out_dt - check_in_dt).days, 1)
        else:
            nights = max(int(nights or 1), 1)
            total = calculate_accommodation_billing(room, timezone.now().date(), timezone.now().date() + timedelta(days=nights))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid dates. Use YYYY-MM-DD."}, status=400)

    return JsonResponse({
        "success": True,
        "total": f"{total:.2f}",
        "nights": nights,
        "rate": f"{room.price_per_night:.2f}",
        "room_name": room.room_name,
        "accommodation": room.accommodation.company_name,
    })


@login_required
@require_http_methods(["POST"])
def accommodation_book(request):
    room_id = request.POST.get("room_id")
    check_in = request.POST.get("check_in")
    check_out = request.POST.get("check_out")
    num_guests = int(request.POST.get("num_guests", "1"))

    room = AdminRoom.objects.select_related("accommodation").filter(room_id=room_id).first()
    if room is None:
        return JsonResponse({"success": False, "message": "Room not found."}, status=404)

    if room.person_limit and num_guests > room.person_limit:
        return JsonResponse({"success": False, "message": "Guest count exceeds room capacity."}, status=400)

    try:
        check_in_dt = datetime.strptime(check_in, "%Y-%m-%d").date()
        check_out_dt = datetime.strptime(check_out, "%Y-%m-%d").date()
        if check_out_dt <= check_in_dt:
            return JsonResponse({"success": False, "message": "Check-out must be after check-in."}, status=400)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid dates. Use YYYY-MM-DD."}, status=400)

    total = calculate_accommodation_billing(room, check_in_dt, check_out_dt)

    booking = AccommodationBooking.objects.create(
        guest=request.user,
        accommodation=room.accommodation,
        room=room,
        check_in=check_in_dt,
        check_out=check_out_dt,
        num_guests=num_guests,
        status="pending",
        total_amount=total,
    )

    return JsonResponse({
        "success": True,
        "message": "Accommodation booking submitted and pending confirmation.",
        "booking_id": booking.booking_id,
        "total_amount": f"{booking.total_amount:.2f}",
    })
