from .forms import TourAddForm
from django.forms import formset_factory
from .forms import TourScheduleForm, TourAdmissionForm
from .models import Tour_Add, Tour_Event, Tour_Schedule, Tour_Admission, Admission_Rates, Tour_SupportingImage
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages
from django.views.generic.edit import UpdateView
from guest_app.models import Pending, Guest
from django.urls import reverse, reverse_lazy
from datetime import date, timedelta
import calendar
from django.shortcuts import redirect, render
from django.views.generic.edit import UpdateView
from django.urls import reverse_lazy
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import user_passes_test, login_required
from django.utils.decorators import method_decorator
from django.http import HttpResponseForbidden
from django.core.exceptions import ObjectDoesNotExist
from functools import wraps
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.core.mail import send_mail
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from decimal import Decimal
from io import BytesIO
from email.mime.image import MIMEImage
import qrcode
from .translation_models import TourAddTranslation
from guest_app.utils import get_current_language, translate, get_translations_json, LANGUAGE_SESSION_KEY
from admin_app.models import Employee, TourAssignment
from admin_app.notification_service import create_notification, notify_assigned_employees_for_schedule
from ai_chatbot.models import RecommendationEvent, SystemMetricLog


def _safe_log_tour_status_event(*, request, user, event_type, item_ref):
    try:
        if not user:
            return
        session_id = ""
        if hasattr(request, "session"):
            session_id = request.session.session_key or ""
        RecommendationEvent.objects.create(
            user=user,
            event_type=str(event_type or "view").strip().lower(),
            item_ref=str(item_ref or "").strip()[:100],
            session_id=session_id,
            data_source="real_world",
        )
    except Exception:
        pass


def _safe_log_tour_email_dispatch(*, email_type, success, error_message=""):
    try:
        SystemMetricLog.objects.create(
            module="email",
            endpoint=f"tour_email:{str(email_type or '').strip().lower()}",
            response_time_ms=0,
            success_flag=bool(success),
            status_code=200 if success else 500,
            error_message=str(error_message or "")[:300],
            data_source="real_world",
        )
    except Exception:
        pass


def admin_employee_required(view_func):
    """
    Decorator for views that checks if the user is admin or employee
    using session-based authentication
    """
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        # Check if user is logged in as employee
        if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
            messages.error(request, "Access denied. Admin or employee privileges required.")
            return redirect('admin_app:login')
        return view_func(request, *args, **kwargs)
    return wrapped_view


def _get_employee_session_context(request):
    if request.session.get('user_type') != 'employee':
        return None, False
    employee_id = request.session.get('employee_id')
    if not employee_id:
        return None, False
    employee = Employee.objects.filter(emp_id=employee_id).first()
    if not employee:
        return None, False
    return employee, bool(request.session.get('is_admin', False))


def _assigned_schedule_ids_for_employee(employee):
    return list(
        TourAssignment.objects.filter(employee=employee).values_list('schedule_id', flat=True)
    )


# Apply the decorator to all view functions
@admin_employee_required
def main_page(request):
    # Fetch all TourAdd objects from the database
    tours = Tour_Add.objects.all()

    # Check if no tours are found
    if not tours:
        print("No tours found!")

    # Pass the tours to the template
    return render(request, 'tour_app/mainpage.html', {'tours': tours})


@admin_employee_required
def add_tour(request, tour_id=None):
    if request.method == 'POST':
        if tour_id:
            tour = get_object_or_404(Tour_Add, tour_id=tour_id)
            form = TourAddForm(request.POST, request.FILES, instance=tour)
        else:
            form = TourAddForm(request.POST, request.FILES)

        if form.is_valid():
            tour_obj = form.save()  # Save the tour pack
            for image_file in request.FILES.getlist('supporting_images'):
                if image_file:
                    Tour_SupportingImage.objects.create(tour=tour_obj, image=image_file)
            return redirect('tour_app:home')  # Redirect to home after saving

    else:
        if tour_id:
            tour = get_object_or_404(Tour_Add, tour_id=tour_id)
            form = TourAddForm(instance=tour)
        else:
            form = TourAddForm()

    existing_supporting_images = []
    if tour_id:
        existing_supporting_images = Tour_SupportingImage.objects.filter(tour_id=tour_id)

    return render(request, 'add_tour.html', {
        'form': form,
        'tour_id': tour_id,  # Ensure tour_id is passed to the template
        'existing_supporting_images': existing_supporting_images,
    })


@admin_employee_required
def create_schedule(request, tour_id):
    # Fetch the tour using the tour_id from the URL
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    AdmissionFormSet = formset_factory(TourAdmissionForm, extra=1, can_delete=True)

    if request.method == "POST":
        form = TourScheduleForm(request.POST)
        formset = AdmissionFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            # Create a new schedule and associate it with the current tour
            schedule = form.save(commit=False)
            schedule.tour = tour  # Set ForeignKey relation correctly
            schedule.slots_booked = 0  # Set booked slots to 0 for new schedules
            
            # Calculate duration in days if not done by model
            if not schedule.duration_days and schedule.start_time and schedule.end_time:
                from datetime import timedelta
                delta = schedule.end_time - schedule.start_time
                schedule.duration_days = max(1, delta.days + (1 if delta.seconds > 0 else 0))
            
            schedule.save()

            # Handle the formset data for the admission records
            for admission_form in formset:
                if admission_form.cleaned_data:
                    admission = admission_form.save(commit=False)
                    admission.sched_id = schedule
                    admission.save()

            # Redirect to itinerary page
            return redirect('tour_app:itinerary', sched_id=schedule.sched_id)

        else:
            print("Form Errors:", form.errors)
            print("Formset Errors:", formset.errors)

    else:
        form = TourScheduleForm()
        formset = AdmissionFormSet()

    return render(request, 'create_schedule.html', {
        'form': form,
        'formset': formset,
        'tour': tour,  # Pass the current tour to the template
        'admission_rates': Admission_Rates.objects.all(),
    })

