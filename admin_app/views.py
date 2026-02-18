from django.contrib.auth import logout
from django.contrib.auth.hashers import check_password
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import EmployeeRegistrationForm, AccomodationForm
from .models import AdminInfo, Accomodation, Employee, UserActivity, AccommodationCertification
from accom_app.forms import OtherEstabForm
from django.http import HttpResponse
from django.shortcuts import render, redirect
from accom_app.models import Other_Estab
from django.http import HttpResponse
import json

from .forms import EstablishmentFormAdmin
from .models import Region, Country, Entry, HotelConfirmation
from accom_app.models import Summary
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from functools import wraps
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.db.models import Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
import datetime as dt
from tour_app.models import Tour_Add, Tour_Schedule, Tour_Event
from guest_app.models import Guest, Pending, AccommodationBooking
from .models import TourAssignment

def log_activity(request, employee, activity_type, description=None, page=None):
    """
    Log user activity for tracking
    
    Parameters:
    - request: The HTTP request object
    - employee: The Employee model instance
    - activity_type: Type of activity (see UserActivity.ACTIVITY_TYPES)
    - description: Optional description of the activity
    - page: Optional page name or URL
    """
    if not page and request.path:
        page = request.path
        
    UserActivity.objects.create(
        employee=employee,
        activity_type=activity_type,
        description=description,
        page=page,
        ip_address=request.META.get('REMOTE_ADDR'),
        user_agent=request.META.get('HTTP_USER_AGENT', '')
    )
    

def accomodation_required(view_func):
    """
    Decorator that checks whether the user is logged in as an accommodation account.
    If not, the user is redirected to the login page.
    """
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        if request.session.get('user_type') != 'accomodation' or not request.session.get('accom_id'):
            return redirect('admin_app:login')
        return view_func(request, *args, **kwargs)
    return wrapped_view

# Admin-only decorator to restrict access to admin users
def admin_required(view_func):
    """
    Decorator that checks whether the user is logged in as an admin.
    If not, the user is redirected to the login page.
    """
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        if request.session.get('user_type') != 'employee' or not request.session.get('is_admin'):
            return redirect('admin_app:login')
        return view_func(request, *args, **kwargs)
    return wrapped_view

def map_view(request):
    """
    View function that renders the map.html template.
    This provides a map interface for the admin to view tour locations.
    """
    # Check if the user is logged in as an admin
    if request.session.get('user_type') != 'employee' or not request.session.get('is_admin'):
        return redirect('admin_app:login')
    
    # Log the activity
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
        log_activity(request, employee, 'view_page', description='Viewed map page')
    except Employee.DoesNotExist:
        pass
    
    return render(request, 'map.html')

# Employee registration view
def employee_register(request):
    if request.method == 'POST':
        form = EmployeeRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save(commit=False)
            user.role = "Employee"  # set role if you like
            user.save()  # password is hashed from form.save()
            messages.success(request, "Registration successful! You can now log in.")
            return redirect('admin_app:login')  # Redirect to login after registration
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = EmployeeRegistrationForm()

    return render(request, 'employee_register.html', {'form': form})


def login(request):
    # Clear any existing session data.
    request.session.flush()

    if request.method == 'POST':
        username_or_email = request.POST.get('username')  # could be an email
        password = request.POST.get('password')

        # Try authenticating as an Employee first.
        try:
            employee = Employee.objects.get(email=username_or_email)
            if employee.check_password(password):
                if employee.status.lower() != "accepted":
                    messages.error(request, "Your employee account is not approved yet.")
                    return redirect('admin_app:login')

                request.session['user_type'] = 'employee'
                request.session['employee_id'] = employee.emp_id
                request.session['user_id'] = employee.emp_id  # Add user_id for consistent authentication
                
                # Update last login timestamp
                employee.last_login = timezone.now()
                employee.save()

                # Set admin flag in session based on role
                if employee.role.lower() == "admin":
                    request.session['is_admin'] = True
                    
                    # Log admin login activity
                    log_activity(
                        request, 
                        employee, 
                        'login', 
                        description=f'Admin login from {request.META.get("REMOTE_ADDR")} using {request.META.get("HTTP_USER_AGENT", "Unknown browser")}'
                    )
                    
                    return redirect('admin_app:admin_dashboard')
                else:
                    request.session['is_admin'] = False
                    
                    # Log employee login activity
                    log_activity(
                        request, 
                        employee, 
                        'login', 
                        description=f'Employee login from {request.META.get("REMOTE_ADDR")} using {request.META.get("HTTP_USER_AGENT", "Unknown browser")}'
                    )
                    
                    return redirect('admin_app:employee_dashboard')
            else:
                messages.error(request, "Invalid username or password")
        except Employee.DoesNotExist:
            # Not found in Employee table; try Accomodation.
            try:
                accom = Accomodation.objects.get(email_address=username_or_email)
                if check_password(password, accom.password):
                    if accom.status.lower() != "accepted":
                        messages.error(request, "Your accommodation account is not approved yet.")
                        return redirect('admin_app:login')

                    # Set session info for accommodation accounts.
                    # Check company type to determine if it's an establishment account
                    if accom.company_type.lower() == "establishment":
                        request.session['user_type'] = 'establishment'
                        request.session['accom_id'] = accom.accom_id
                        request.session['company_name'] = accom.company_name
                        request.session['company_type'] = accom.company_type

                        # Redirect to the establishment dashboard
                        return redirect('admin_app:establishment_dashboard')
                    else:
                        # Set session info for regular accommodation accounts
                        request.session['user_type'] = 'accomodation'
                        request.session['accom_id'] = accom.accom_id
                        # Storing additional info in session for later use.
                        request.session['company_name'] = accom.company_name if hasattr(accom, 'company_name') else accom.email_address
                        request.session['company_type'] = accom.company_type

                        # Redirect to the accommodation dashboard.
                        return redirect('admin_app:accommodation_dashboard')
                else:
                    messages.error(request, "Invalid username or password")
            except Accomodation.DoesNotExist:
                messages.error(request, "Invalid username or password")

    return render(request, 'login.html')

def admin_logout(request):
    # Log the logout activity before clearing the session
    if request.session.get('user_type') == 'employee' and request.session.get('employee_id'):
        try:
            employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
            is_admin = request.session.get('is_admin', False)
            user_type = "Admin" if is_admin else "Employee"
            
            log_activity(
                request, 
                employee, 
                'logout', 
                description=f'{user_type} logged out from {request.META.get("REMOTE_ADDR")} after {(timezone.now() - employee.last_login).total_seconds() / 60:.1f} minutes' if hasattr(employee, 'last_login') and employee.last_login else f'{user_type} logged out from {request.META.get("REMOTE_ADDR")}'
            )
            
            # Update last_login time for next session duration calculation
            employee.last_login = None  
            employee.save()
        except Employee.DoesNotExist:
            pass
    
    # Perform the logout
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect('admin_app:login')  # Redirect to login after logout



def accommodation_register(request):
    if request.method == 'POST':
        form = AccomodationForm(request.POST, request.FILES)
        if form.is_valid():
            accommodation = form.save()  # Form now handles certifications internally
            
            # Process individual certification files
            for key, file in request.FILES.items():
                if key.startswith('certification_'):
                    AccommodationCertification.objects.create(
                        accommodation=accommodation,
                        image=file
                    )
                    
            messages.success(request, "Accomodation registered successfully! You can now log in.")
            return redirect('admin_app:login')  # Redirect to login after successful registration
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AccomodationForm()

    # Add request object to context for template conditional rendering
    context = {
        'form': form,
        'request': request,
    }
    return render(request, 'accommodation.html', context)

def create_accommodation(request):
    accommodations = Accomodation.objects.all()
    return render(request, 'accommodation.html', {'accommodations': accommodations})

def pending_accommodation(request):
    pending_accommodations = Accomodation.objects.filter(status__iexact="Pending")
    accepted_accommodations = Accomodation.objects.filter(status__iexact="Accepted")
    declined_accommodations = Accomodation.objects.filter(status__iexact="Declined")
    context = {
        'pending_accommodations': pending_accommodations,
        'accepted_accommodations': accepted_accommodations,
        'declined_accommodations': declined_accommodations,
    }
    return render(request, 'pending_accommodation.html', context)