@admin_employee_required
def itinerary(request, sched_id):
    schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
    tour = schedule.tour  # Get the related tour object
    
    # Get the duration of the tour in days
    duration_days = schedule.duration_days
    
    # Prepare form for adding events
    if request.method == 'POST':
        # Get form data
        day_number = int(request.POST.get('day_number', 1))
        event_time = request.POST.get('event_time')
        event_name = request.POST.get('event_name')
        event_description = request.POST.get('event_description')
        event_location = request.POST.get('event_location', '').strip()
        image = request.FILES.get('image')
        
        # Validate day number is within duration
        if day_number < 1 or day_number > duration_days:
            messages.error(request, f"Day number must be between 1 and {duration_days}")
            return redirect('tour_app:itinerary', sched_id=schedule.sched_id)
        
        # Create and save the new event
        new_event = Tour_Event(
            sched_id=schedule,
            day_number=day_number,
            event_time=event_time,
            event_name=event_name,
            event_description=event_description,
            event_location=event_location,
            image=image
        )
        new_event.save()
        messages.success(request, f"Event '{event_name}' added to Day {day_number} itinerary!")
        
        return redirect('tour_app:itinerary', sched_id=schedule.sched_id)
    
    # Fetch events and organize by day
    all_events = Tour_Event.objects.filter(sched_id=schedule).order_by('day_number', 'event_time')
    
    # Group events by day
    events_by_day = {}
    for day in range(1, duration_days + 1):
        events_by_day[day] = [event for event in all_events if event.day_number == day]
    
    # Create a list of day choices for the form dropdown
    day_choices = [(day, f"Day {day}") for day in range(1, duration_days + 1)]
    
    return render(request, 'itinerary.html', {
        'tour': tour,
        'events_by_day': events_by_day,
        'schedule': schedule,
        'duration_days': duration_days,
        'day_choices': day_choices,
    })

@admin_employee_required
def home(request):
    selected_status = (request.GET.get('status') or 'all').strip().lower()
    tours_qs = Tour_Add.objects.all().order_by('tour_name')
    if selected_status in {'draft', 'published', 'archived'}:
        tours_qs = tours_qs.filter(publication_status=selected_status)

    tours = list(tours_qs)
    welcome_name = request.session.get('company_name') or "User"
    pending_suggestions_count = Pending.objects.filter(status="Pending").count()

    context = {
        'tours': tours,
        'request': request,  # Add request object to context
        'selected_status': selected_status,
        'tour_count': len(tours),
        'pending_suggestions_count': pending_suggestions_count,
        'welcome_name': welcome_name,
    }
    return render(request, 'home.html', context)


@admin_employee_required
def tour_detail(request, tour_id):
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    schedules = Tour_Schedule.objects.filter(tour=tour)
    events = Tour_Event.objects.filter(sched_id__in=schedules)

    # Generate the calendar
    today = date.today()
    first_day_of_month = date(today.year, today.month, 1)
    _, last_day = calendar.monthrange(today.year, today.month)
    last_day_of_month = date(today.year, today.month, last_day)

    days = []
    current_day = first_day_of_month
    while current_day <= last_day_of_month:
        has_tour = any(sched.start_time.date() <= current_day <= sched.end_time.date() for sched in schedules)
        is_tour_start = any(sched.start_time.date() == current_day for sched in schedules)
        is_tour_end = any(sched.end_time.date() == current_day for sched in schedules)
        is_tour_range = has_tour and not is_tour_start and not is_tour_end

        days.append({
            'day': current_day.day,
            'is_current_month': True,
            'has_tour': has_tour,
            'is_tour_start': is_tour_start,
            'is_tour_range': is_tour_range,
            'is_tour_end': is_tour_end
        })
        current_day += timedelta(days=1)

    # Organize days into weeks
    calendar_weeks = []
    week = []
    for day in days:
        week.append(day)
        if len(week) == 7:
            calendar_weeks.append(week)
            week = []
    if week:
        calendar_weeks.append(week)

    context = {
        'tour': tour,
        'schedules': schedules,
        'events': events,
        'calendar': calendar_weeks,
        'current_month': today.strftime("%B %Y"),
    }

    return render(request, 'tour_detail.html', context)