@admin_required
def accommodation_bookings(request):
    pending_bookings = AccommodationBooking.objects.select_related(
        "guest", "accommodation", "room"
    ).filter(status="pending").order_by("-booking_date")

    confirmed_bookings = AccommodationBooking.objects.select_related(
        "guest", "accommodation", "room"
    ).filter(status="confirmed").order_by("-booking_date")

    declined_bookings = AccommodationBooking.objects.select_related(
        "guest", "accommodation", "room"
    ).filter(status="declined").order_by("-booking_date")

    cancelled_bookings = AccommodationBooking.objects.select_related(
        "guest", "accommodation", "room"
    ).filter(status="cancelled").order_by("-booking_date")

    context = {
        "pending_bookings": pending_bookings,
        "confirmed_bookings": confirmed_bookings,
        "declined_bookings": declined_bookings,
        "cancelled_bookings": cancelled_bookings,
    }
    return render(request, "pending_accommodation_bookings.html", context)


@admin_required
@require_POST
def accommodation_booking_update(request, booking_id):
    booking = get_object_or_404(AccommodationBooking, booking_id=booking_id)
    action = request.POST.get("action")

    if action == "confirm":
        booking.status = "confirmed"
        messages.success(request, "Booking confirmed.")
    elif action == "decline":
        booking.status = "declined"
        messages.success(request, "Booking declined.")
    elif action == "cancel":
        booking.status = "cancelled"
        booking.cancellation_reason = request.POST.get("reason") or "Cancelled by admin."
        booking.cancellation_date = timezone.now()
        messages.success(request, "Booking cancelled.")
    else:
        messages.error(request, "Invalid action.")
        return redirect('admin_app:accommodation_bookings')

    booking.save()
    return redirect('admin_app:accommodation_bookings')

def accommodation_update(request, pk):
    try:
        accom = Accomodation.objects.get(pk=pk)
    except Accomodation.DoesNotExist:
        messages.error(request, "Accommodation not found.")
        return redirect('admin_app:pending_accommodation')

    if request.method == 'POST':
        new_status = request.POST.get('status')
        # Directly store the lowercase status values.
        if new_status in ['accepted', 'declined']:
            accom.status = new_status
            accom.save(update_fields=['status'])
            messages.success(request, "Status updated successfully.")
        else:
            messages.error(request, "Invalid status selected.")
    return redirect('admin_app:pending_accommodation')




from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import Employee


def update_employees(request, emp_id):
    employee = get_object_or_404(Employee, emp_id=emp_id)
    if request.method == 'POST':
        # Update all editable fields
        employee.first_name = request.POST.get('first_name')
        employee.last_name = request.POST.get('last_name')
        employee.middle_name = request.POST.get('middle_name')
        employee.phone_number = request.POST.get('phone_number')
        employee.email = request.POST.get('email')
        employee.age = request.POST.get('age')
        employee.sex = request.POST.get('sex')
        employee.role = request.POST.get('role')
        employee.status = request.POST.get('status')
        # Handle file upload for profile picture if provided
        if request.FILES.get('profile_picture'):
            employee.profile_picture = request.FILES.get('profile_picture')
        employee.save()
        messages.success(request, 'Employee updated successfully!')
        return redirect('admin_app:pending_employees')
    return render(request, 'update_employee.html', {'employee': employee})


def pending_employees(request):
    # Check if the user is logged in as an admin
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')

    # Retrieve the employee from the database and check if the role is admin
    employee = get_object_or_404(Employee, emp_id=request.session.get('employee_id'))
    if employee.role.lower() != 'admin':
        return redirect('admin_app:login')

    # Retrieve all employees regardless of status
    employees = Employee.objects.all()
    return render(request, 'pending_employees.html', {'employees': employees})


def employee_dashboard(request):
    # Check if the user is logged in as an employee
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
    
    # Get employee details
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        # Redirect to login if employee not found
        return redirect('admin_app:login')
    
    # Get assigned tours
    assignments = TourAssignment.objects.filter(employee=employee).select_related('schedule', 'schedule__tour')
    
    context = {
        'employee': employee,
        'assignments': assignments,
    }
    
    return render(request, 'employee_dashboard.html', context)


def employee_assigned_tours(request):
    """View for displaying tours assigned to the employee"""
    # Check if the user is logged in as an employee
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
    
    # Get employee details
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')
    
    # Get assigned tours
    assignments = TourAssignment.objects.filter(employee=employee).select_related('schedule', 'schedule__tour')
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed assigned tours')
    
    context = {
        'employee': employee,
        'assignments': assignments,
    }
    
    return render(request, 'employee/assigned_tours.html', context)