@admin_employee_required
def pending_view(request):
    employee, is_admin = _get_employee_session_context(request)
    if employee is None:
        messages.error(request, "Session expired. Please log in again.")
        return redirect('admin_app:login')

    # Admin safeguard: explicit override is required to approve/reject from this screen.
    # Employees can still update statuses for schedules assigned to them.
    allow_override = str(request.GET.get("override") or "").strip() == "1"

    if request.method == 'POST':
        print("\n🔵 POST Request Received 🔵")
        print("📩 Request Data:", request.POST)  # Debugging print

        try:
            # Fetch required form fields
            num_adults = int(request.POST.get('num_adults', 0))
            num_children = int(request.POST.get('num_children', 0))
            total_guests = num_adults + num_children

            # This panel is used by employee/admin accounts; guest is selected explicitly.
            guest_id = (request.POST.get('guest_id') or '').strip()
            if not guest_id:
                messages.error(request, "Guest ID is required.")
                return redirect('tour_app:pending_view')
            guest = get_object_or_404(Guest, guest_id=guest_id)

            # Get the selected schedule and tour from session or request
            sched_id = request.session.get('selected_schedule_id')
            tour_id = request.session.get('selected_tour_id')

            if not sched_id or not tour_id:
                messages.error(request, "Missing schedule or tour information.")
                return redirect('tour_app:pending_view')

            schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
            tour = get_object_or_404(Tour_Add, tour_id=tour_id)

            if not is_admin:
                assigned_schedule_ids = set(_assigned_schedule_ids_for_employee(employee))
                if schedule.sched_id not in assigned_schedule_ids:
                    messages.error(request, "You can only manage bookings for tours assigned to you.")
                    return redirect('tour_app:pending_view')

            print(f"👤 Guest: {guest}, 📅 Schedule: {schedule}, 🎟️ Tour: {tour}, 👥 Total Guests: {total_guests}")

            # Ensure that there are enough available slots
            if schedule.slots_available < total_guests:
                messages.error(request, "Not enough available slots.")
                return redirect('tour_app:pending_view')

            # Create a new Pending booking entry
            pending = Pending.objects.create(
                guest_id=guest,
                sched_id=schedule,
                tour_id=tour,
                status="Pending",
                total_guests=total_guests,
                your_name=f"{guest.first_name} {guest.last_name}",
                your_email=guest.email,
                your_phone=guest.phone_number,
                num_adults=num_adults,
                num_children=num_children
            )
            create_notification(
                recipient_guest=guest,
                title="Tour booking submitted",
                message=f"Your booking request for {tour.tour_name} is pending staff review.",
                notification_type="booking",
                url=reverse("main-page") + "#user-bookings",
                dedupe_key=f"tour-pending-{pending.id}",
                related_object_id=str(pending.id),
            )
            notify_assigned_employees_for_schedule(
                schedule=schedule,
                title="New tour booking request",
                message=f"{tour.tour_name} received a new pending booking from {guest.first_name} {guest.last_name}.",
                notification_type="booking",
                url=reverse("tour_app:pending_view"),
                dedupe_key_prefix=f"tour-pending-{pending.id}",
            )

            # Update slots availability in the schedule
            schedule.slots_booked += total_guests
            schedule.slots_available -= total_guests
            schedule.save()

            print("✅ Booking saved successfully:", pending)
            messages.success(request, "Booking added successfully!")
            return redirect('tour_app:pending_view')  # Ensure correct namespace

        except ObjectDoesNotExist as e:
            messages.error(request, f"Error: {e}")
            return redirect('tour_app:pending_view')

        except Exception as e:
            print("❌ ERROR:", e)
            messages.error(request, f"Unexpected error: {e}")
            return redirect('tour_app:pending_view')

    # Keep schedule status synchronized before showing booking action queues.
    Tour_Schedule.get_tour_statistics()
    now = timezone.now()
    base_qs = Pending.objects.select_related('guest_id', 'tour_id', 'sched_id')
    if not is_admin:
        assigned_schedule_ids = _assigned_schedule_ids_for_employee(employee)
        base_qs = base_qs.filter(sched_id_id__in=assigned_schedule_ids)

    # Fetch all pending, accepted, and declined bookings
    pending_bookings = (
        base_qs.filter(status__iexact="Pending", sched_id__end_time__gte=now)
        .exclude(sched_id__status__iexact="cancelled")
    )
    accepted_bookings = base_qs.filter(status__iexact="Accepted")
    declined_bookings = base_qs.filter(status__iexact="Declined")
    # Add cancelled bookings by users
    cancelled_by_guest_bookings = base_qs.filter(status__iexact="Cancelled")

    return render(request, 'pending.html', {
        'pending_bookings': pending_bookings,
        'accepted_bookings': accepted_bookings,
        'declined_bookings': declined_bookings,
        'cancelled_by_guest_bookings': cancelled_by_guest_bookings,
        'is_admin': is_admin,
        'allow_override': allow_override,
        'can_update_booking_status': (not is_admin) or allow_override,
    })