def employee_tour_calendar(request):
    """View for displaying a calendar of tours assigned to the employee"""
    # Check if the user is logged in as an employee
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
    
    # Get employee details
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')
    
    # Get assigned tours for the calendar
    assignments = TourAssignment.objects.filter(employee=employee).select_related('schedule', 'schedule__tour')
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed tour calendar')
    
    context = {
        'employee': employee,
        'assignments': assignments,
    }
    
    return render(request, 'employee/tour_calendar.html', context)


def employee_accommodations(request):
    """View for displaying accommodations available to the employee"""
    # Check if the user is logged in as an employee
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
    
    # Get employee details
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')
    
    # Get accommodations (hotels, attractions, etc.)
    from accom_app.models import Accomodation
    accommodations = Accomodation.objects.filter(status='Accepted')
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed accommodations')
    
    context = {
        'employee': employee,
        'accommodations': accommodations,
    }
    
    return render(request, 'employee/accommodations.html', context)


def employee_profile(request):
    """View for displaying and updating employee profile"""
    # Check if the user is logged in as an employee
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
    
    # Get employee details
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')
    
    # Handle profile update (if form was submitted)
    if request.method == 'POST':
        # Update profile fields
        employee.first_name = request.POST.get('first_name', employee.first_name)
        employee.last_name = request.POST.get('last_name', employee.last_name)
        employee.middle_name = request.POST.get('middle_name', employee.middle_name)
        employee.phone_number = request.POST.get('phone_number', employee.phone_number)
        
        # Handle profile picture if uploaded
        if 'profile_picture' in request.FILES:
            employee.profile_picture = request.FILES['profile_picture']
            
        # Save changes
        employee.save()
        
        # Log the activity
        log_activity(request, employee, 'update_profile', description='Updated profile information')
        
        messages.success(request, "Profile updated successfully!")
        return redirect('admin_app:employee_profile')
    
    # Log the view activity
    log_activity(request, employee, 'view_page', description='Viewed profile page')
    
    context = {
        'employee': employee,
    }
    
    return render(request, 'employee/profile.html', context)


def admin_dashboard(request):
    """View for admin dashboard showing statistics and links to admin functions."""
    # Check if the user is logged in as an admin
    if request.session.get('user_type') != 'employee' or not request.session.get('is_admin'):
        return redirect('admin_app:login')
    
    # Log the activity
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
        log_activity(request, employee, 'view_page', description='Viewed admin dashboard')
    except Employee.DoesNotExist:
        pass
    
    # Get all employees for the employee assignment dropdown
    employees = Employee.objects.all()
    
    # Get active tours
    active_tours = Tour_Schedule.objects.filter(
        end_time__gte=timezone.now()
    ).order_by('start_time')
    
    # Get tour assignments for active tours
    tour_assignments = {}
    for tour in active_tours:
        assignment = TourAssignment.objects.filter(schedule=tour).first()
        if assignment:
            tour_assignments[tour.sched_id] = {
                'employee_id': assignment.employee.emp_id,
                'employee_name': f"{assignment.employee.first_name} {assignment.employee.last_name}"
            }
    
    # Add any dashboard data to context
    context = {
        'active_tours_count': Tour_Add.objects.count(),  # Add a count for stats
        'active_tours': active_tours,
        'pending_bookings': Pending.objects.filter(status='Pending').count(),
        'total_users': Guest.objects.count(),
        'employees': employees,
        'tour_assignments': tour_assignments,
    }
    
    return render(request, 'admin_dashboard.html', context)


@accomodation_required
def accommodation_dashboard(request):
    if 'accom_id' not in request.session:
        return redirect('admin_app:login')
    
    from admin_app.models import Accomodation
    try:
        accom = Accomodation.objects.get(accom_id=request.session.get('accom_id'))
    except Accomodation.DoesNotExist:
        return redirect('admin_app:login')
    
    username = getattr(accom, 'name', accom.email_address)
    # Set is_hotel to True if the company_type (case-insensitive) is "hotel".
    is_hotel = (accom.company_type.lower() == "hotel")
    
    context = {
        'username': username,
        'is_hotel': is_hotel,
    }
    
    # If this is a hotel account, get available rooms
    if is_hotel:
        from admin_app.models import Room
        available_rooms = Room.objects.filter(
            accommodation=accom,
            status='AVAILABLE'
        )
        context['available_rooms'] = available_rooms
    
    return render(request, 'accommodation_dashboard.html', context)