#this handles the declined status of bookings 
# Apply decorator to class-based view
class StatusUpdateView(UpdateView):
    model = Pending
    fields = ['status']
    template_name = 'pending.html'
    success_url = reverse_lazy('tour_app:pending_view')

    @method_decorator(admin_employee_required)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def form_valid(self, form):
        instance = self.get_object()
        employee, is_admin = _get_employee_session_context(self.request)
        if employee is None:
            messages.error(self.request, "Session expired. Please log in again.")
            return redirect('admin_app:login')

        requested_status = str(self.request.POST.get("status") or "").strip()
        if requested_status not in {"Accepted", "Declined"}:
            messages.error(self.request, "Only Accepted or Declined status is allowed.")
            return redirect(self.success_url)

        if str(instance.status or "").strip() != "Pending":
            messages.error(self.request, "Only pending bookings can be updated.")
            return redirect(self.success_url)

        allow_override = str(self.request.POST.get("allow_override") or "").strip() == "1"
        if is_admin and not allow_override:
            messages.error(
                self.request,
                "Admin monitoring mode is enabled. Direct approve/reject is blocked unless override is explicitly enabled.",
            )
            return redirect(self.success_url)

        if not is_admin:
            is_assigned = TourAssignment.objects.filter(
                employee=employee,
                schedule=instance.sched_id,
            ).exists()
            if not is_assigned:
                messages.error(self.request, "You can only update bookings assigned to your tours.")
                return redirect(self.success_url)

        if requested_status == "Accepted":
            schedule = instance.sched_id
            if str(schedule.status or "").lower() == "cancelled":
                messages.error(self.request, "This tour schedule is cancelled and cannot accept new bookings.")
                return redirect(self.success_url)
            if int(instance.total_guests or 0) < 1:
                messages.error(self.request, "Invalid booking: total guests must be at least 1.")
                return redirect(self.success_url)
            if int(schedule.slots_available or 0) < 0:
                messages.error(self.request, "Schedule availability is inconsistent. Please review capacity first.")
                return redirect(self.success_url)

        instance.status = requested_status
        status_key = str(requested_status).strip().lower()
        if status_key in {"accepted", "declined"}:
            create_notification(
                recipient_guest=instance.guest_id,
                title=f"Tour booking {status_key}",
                message=f"Your booking for {instance.tour_id.tour_name} was {status_key} by staff.",
                notification_type="booking",
                url=reverse("main-page") + "#user-bookings",
                dedupe_key=f"tour-status-{instance.id}-{status_key}",
                related_object_id=str(instance.id),
            )
        if status_key == "accepted":
            _safe_log_tour_status_event(
                request=self.request,
                user=instance.guest_id,
                event_type="book",
                item_ref="tour_booking_approved",
            )
        elif status_key == "declined":
            _safe_log_tour_status_event(
                request=self.request,
                user=instance.guest_id,
                event_type="book",
                item_ref="tour_booking_declined",
            )
        
        # Resolve recipient from trusted persisted data first, then form fallback.
        guest_email = (
            str(getattr(instance.guest_id, "email", "") or "").strip()
            or str(getattr(instance, "your_email", "") or "").strip()
            or str(self.request.POST.get("guest_email") or "").strip()
        )
        guest_name = (
            str(getattr(instance, "your_name", "") or "").strip()
            or f"{getattr(instance.guest_id, 'first_name', '')} {getattr(instance.guest_id, 'last_name', '')}".strip()
            or str(self.request.POST.get("guest_name") or "").strip()
            or "Guest"
        )
        
        # Handle email notifications
        if str(instance.status or "").strip().lower() == "accepted":
            # For accepted bookings, send confirmation email
            subject = f"Booking Confirmation: {instance.tour_id.tour_name}"
            payment_url = "https://bayawancity.gov.ph/payments/tour-booking"
            
            # Calculate prices for email - get price from schedule instead of tour
            price_per_adult = instance.sched_id.price if hasattr(instance.sched_id, 'price') else 0
            price_per_child = price_per_adult  # Same price for children (not half price)
            adults_subtotal = instance.num_adults * price_per_adult
            children_subtotal = instance.num_children * price_per_child
            total_amount = adults_subtotal + children_subtotal
            
            html_message = render_to_string('email/booking_confirmation.html', {
                'name': guest_name,
                'tour_name': instance.tour_id.tour_name,
                'start_time': instance.sched_id.start_time.strftime("%B %d, %Y, %I:%M %p"),
                'end_time': instance.sched_id.end_time.strftime("%B %d, %Y, %I:%M %p"),
                'num_adults': instance.num_adults,
                'num_children': instance.num_children,
                'total_guests': instance.total_guests,
                'price_per_adult': price_per_adult,
                'price_per_child': price_per_child,
                'adults_subtotal': adults_subtotal,
                'children_subtotal': children_subtotal,
                'total_amount': total_amount,
                'payment_url': payment_url,
            })
            plain_message = strip_tags(html_message)
            
            try:
                if not guest_email:
                    raise ValueError("Guest email is missing for accepted booking notification.")

                email_message = EmailMultiAlternatives(
                    subject=subject,
                    body=plain_message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[guest_email],
                )
                email_message.attach_alternative(html_message, "text/html")

                # Inline QR code for the official Treasurer payment page.
                qr = qrcode.QRCode(box_size=6, border=2)
                qr.add_data(payment_url)
                qr.make(fit=True)
                qr_image = qr.make_image(fill_color="black", back_color="white")
                qr_buffer = BytesIO()
                qr_image.save(qr_buffer, format="PNG")
                qr_mime = MIMEImage(qr_buffer.getvalue(), _subtype="png")
                qr_mime.add_header("Content-ID", "<tour_payment_qr>")
                qr_mime.add_header("Content-Disposition", "inline", filename="tour_payment_qr.png")
                email_message.attach(qr_mime)

                email_message.send(fail_silently=False)
                messages.success(self.request, f"Confirmation email sent to {guest_email}")
                _safe_log_tour_email_dispatch(
                    email_type="tour_approval_email_sent",
                    success=True,
                )
            except Exception as e:
                messages.error(self.request, f"Failed to send email: {e}")
                _safe_log_tour_email_dispatch(
                    email_type="tour_approval_email_failed",
                    success=False,
                    error_message=str(e),
                )
                
        elif str(instance.status or "").strip().lower() == "declined":
            # Send declined notification
            subject = f"Booking Update: {instance.tour_id.tour_name} - {instance.status}"
            
            html_message = render_to_string('email/booking_declined.html', {
                'name': guest_name,
                'tour': instance.tour_id.tour_name,
                'start_time': instance.sched_id.start_time.strftime("%B %d, %Y, %I:%M %p"),
                'end_time': instance.sched_id.end_time.strftime("%B %d, %Y, %I:%M %p"),
                'adults': instance.num_adults,
                'children': instance.num_children,
                'total_guests': instance.total_guests
            })
            plain_message = strip_tags(html_message)
            
            try:
                if not guest_email:
                    raise ValueError("Guest email is missing for declined booking notification.")

                send_mail(
                    subject,
                    plain_message,
                    settings.DEFAULT_FROM_EMAIL,
                    [guest_email],
                    html_message=html_message,
                    fail_silently=False,
                )
                messages.success(self.request, f"Notification email sent to {guest_email}")
                _safe_log_tour_email_dispatch(
                    email_type="tour_rejection_email_sent",
                    success=True,
                )
            except Exception as e:
                messages.error(self.request, f"Failed to send email: {e}")
                _safe_log_tour_email_dispatch(
                    email_type="tour_rejection_email_failed",
                    success=False,
                    error_message=str(e),
                )
        
        instance.save(update_fields=["status"])
        return redirect(self.success_url)

    def form_invalid(self, form):
        print("Form invalid:", form.errors)
        return HttpResponse("Error: Form submission failed", status=400)