# ------------------------------------------------------------------------------
# Standard Form Processing (if needed for batch updates)
# ------------------------------------------------------------------------------
def admin_create_form(request):
    # Check if the user is logged in as either employee or admin (which is also an employee)
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')
        
    if request.method == 'POST':
        # ------------------------------
        # Process Regions
        # ------------------------------
        # Expected format for each hidden field: "region_key::region_name"
        posted_region_fields = request.POST.getlist('dynamic_field_region[]')
        posted_region_ids = []
        region_mapping = {}  # Map form's region key to the corresponding DB Region instance
        for region_field in posted_region_fields:
            if '::' not in region_field:
                continue
            region_key, region_name = region_field.split('::', 1)
            if region_key.startswith('new-'):
                region, created = Region.objects.get_or_create(name=region_name)
            else:
                try:
                    region = Region.objects.get(id=region_key)
                    if region.name != region_name:
                        region.name = region_name
                        region.save()
                except Region.DoesNotExist:
                    region, created = Region.objects.get_or_create(name=region_name)
            posted_region_ids.append(region.id)
            region_mapping[region_key] = region
        Region.objects.exclude(id__in=posted_region_ids).delete()

        # ------------------------------
        # Process Countries
        # ------------------------------
        # Expected format: "region_key::country_name"
        posted_country_fields = request.POST.getlist('dynamic_field_country[]')
        posted_country_ids = []
        for country_field in posted_country_fields:
            if '::' not in country_field:
                continue
            region_key, country_name = country_field.split('::', 1)
            region_obj = region_mapping.get(region_key)
            if not region_obj:
                continue
            country, created = Country.objects.get_or_create(name=country_name, region=region_obj)
            posted_country_ids.append(country.id)
        Country.objects.exclude(id__in=posted_country_ids).delete()

        # ------------------------------
        # Process Entries
        # ------------------------------
        # Retrieves a comma-separated list of entry titles.
        compiled_entries = request.POST.get('compiled_entry_data', '')
        posted_entry_titles = [entry.strip() for entry in compiled_entries.split(',') if entry.strip()]
        for title in posted_entry_titles:
            Entry.objects.get_or_create(title=title)
        Entry.objects.exclude(title__in=posted_entry_titles).delete()

        return redirect('admin_app:admin_create_form')
    else:
        form = EstablishmentFormAdmin()

    regions = Region.objects.all()
    countries = Country.objects.all()
    # Include is_hotel status in entries query
    entries = Entry.objects.all().order_by('title')

    context = {
        'form': form,
        'regions': regions,
        'countries': countries,
        'entries': entries,
        'request': request,  # Add request to context for template checks
    }

    return render(request, 'accom_establishment.html', context)

def establishment_summary(request):
    regions = Region.objects.all()
    countries = Country.objects.all()
    entries = Entry.objects.all()
    return render(request, 'your_template.html', {
        'regions': regions,
        'countries': countries,
        'entries': entries
    })

# ------------------------------------------------------------------------------
# AJAX Endpoints for Immediate (Auto) Save
# ------------------------------------------------------------------------------

# ----- REGION Endpoints -----
@require_POST
def ajax_add_region(request):
    region_name = request.POST.get('name', '').strip()
    if region_name:
        region, created = Region.objects.get_or_create(name=region_name)
        return JsonResponse({'status': 'success', 'region_id': region.id, 'region_name': region.name})
    return JsonResponse({'status': 'error', 'error': 'Missing region name'}, status=400)

@require_POST
def ajax_edit_region(request):
    region_id = request.POST.get('region_id')
    new_name = request.POST.get('name', '').strip()
    if region_id and new_name:
        region = get_object_or_404(Region, id=region_id)
        region.name = new_name
        region.save()
        return JsonResponse({'status': 'success', 'region_name': region.name})
    return JsonResponse({'status': 'error', 'error': 'Invalid parameters'}, status=400)

@require_POST
def ajax_delete_region(request):
    region_id = request.POST.get('region_id')
    if region_id:
        region = get_object_or_404(Region, id=region_id)
        region.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid region id'}, status=400)