@admin_employee_required
def add_admission_rate(request):
    age_categories = ["Infant", "Toddler", "Child", "Teen", "Adult", "Middle Adult", "Senior"]
    disability_categories = ["PWD", "Visually Impaired", "Hearing Impaired", "Mobility Impaired"]

    def _discount_label(group_name, category_name):
        return f"{group_name} Discount - {category_name}"

    def _upsert_discount(group_name, category_name, percentage, status):
        label = _discount_label(group_name, category_name)
        pct_value = Decimal(str(percentage or 0))
        if str(status).lower() != "active":
            pct_value = Decimal("0")
        rate_obj, _ = Admission_Rates.objects.get_or_create(
            tour_id=None,
            payables=label,
            defaults={"price": pct_value},
        )
        rate_obj.price = pct_value
        rate_obj.tour_id = None
        rate_obj.save()

    if request.method == "POST":
        rate_id = request.POST.get("rate_id")
        action = request.POST.get("action")

        if action == "save_all_age":
            count = int(request.POST.get("age_count", 0) or 0)
            for idx in range(count):
                category = request.POST.get(f"age_category_{idx}", "").strip()
                percentage = request.POST.get(f"age_percentage_{idx}", "0").strip() or "0"
                status = request.POST.get(f"age_status_{idx}", "active").strip().lower()
                if category:
                    _upsert_discount("Age", category, percentage, status)
            messages.success(request, "Age discounts updated successfully.")

        elif action == "save_all_disability":
            count = int(request.POST.get("disability_count", 0) or 0)
            for idx in range(count):
                category = request.POST.get(f"disability_category_{idx}", "").strip()
                percentage = request.POST.get(f"disability_percentage_{idx}", "0").strip() or "0"
                status = request.POST.get(f"disability_status_{idx}", "active").strip().lower()
                if category:
                    _upsert_discount("Disability", category, percentage, status)
            messages.success(request, "Disability discounts updated successfully.")

        elif action == "seed_age":
            for category in age_categories:
                Admission_Rates.objects.get_or_create(
                    tour_id=None,
                    payables=_discount_label("Age", category),
                    defaults={"price": Decimal("0")},
                )
            messages.success(request, "Age discount categories prepared.")

        elif action == "seed_disability":
            for category in disability_categories:
                Admission_Rates.objects.get_or_create(
                    tour_id=None,
                    payables=_discount_label("Disability", category),
                    defaults={"price": Decimal("0")},
                )
            messages.success(request, "Disability discount categories prepared.")

        elif action == "delete":
            # Handle deletion
            try:
                rate = Admission_Rates.objects.get(rate_id=rate_id)
                rate.delete()
            except Admission_Rates.DoesNotExist:
                pass  # Ignore if the rate doesn't exist

        elif action == "update":
            # Handle update
            tour_id = request.POST.get("tour_id")
            payables = request.POST.get("payables")
            price = request.POST.get("price")
            try:
                rate = Admission_Rates.objects.get(rate_id=rate_id)
                # Set tour_id to None if empty string is provided
                if tour_id:
                    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
                    rate.tour_id = tour
                else:
                    rate.tour_id = None
                rate.payables = payables
                rate.price = price
                rate.save()
            except Admission_Rates.DoesNotExist:
                pass  # Ignore if the rate doesn't exist

        elif action == "add":
            # Handle creation of new admission rate
            tour_id = request.POST.get("tour_id")
            payables = request.POST.get("payables")
            price = request.POST.get("price")
            
            # Create the admission rate with the tour_id if provided
            if tour_id:
                tour = get_object_or_404(Tour_Add, tour_id=tour_id)
                Admission_Rates.objects.create(tour_id=tour, payables=payables, price=price)
            else:
                Admission_Rates.objects.create(payables=payables, price=price)

        return redirect("tour_app:admission_rate")  # Redirect to refresh the table

    # Get all tours to populate the dropdown
    tours = Tour_Add.objects.all().order_by("tour_name")
    admission_rates = Admission_Rates.objects.select_related("tour_id").all().order_by("rate_id")

    rates_by_payable = {row.payables: row for row in admission_rates if row.tour_id_id is None}

    age_discount_rows = []
    for category in age_categories:
        key = _discount_label("Age", category)
        row = rates_by_payable.get(key)
        price = float(row.price) if row else 0.0
        age_discount_rows.append({
            "category": category,
            "price": price,
            "status": "active" if price > 0 else "inactive",
        })

    disability_discount_rows = []
    for category in disability_categories:
        key = _discount_label("Disability", category)
        row = rates_by_payable.get(key)
        price = float(row.price) if row else 0.0
        disability_discount_rows.append({
            "category": category,
            "price": price,
            "status": "active" if price > 0 else "inactive",
        })

    return render(
        request,
        "admission_rate.html",
        {
            "admission_rates": admission_rates,
            "tours": tours,
            "age_discount_rows": age_discount_rows,
            "disability_discount_rows": disability_discount_rows,
        },
    )


@admin_employee_required
def cancel_tour_view(request):
    """View for cancelling tour schedules"""
    if request.method == 'POST':
        sched_id = request.POST.get('sched_id')
        cancellation_reason = request.POST.get('cancellation_reason', '')
        
        if not sched_id:
            messages.error(request, "No schedule ID provided")
            return redirect('tour_app:cancel_tour')
            
        try:
            tour_schedule = Tour_Schedule.objects.get(sched_id=sched_id)
            
            # Cancel the tour and store the reason
            tour_schedule.cancel_tour(reason=cancellation_reason)
            
            # Get all pending bookings for this schedule
            pending_bookings = Pending.objects.filter(sched_id=tour_schedule)
            
            # Update all pending bookings to "Cancelled"
            for booking in pending_bookings:
                booking.status = "Cancelled"
                booking.save()
                
            messages.success(request, f"Tour {tour_schedule.tour.tour_name} ({tour_schedule.sched_id}) has been cancelled successfully.")
            
            # Send cancellation emails
            for booking in pending_bookings:
                guest_email = booking.your_email
                guest_name = booking.your_name
                
                # Once you create the tour_cancellation.html template:
                html_message = render_to_string('email/tour_cancellation.html', {
                    'name': guest_name,
                    'tour': tour_schedule.tour.tour_name,
                    'reason': cancellation_reason,
                    'start_time': tour_schedule.start_time.strftime("%B %d, %Y, %I:%M %p"),
                })
                plain_message = strip_tags(html_message)
                
                subject = f"Important: Your Booking for {tour_schedule.tour.tour_name} has been Cancelled"
                
                try:
                    send_mail(
                        subject,
                        plain_message,
                        settings.DEFAULT_FROM_EMAIL,
                        [guest_email],
                        html_message=html_message,
                        fail_silently=False,
                    )
                except Exception as e:
                    print(f"Failed to send cancellation email to {guest_email}: {e}")
            
        except Tour_Schedule.DoesNotExist:
            messages.error(request, "Tour schedule not found")
        except Exception as e:
            messages.error(request, f"Error cancelling tour: {str(e)}")
            
        return redirect('tour_app:cancel_tour')
    
    # For GET requests, show active and completed tours that can be cancelled
    active_tours = Tour_Schedule.objects.filter(status='active').select_related('tour')
    completed_tours = Tour_Schedule.objects.filter(status='completed').select_related('tour')
    cancelled_tours = Tour_Schedule.objects.filter(status='cancelled').select_related('tour')
    
    context = {
        'active_tours': active_tours,
        'completed_tours': completed_tours,
        'cancelled_tours': cancelled_tours,
    }
    
    return render(request, 'cancel_tour.html', context)