# ----- COUNTRY Endpoints -----
@require_POST
def ajax_add_country(request):
    region_id = request.POST.get('region_id')
    country_name = request.POST.get('name', '').strip()
    if region_id and country_name:
        region = get_object_or_404(Region, id=region_id)
        country, created = Country.objects.get_or_create(name=country_name, region=region)
        return JsonResponse({'status': 'success', 'country_id': country.id, 'country_name': country.name})
    return JsonResponse({'status': 'error', 'error': 'Invalid parameters'}, status=400)

@require_POST
def ajax_edit_country(request):
    country_id = request.POST.get('country_id')
    new_name = request.POST.get('name', '').strip()
    if country_id and new_name:
        country = get_object_or_404(Country, id=country_id)
        country.name = new_name
        country.save()
        return JsonResponse({'status': 'success', 'country_name': country.name})
    return JsonResponse({'status': 'error', 'error': 'Invalid parameters'}, status=400)

@require_POST
def ajax_delete_country(request):
    country_id = request.POST.get('country_id')
    if country_id:
        country = get_object_or_404(Country, id=country_id)
        country.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid country id'}, status=400)

# ----- ENTRY Endpoints -----
@require_POST
def ajax_add_entry(request):
    entry_title = request.POST.get('title', '').strip()
    if entry_title:
        entry, created = Entry.objects.get_or_create(title=entry_title)
        return JsonResponse({'status': 'success', 'entry_id': entry.id, 'entry_title': entry.title})
    return JsonResponse({'status': 'error', 'error': 'Missing entry title'}, status=400)

@require_POST
def ajax_edit_entry(request):
    entry_id = request.POST.get('entry_id')
    new_title = request.POST.get('title', '').strip()
    if entry_id and new_title:
        entry = get_object_or_404(Entry, id=entry_id)
        entry.title = new_title
        entry.save()
        return JsonResponse({'status': 'success', 'entry_title': entry.title})
    return JsonResponse({'status': 'error', 'error': 'Invalid parameters'}, status=400)

@require_POST
def ajax_delete_entry(request):
    entry_id = request.POST.get('entry_id')
    if entry_id:
        entry = get_object_or_404(Entry, id=entry_id)
        entry.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid entry id'}, status=400)

@csrf_exempt
def ajax_mark_as_hotel(request):
    if request.method == "POST":
        entry_id = request.POST.get("entry_id")
        status = request.POST.get("status")  # "yes" for marking as hotel; "no" for unmarking
        try:
            entry = Entry.objects.get(id=entry_id)
            if status == "yes":
                entry.is_hotel = True
            else:
                entry.is_hotel = False
            entry.save()
            return JsonResponse({"status": "success", "message": "Updated successfully."})
        except Entry.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Entry not found."})
    return JsonResponse({"status": "error", "message": "Invalid request."})

@csrf_exempt
def ajax_mark_summary_as_hotel(request):
    if request.method == "POST":
        summary_id = request.POST.get("summary_id")
        status = request.POST.get("status")  # "1" for highlighted, "0" for unhighlighted
        try:
            summary = Summary.objects.get(id=summary_id)
            # Mimic the is_hotel logic from the Entry form
            summary.hotel = "1" if status == "1" else "0"
            summary.save()
            return JsonResponse({"status": "success", "hotel": summary.hotel})
        except Summary.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Summary not found"})
    return JsonResponse({"status": "error", "message": "Invalid request"})


def tour_calendar(request):
    """
    View function for displaying the tour calendar interface.
    """
    # In a real application, you would fetch tour data from the database here
    # For now, we'll just render the template with sample data that's defined in the template
    
    context = {
        'page_title': 'Tour Calendar'
    }
    
    return render(request, 'tour_calendar.html', context)

@admin_required
def activity_tracker(request):
    """
    View for displaying user activity logs.
    Only accessible to admin users.
    Includes filtering and pagination.
    """
    # Get filter parameters from the request
    search_query = request.GET.get('search', '')
    selected_type = request.GET.get('activity_type', '')
    selected_employee = request.GET.get('employee', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    # Base queryset
    activities = UserActivity.objects.select_related('employee').all()
    
    # Apply filters
    if search_query:
        activities = activities.filter(
            Q(employee__first_name__icontains=search_query) |
            Q(employee__last_name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    if selected_type:
        activities = activities.filter(activity_type=selected_type)
    
    if selected_employee:
        activities = activities.filter(employee__emp_id=selected_employee)
    
    if date_from:
        try:
            date_from_obj = dt.datetime.strptime(date_from, '%Y-%m-%d').date()
            activities = activities.filter(timestamp__date__gte=date_from_obj)
        except ValueError:
            pass  # Invalid date format, ignore filter
    
    if date_to:
        try:
            date_to_obj = dt.datetime.strptime(date_to, '%Y-%m-%d').date()
            # Add one day to include activities from the selected end date
            date_to_obj = date_to_obj + dt.timedelta(days=1)
            activities = activities.filter(timestamp__date__lt=date_to_obj)
        except ValueError:
            pass  # Invalid date format, ignore filter
    
    # Calculate summary statistics
    total_activities = UserActivity.objects.count()
    login_count = UserActivity.objects.filter(activity_type='login').count()
    
    # Today's activity
    today = timezone.now().date()
    today_count = UserActivity.objects.filter(timestamp__date=today).count()
    
    # Active users today (users with any activity today)
    active_users_today = Employee.objects.filter(
        activities__timestamp__date=today
    ).distinct().count()
    
    # Get all employees for the filter dropdown
    employees = Employee.objects.all().order_by('first_name', 'last_name')
    
    # Get activity type choices
    activity_types = UserActivity.ACTIVITY_TYPES
    
    # Pagination
    page = request.GET.get('page', 1)
    paginator = Paginator(activities, 25)  # Show 25 activities per page
    
    try:
        activities = paginator.page(page)
    except PageNotAnInteger:
        activities = paginator.page(1)
    except EmptyPage:
        activities = paginator.page(paginator.num_pages)
    
    context = {
        'activities': activities,
        'search_query': search_query,
        'selected_type': selected_type,
        'selected_employee': selected_employee,
        'date_from': date_from,
        'date_to': date_to,
        'employees': employees,
        'activity_types': activity_types,
        'total_activities': total_activities,
        'login_count': login_count,
        'today_count': today_count,
        'active_users_today': active_users_today,
    }
    
    # Log this view access
    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
        log_activity(request, employee, 'view_page', 'Viewed activity tracker page')
    except Employee.DoesNotExist:
        pass
    
    return render(request, 'activity_tracker.html', context)


@admin_required
def assign_employee_direct(request):
    """
    View for directly assigning an employee to a tour.
    Handles POST requests from the admin dashboard modal.
    """
    if request.method == 'POST':
        tour_id = request.POST.get('tour_id')
        employee_id = request.POST.get('employee_id')

        if not tour_id or not employee_id:
            messages.error(request, "Tour ID and Employee ID are required.")
            return redirect('admin_app:admin_dashboard')

        try:
            # Get the tour schedule
            tour_schedule = Tour_Schedule.objects.get(sched_id=tour_id)
        except Tour_Schedule.DoesNotExist:
            messages.error(request, "Tour schedule not found.")
            return redirect('admin_app:admin_dashboard')

        try:
            # Get the employee
            employee = Employee.objects.get(emp_id=employee_id)
        except Employee.DoesNotExist:
            messages.error(request, "Employee not found.")
            return redirect('admin_app:admin_dashboard')

        # Check if this employee is already assigned to this tour
        existing_assignment = TourAssignment.objects.filter(
            employee=employee,
            schedule=tour_schedule
        ).exists()

        if existing_assignment:
            messages.warning(request, f"{employee.first_name} {employee.last_name} is already assigned to this tour.")
        else:
            # Create the assignment
            TourAssignment.objects.create(
                employee=employee,
                schedule=tour_schedule
            )
            messages.success(request, f"Successfully assigned {employee.first_name} {employee.last_name} to {tour_schedule.tour_id.tour_name}.")

            # Log the activity
            try:
                admin_employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
                log_activity(
                    request,
                    admin_employee,
                    'create',
                    description=f'Assigned employee {employee.first_name} {employee.last_name} to tour {tour_schedule.tour_id.tour_name}'
                )
            except Employee.DoesNotExist:
                pass

        return redirect('admin_app:admin_dashboard')

    # If not POST, redirect to dashboard
    return redirect('admin_app:admin_dashboard')