@admin_employee_required
def update_schedules(request, tour_id):
    """View for updating schedules of a specific tour"""
    # Get the tour
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    
    # Get all schedules for this tour
    schedules = Tour_Schedule.objects.filter(tour=tour)
    
    # Handle POST request (when updating a schedule)
    if request.method == 'POST':
        schedule_id = request.POST.get('schedule_id')
        action = request.POST.get('action')
        
        if action == 'update' and schedule_id:
            # Get the schedule to update
            schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
            
            # Update fields
            schedule.start_time = request.POST.get('start_time', schedule.start_time)
            schedule.end_time = request.POST.get('end_time', schedule.end_time)
            schedule.total_slots = request.POST.get('total_slots', schedule.total_slots)
            
            # Recalculate slots available
            if 'total_slots' in request.POST:
                # Calculate the difference between new and old total slots
                slots_diff = int(request.POST.get('total_slots')) - schedule.total_slots
                # Update slots available accordingly
                schedule.slots_available += slots_diff
            
            schedule.save()
            messages.success(request, f"Schedule {schedule_id} updated successfully!")
            
        elif action == 'delete' and schedule_id:
            # Delete the schedule
            schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
            schedule.delete()
            messages.success(request, f"Schedule {schedule_id} deleted successfully!")
    
    # Refresh the schedules list
    schedules = Tour_Schedule.objects.filter(tour=tour)
    
    # Render the update schedules template
    return render(request, 'update_schedules.html', {
        'tour': tour,
        'schedules': schedules
    })


@admin_employee_required
def update_itinerary(request, tour_id):
    """View for updating itineraries of a specific tour"""
    # Get the tour
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    
    # Get all schedules for this tour
    schedules = Tour_Schedule.objects.filter(tour=tour)
    
    selected_schedule_id = request.GET.get('schedule_id')
    
    if selected_schedule_id:
        # Get the selected schedule
        selected_schedule = get_object_or_404(Tour_Schedule, sched_id=selected_schedule_id)
        # Get events for the selected schedule
        events = Tour_Event.objects.filter(sched_id=selected_schedule)
    else:
        selected_schedule = None
        events = None
    
    # Handle POST request (when updating an event)
    if request.method == 'POST':
        event_id = request.POST.get('event_id')
        action = request.POST.get('action')
        
        if action == 'update' and event_id:
            # Get the event to update
            event = get_object_or_404(Tour_Event, id=event_id)
            
            # Update fields
            event.event_time = request.POST.get('event_time', event.event_time)
            event.event_name = request.POST.get('event_name', event.event_name)
            event.event_description = request.POST.get('event_description', event.event_description)
            event.event_location = request.POST.get('event_location', event.event_location)
            
            # Handle image update if provided
            if 'image' in request.FILES:
                event.image = request.FILES['image']
            
            event.save()
            messages.success(request, f"Event '{event.event_name}' updated successfully!")
            
        elif action == 'delete' and event_id:
            # Delete the event
            event = get_object_or_404(Tour_Event, id=event_id)
            event_name = event.event_name
            event.delete()
            messages.success(request, f"Event '{event_name}' deleted successfully!")
            
        elif action == 'add' and selected_schedule_id:
            # Create a new event
            event_time = request.POST.get('event_time')
            event_name = request.POST.get('event_name')
            event_description = request.POST.get('event_description')
            event_location = request.POST.get('event_location', '').strip()
            image = request.FILES.get('image')
            
            # Create and save the new event
            new_event = Tour_Event(
                sched_id=selected_schedule,
                event_time=event_time,
                event_name=event_name,
                event_description=event_description,
                event_location=event_location,
                image=image
            )
            new_event.save()
            messages.success(request, f"Event '{event_name}' added successfully!")
        
        # Redirect to same page with schedule_id parameter to maintain context
        if selected_schedule_id:
            return redirect(f"{request.path}?schedule_id={selected_schedule_id}")
        return redirect(request.path)
    
    # Render the update itinerary template
    return render(request, 'update_itinerary.html', {
        'tour': tour,
        'schedules': schedules,
        'selected_schedule': selected_schedule,
        'events': events
    })


# API endpoints for AJAX functionality
@admin_employee_required
def get_tour_schedules_api(request, tour_id):
    """API to get all schedules for a specific tour in JSON format"""
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    schedules = Tour_Schedule.objects.filter(tour=tour)
    
    schedules_data = []
    for schedule in schedules:
        schedules_data.append({
            'schedule_id': schedule.sched_id,
            'start_time': schedule.start_time.isoformat(),
            'end_time': schedule.end_time.isoformat(),
            'total_slots': schedule.slots_available + schedule.slots_booked,
            'slots_available': schedule.slots_available,
            'slots_booked': schedule.slots_booked,
            'status': schedule.status
        })
    
    return JsonResponse(schedules_data, safe=False)

@admin_employee_required
def get_schedule_details(request, schedule_id):
    """API to get details of a specific schedule in JSON format"""
    try:
        print(f"Getting schedule details for ID: {schedule_id}, type: {type(schedule_id)}")
        schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
        
        schedule_data = {
            'schedule_id': schedule.sched_id,
            'tour_id': schedule.tour.tour_id,
            'tour_name': schedule.tour.tour_name,
            'start_time': schedule.start_time.isoformat(),
            'end_time': schedule.end_time.isoformat(),
            'total_slots': schedule.slots_available + schedule.slots_booked,
            'slots_available': schedule.slots_available,
            'slots_booked': schedule.slots_booked,
            'price': schedule.price,
            'status': schedule.status
        }
        
        print(f"Returning schedule data: {schedule_data}")
        return JsonResponse(schedule_data)
    except Exception as e:
        print(f"Error getting schedule details: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@admin_employee_required
def update_schedule(request, schedule_id):
    """API to update a specific schedule"""
    try:
        print(f"Updating schedule ID: {schedule_id}, type: {type(schedule_id)}")
        print(f"POST data: {request.POST}")
        schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
        
        if request.method == 'POST':
            # Update schedule fields from POST data
            start_time = request.POST.get('start_time')
            end_time = request.POST.get('end_time')
            total_slots = request.POST.get('total_slots')
            slots_available = request.POST.get('slots_available')
            slots_booked = request.POST.get('slots_booked')
            price = request.POST.get('price')
            
            print(f"Updating with: start_time={start_time}, end_time={end_time}, total_slots={total_slots}, slots_available={slots_available}, slots_booked={slots_booked}, price={price}")
            
            # Convert datetime fields properly
            if start_time:
                try:
                    parsed_start_time = parse_datetime(start_time)
                    if parsed_start_time:
                        # Add timezone if datetime is naive
                        if timezone.is_naive(parsed_start_time):
                            parsed_start_time = timezone.make_aware(parsed_start_time)
                        schedule.start_time = parsed_start_time
                    else:
                        print(f"Could not parse start_time: {start_time}")
                except Exception as e:
                    print(f"Error parsing start_time: {e}")
                    
            if end_time:
                try:
                    parsed_end_time = parse_datetime(end_time)
                    if parsed_end_time:
                        # Add timezone if datetime is naive
                        if timezone.is_naive(parsed_end_time):
                            parsed_end_time = timezone.make_aware(parsed_end_time)
                        schedule.end_time = parsed_end_time
                    else:
                        print(f"Could not parse end_time: {end_time}")
                except Exception as e:
                    print(f"Error parsing end_time: {e}")
            
            # Directly update slots values from form inputs
            if slots_available and total_slots:
                try:
                    # Convert to integers
                    slots_available_int = int(slots_available)
                    total_slots_int = int(total_slots)
                    slots_booked_int = int(slots_booked) if slots_booked else schedule.slots_booked
                    
                    # Validate that slots available + booked = total slots
                    if slots_available_int + slots_booked_int != total_slots_int:
                        print(f"Warning: slots_available ({slots_available_int}) + slots_booked ({slots_booked_int}) != total_slots ({total_slots_int})")
                        print("Adjusting slots_available to maintain consistency")
                        slots_available_int = total_slots_int - slots_booked_int
                    
                    # Update the schedule with the validated values
                    schedule.slots_available = max(0, slots_available_int)  # Ensure non-negative
                    
                    print(f"Updated slots: available={schedule.slots_available}, booked={slots_booked_int}, total={total_slots_int}")
                except Exception as e:
                    print(f"Error updating slots: {e}")
                    
            if price:
                try:
                    # Remove any currency symbols and convert to float
                    clean_price = price.replace('₱', '').strip()
                    schedule.price = float(clean_price)
                except Exception as e:
                    print(f"Error updating price: {e}")
            
            # Ensure end time is after start time
            if schedule.end_time <= schedule.start_time:
                return JsonResponse({
                    'success': False, 
                    'message': 'End time must be after start time'
                }, status=400)
                
            # Save the updated schedule
            schedule.save()
            
            # Handle payables update
            # First, clear existing admissions
            Tour_Admission.objects.filter(sched_id=schedule).delete()
            
            # Get the payable count
            payable_count = int(request.POST.get('payable_count', 0))
            
            # Create new admissions
            for index in range(payable_count):
                rate_id = request.POST.get(f'payables-{index}')
                amount = request.POST.get(f'amount-{index}')
                
                if rate_id and amount:
                    try:
                        rate = get_object_or_404(Admission_Rates, rate_id=rate_id)
                        admission = Tour_Admission(
                            sched_id=schedule,
                            payables=rate,
                            amount=float(amount)
                        )
                        admission.save()
                        print(f"Saved admission: {rate.payables} - {amount}")
                    except Exception as e:
                        print(f"Error saving admission {index}: {e}")
            
            return JsonResponse({'success': True, 'message': 'Schedule updated successfully'})
        
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=400)
    except Exception as e:
        print(f"Error updating schedule: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@admin_employee_required
def get_schedule_payables(request, schedule_id):
    """API to get payables for a specific schedule"""
    try:
        print(f"Getting payables for schedule ID: {schedule_id}, type: {type(schedule_id)}")
        schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
        admissions = Tour_Admission.objects.filter(sched_id=schedule)
        
        payables_data = []
        for admission in admissions:
            payables_data.append({
                'admission_id': admission.Admis_id,
                'rate_id': admission.payables.rate_id,
                'payable_name': admission.payables.payables,
                'amount': float(admission.amount)
            })
        
        print(f"Found {len(payables_data)} payables for schedule {schedule_id}")
        return JsonResponse(payables_data, safe=False)
    except Exception as e:
        print(f"Error getting schedule payables: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@admin_employee_required
def get_admission_rates_json(request):
    """API to get all admission rates in JSON format"""
    rates = Admission_Rates.objects.all()
    
    rates_data = []
    for rate in rates:
        rate_data = {
            'rate_id': rate.rate_id,
            'payables': rate.payables,
            'price': float(rate.price)
        }
        
        if rate.tour_id:
            rate_data['tour_id'] = rate.tour_id.tour_id
            rate_data['tour_name'] = rate.tour_id.tour_name
            
        rates_data.append(rate_data)
    
    return JsonResponse(rates_data, safe=False)

@admin_employee_required
def get_schedule_events(request, schedule_id):
    """API to get all events for a specific schedule in JSON format"""
    schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
    events = Tour_Event.objects.filter(sched_id=schedule)
    
    events_data = []
    for event in events:
        event_data = {
            'event_id': event.event_ID,
            'event_name': event.event_name,
            'event_time': event.event_time.strftime('%H:%M'),
            'event_description': event.event_description,
            'event_location': event.event_location,
            'has_image': bool(event.image)
        }
        if event.image:
            event_data['image_url'] = event.image.url
        events_data.append(event_data)
    
    return JsonResponse(events_data, safe=False)

@admin_employee_required
def get_event_details(request, event_id):
    """API to get details of a specific event in JSON format"""
    event = get_object_or_404(Tour_Event, event_ID=event_id)
    
    event_data = {
        'event_id': event.event_ID,
        'schedule_id': event.sched_id.sched_id,
        'event_name': event.event_name,
        'event_time': event.event_time.strftime('%H:%M'),
        'event_description': event.event_description,
        'event_location': event.event_location,
        'has_image': bool(event.image)
    }
    
    if event.image:
        event_data['image_url'] = event.image.url
    
    return JsonResponse(event_data)

@admin_employee_required
def update_event(request, event_id):
    """API to update a specific event"""
    event = get_object_or_404(Tour_Event, event_ID=event_id)
    
    if request.method == 'POST':
        # Update event fields from POST data
        event_time = request.POST.get('event_time')
        event_name = request.POST.get('event_name')
        event_description = request.POST.get('event_description')
        event_location = request.POST.get('event_location')
        day_number = request.POST.get('day_number')
        
        if event_time:
            event.event_time = event_time
        if event_name:
            event.event_name = event_name
        if event_description:
            event.event_description = event_description
        if event_location is not None:
            event.event_location = event_location
        if day_number:
            event.day_number = int(day_number)
        
        # Handle image update if provided
        if 'image' in request.FILES:
            event.image = request.FILES['image']
        
        event.save()
        messages.success(request, f"Event '{event_name}' updated successfully!")
        return redirect('tour_app:itinerary', sched_id=event.sched_id.sched_id)
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=400)

@admin_employee_required
def add_event(request, schedule_id):
    """API to add a new event to a schedule"""
    try:
        schedule = get_object_or_404(Tour_Schedule, sched_id=schedule_id)
        
        if request.method == 'POST':
            event_time = request.POST.get('event_time')
            event_name = request.POST.get('event_name')
            event_description = request.POST.get('event_description')
            event_location = request.POST.get('event_location', '').strip()
            
            # Create new event
            event = Tour_Event(
                sched_id=schedule,
                event_time=event_time,
                event_name=event_name,
                event_description=event_description,
                event_location=event_location
            )
            
            # Handle image if provided
            if 'image' in request.FILES:
                event.image = request.FILES['image']
                
            event.save()
            
            return JsonResponse({
                'success': True, 
                'message': 'Event added successfully',
                'event_id': event.event_id
            })
        
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=400)
    except Exception as e:
        print(f"Error adding event: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@admin_employee_required
def delete_event(request, event_id):
    """API to delete a specific event"""
    try:
        event = get_object_or_404(Tour_Event, event_ID=event_id)
        
        if request.method == 'POST':
            # Store data before deleting for the response
            event_name = event.event_name
            schedule_id = event.sched_id.sched_id
            
            # Delete the event
            event.delete()
            
            messages.success(request, f"Event '{event_name}' deleted successfully")
            return redirect('tour_app:itinerary', sched_id=schedule_id)
        
        return JsonResponse({'success': False, 'message': 'Invalid request method'}, status=400)
    except Exception as e:
        print(f"Error deleting event: {e}")
        return JsonResponse({'error': str(e)}, status=500)

@admin_employee_required
def get_tour_details(request, tour_id):
    """API endpoint to get tour details for editing"""
    try:
        tour = get_object_or_404(Tour_Add, tour_id=tour_id)
        data = {
            'tour_id': tour.tour_id,
            'tour_name': tour.tour_name,
            'description': tour.description,
            'image_url': tour.image.url if tour.image else None
        }
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

@admin_employee_required
def delete_tour(request, tour_id):
    """API endpoint to delete a tour - only available to admin users"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Only POST method is allowed'}, status=405)
    
    # Check if user is admin
    is_admin = request.session.get('is_admin', False)
    if not is_admin:
        return JsonResponse({
            'success': False, 
            'message': 'Permission denied. Only admin users can delete tours.'
        }, status=403)
    
    try:
        tour = get_object_or_404(Tour_Add, tour_id=tour_id)
        
        # Optional: Add additional checks here, like checking if there are active bookings
        
        # Delete the tour - this will cascade delete related schedules, events, etc.
        tour_name = tour.tour_name  # Save for response message
        tour.delete()
        
        # Add success message
        messages.success(request, f"Tour '{tour_name}' has been deleted successfully.")
        
        return JsonResponse({
            'success': True,
            'message': f"Tour '{tour_name}' has been deleted successfully."
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)

@admin_employee_required
def update_tour(request, tour_id):
    """API endpoint to update tour details"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Only POST method is allowed'}, status=405)

    try:
        tour = get_object_or_404(Tour_Add, tour_id=tour_id)
        
        # Update the tour fields
        tour_name = request.POST.get('tour_name')
        description = request.POST.get('description')
        
        if tour_name:
            tour.tour_name = tour_name
        if description:
            tour.description = description
        
        # Handle image update if provided
        if 'image' in request.FILES and request.FILES['image']:
            tour.image = request.FILES['image']
        
        tour.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)

# Add language utility functions to fetch translated tour content
def get_translated_tour(tour, language):
    """Get translated tour data based on language preference"""
    # Try to get specific translation
    try:
        translation = TourAddTranslation.objects.get(tour=tour, language=language)
        return {
            'tour_id': tour.tour_id,
            'tour_name': translation.tour_name,
            'description': translation.description,
            'image': tour.image,
        }
    except TourAddTranslation.DoesNotExist:
        # Fall back to default language (English)
        return {
            'tour_id': tour.tour_id,
            'tour_name': tour.tour_name,
            'description': tour.description,
            'image': tour.image,
        }

# Update existing views
def tour_list(request):
    """View to display list of tours with translation support"""
    # Get the current language
    current_language = get_current_language(request)
    
    # Get all tours
    tours = Tour_Add.objects.all()
    
    # Get translated tour data
    translated_tours = []
    for tour in tours:
        translated_data = get_translated_tour(tour, current_language)
        translated_tours.append({
            'tour': tour,
            'translated': translated_data
        })
    
    return render(request, 'tours/tour_list.html', {
        'tours': tours,
        'translated_tours': translated_tours,
        'current_language': current_language
    })

# Other views would be updated similarly

# Add a view to update tour translations
def update_tour_translation(request, tour_id):
    """View to update tour translations"""
    tour = get_object_or_404(Tour_Add, tour_id=tour_id)
    
    if request.method == 'POST':
        language = request.POST.get('language')
        tour_name = request.POST.get('tour_name')
        description = request.POST.get('description')
        
        # Create or update translation
        translation, created = TourAddTranslation.objects.update_or_create(
            tour=tour,
            language=language,
            defaults={
                'tour_name': tour_name,
                'description': description
            }
        )
        
        messages.success(request, f'Translation for {language} updated successfully.')
        return redirect('tour_detail', tour_id=tour_id)
    
    # Get existing translations
    translations = TourAddTranslation.objects.filter(tour=tour)
    
    return render(request, 'tours/tour_translation.html', {
        'tour': tour,
        'translations': translations
    })

    allow_override = str(request.GET.get("override") or "").strip() == "1"
