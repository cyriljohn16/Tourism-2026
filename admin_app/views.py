from django.contrib.auth import logout, login as auth_login, authenticate
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib import messages
from .forms import (
    EmployeeRegistrationForm,
    AccommodationRegistrationForm,
    AdminAccommodationEncodeForm,
    OwnerAccommodationEditForm,
)
from .models import (
    AdminInfo,
    Accomodation,
    Employee,
    UserActivity,
    InAppNotification,
    OwnerMonthlyReport,
    MonthlyReportRoomUsage,
    Room,
)
from accom_app.forms import OtherEstabForm
from accom_app.models import mies_table
from django.http import HttpResponse
from django.shortcuts import render, redirect
from accom_app.models import Other_Estab
from django.http import HttpResponse
import json
import csv
import re

from .forms import EstablishmentFormAdmin, TourismInformationForm
from .models import Region, Country, Entry, HotelConfirmation, TourismInformation
from accom_app.models import Summary
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST, require_http_methods
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.conf import settings
from django.core.mail import send_mail
from django.core import signing
from functools import wraps
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.db import transaction
from django.db.models import Q, Count, Avg, F, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
from urllib.parse import quote
import requests
import datetime as dt
import sys
from admin_app.mainpage_media import (
    upload_logo,
    upload_hero,
    set_active_logo,
    set_active_hero,
    delete_logo,
    delete_hero,
    get_admin_context as get_mainpage_media_admin_context,
)
from tour_app.models import Tour_Add, Tour_Schedule, Tour_Event
from guest_app.models import Guest, Pending, AccommodationBooking, AccommodationReview
from guest_app.booking_integrity import sync_room_current_availability
from .models import TourAssignment
from ai_chatbot.models import UsabilitySurveyResponse
from admin_app.notification_service import (
    create_notification,
    notify_accommodation_owner,
    notify_admins,
    notify_assigned_employees_for_schedule,
    serialize_notification_rows,
)


PASSWORD_RESET_SALT = "admin_app.password_reset"


def _clear_admin_panel_session_state(request):
    """
    Clear admin/accommodation panel session markers without flushing the whole
    session. This preserves Django messages across redirects from failed logins.
    """
    for key in (
        "user_type",
        "employee_id",
        "user_id",
        "is_admin",
        "accom_id",
        "company_name",
        "company_type",
    ):
        request.session.pop(key, None)


def _verify_recaptcha_response(request):
    """
    Verify Google reCAPTCHA token if RECAPTCHA_SECRET_KEY is configured.
    Returns (is_valid, error_message).
    """
    recaptcha_secret = str(getattr(settings, "RECAPTCHA_SECRET_KEY", "") or "").strip()
    if getattr(settings, "TESTING", False) or "test" in sys.argv:
        return True, ""
    if not recaptcha_secret:
        # Safe fallback for local/dev when secret is not configured.
        return True, ""

    recaptcha_response = (
        request.POST.get("g-recaptcha-response")
        or request.POST.get("g_recaptcha_response")
        or ""
    ).strip()
    if not recaptcha_response:
        return False, "Please complete the reCAPTCHA verification."

    try:
        recaptcha_result = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": recaptcha_secret, "response": recaptcha_response},
            timeout=15,
        ).json()
    except Exception:
        return False, "reCAPTCHA verification is temporarily unavailable. Please try again."

    if not recaptcha_result.get("success", False):
        return False, "reCAPTCHA verification failed. Please try again."
    return True, ""


def _build_password_reset_token(account_type, account_id, email):
    payload = {
        "account_type": account_type,
        "account_id": str(account_id),
        "email": str(email).strip().lower(),
    }
    return signing.dumps(payload, salt=PASSWORD_RESET_SALT)


def _load_password_reset_token(token, max_age_seconds=3600):
    try:
        return signing.loads(token, salt=PASSWORD_RESET_SALT, max_age=max_age_seconds), None
    except signing.SignatureExpired:
        return None, "This reset link has expired. Please request a new one."
    except signing.BadSignature:
        return None, "Invalid reset link."


def _find_reset_account_by_email(email):
    normalized = (email or "").strip().lower()
    if not normalized:
        return None

    employee = Employee.objects.filter(email__iexact=normalized).first()
    if employee:
        return {"type": "employee", "obj": employee, "email": employee.email}

    owner_guest = Guest.objects.filter(email__iexact=normalized).first()
    if owner_guest:
        role_value = str(getattr(owner_guest, "role", "") or "").strip().lower()
        owner_group_names = {
            "accommodation_owner",
            "accommodation_owner_pending",
            "accommodation_owner_declined",
        }
        in_owner_group = owner_guest.groups.filter(name__in=owner_group_names).exists()
        has_owner_role = role_value in {"accommodation_owner", "accommodation owner", "owner"}
        has_owned_accommodation = Accomodation.objects.filter(owner=owner_guest).exists()
        if in_owner_group or has_owner_role or has_owned_accommodation:
            return {"type": "owner_guest", "obj": owner_guest, "email": owner_guest.email}

    guest_user = Guest.objects.filter(email__iexact=normalized).first()
    if guest_user:
        return {"type": "guest", "obj": guest_user, "email": guest_user.email}

    accom = Accomodation.objects.filter(email_address__iexact=normalized).first()
    if accom:
        return {"type": "accommodation", "obj": accom, "email": accom.email_address}

    return None


def _find_owner_login_candidate(username_or_email):
    """
    Identify a potential accommodation owner account from either username or email.
    This is intentionally broader than `is_accommodation_owner` so pending/declined
    owners receive the correct approval-status message instead of generic invalid login.
    """
    identifier = str(username_or_email or "").strip()
    if not identifier:
        return None

    owner_candidate = Guest.objects.filter(
        Q(email__iexact=identifier) | Q(username__iexact=identifier)
    ).first()
    if not owner_candidate:
        return None

    role_value = str(getattr(owner_candidate, "role", "") or "").strip().lower()
    owner_group_names = {
        "accommodation_owner",
        "accommodation_owner_pending",
        "accommodation_owner_declined",
    }
    in_owner_group = owner_candidate.groups.filter(name__in=owner_group_names).exists()
    has_owner_role = role_value in {"accommodation_owner", "accommodation owner", "owner"}
    has_owned_accommodation = Accomodation.objects.filter(owner=owner_candidate).exists()

    return owner_candidate if (in_owner_group or has_owner_role or has_owned_accommodation) else None


def forgot_password(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip()

        account = _find_reset_account_by_email(email)
        # Avoid account enumeration: show the same message regardless.
        if account:
            token = _build_password_reset_token(
                account_type=account["type"],
                account_id=getattr(account["obj"], "pk"),
                email=account["email"],
            )
            reset_link = request.build_absolute_uri(
                reverse("admin_app:reset_password") + f"?token={token}"
            )
            subject = "Ibayaw Tour Password Reset"
            message = (
                "We received a request to reset your password.\n\n"
                f"Use this link to reset your password (valid for 1 hour):\n{reset_link}\n\n"
                "If you did not request this, you can ignore this email."
            )
            try:
                send_mail(
                    subject,
                    message,
                    getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    [account["email"]],
                    fail_silently=False,
                )
            except Exception:
                messages.error(request, "Unable to send reset email right now. Please try again later.")
                return render(request, "forgot_password.html")

        messages.success(
            request,
            "If the email exists in our system, a password reset link has been sent.",
        )
        return redirect("admin_app:forgot_password")

    return render(request, "forgot_password.html")


def reset_password(request):
    token = (request.GET.get("token") or request.POST.get("token") or "").strip()
    payload, token_error = _load_password_reset_token(token) if token else (None, "Missing reset token.")

    if token_error:
        messages.error(request, token_error)
        return render(request, "reset_password.html", {"token": token, "token_valid": False})

    account_type = payload.get("account_type")
    account_id = payload.get("account_id")
    email = (payload.get("email") or "").strip().lower()

    account_obj = None
    if account_type == "employee":
        account_obj = Employee.objects.filter(pk=account_id, email__iexact=email).first()
    elif account_type == "owner_guest":
        account_obj = Guest.objects.filter(pk=account_id, email__iexact=email).first()
    elif account_type == "guest":
        account_obj = Guest.objects.filter(pk=account_id, email__iexact=email).first()
    elif account_type == "accommodation":
        account_obj = Accomodation.objects.filter(pk=account_id, email_address__iexact=email).first()

    if not account_obj:
        messages.error(request, "The account for this reset link was not found.")
        return render(request, "reset_password.html", {"token": token, "token_valid": False})

    if request.method == "POST":
        password = request.POST.get("password") or ""
        confirm_password = request.POST.get("confirm_password") or ""

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters long.")
            return render(request, "reset_password.html", {"token": token, "token_valid": True})

        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, "reset_password.html", {"token": token, "token_valid": True})

        if account_type in {"employee", "owner_guest", "guest"}:
            account_obj.set_password(password)
            account_obj.save()
        else:
            account_obj.password = make_password(password)
            account_obj.save(update_fields=["password"])

        messages.success(request, "Password reset successful. You can now log in.")
        return redirect("admin_app:login")

    return render(
        request,
        "reset_password.html",
        {"token": token, "token_valid": True, "email": email},
    )

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
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return redirect('admin_app:login')
        if not is_accommodation_owner(user):
            return redirect('admin_app:login')
        owned_accommodation = (
            Accomodation.objects.filter(owner=user, approval_status="accepted")
            .order_by("accom_id")
            .first()
        )
        if owned_accommodation is None:
            messages.info(request, "No approved accommodation is linked yet. Please submit an accommodation first.")
            return redirect("admin_app:owner_hub")
        request.current_accommodation = owned_accommodation
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


def _resolve_notification_recipient(request):
    if request.session.get("user_type") == "employee" and request.session.get("employee_id"):
        employee = Employee.objects.filter(emp_id=request.session.get("employee_id")).first()
        if employee:
            return {"employee": employee, "guest": None}
    if request.user.is_authenticated:
        return {"employee": None, "guest": request.user}
    return {"employee": None, "guest": None}


@require_http_methods(["GET", "POST"])
def notifications_feed(request):
    recipient = _resolve_notification_recipient(request)
    employee = recipient["employee"]
    guest_user = recipient["guest"]
    if not employee and not guest_user:
        return JsonResponse({"success": False, "message": "Unauthorized."}, status=401)

    base_qs = InAppNotification.objects.all()
    if employee:
        base_qs = base_qs.filter(recipient_employee=employee)
    else:
        base_qs = base_qs.filter(recipient_guest=guest_user)

    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        action = str(payload.get("action") or "").strip().lower()
        notif_id = str(payload.get("notification_id") or "").strip()
        notif_ids = payload.get("notification_ids") if isinstance(payload.get("notification_ids"), list) else []

        if action == "mark_all_read":
            scoped = base_qs.filter(is_read=False)
            if notif_ids:
                normalized_ids = []
                for value in notif_ids:
                    try:
                        normalized_ids.append(int(str(value).strip()))
                    except Exception:
                        continue
                if normalized_ids:
                    scoped = scoped.filter(id__in=normalized_ids)
            scoped.update(is_read=True)
            unread_count = base_qs.filter(is_read=False).count()
            return JsonResponse({"success": True, "unread_count": unread_count})

        if notif_id:
            try:
                target_id = int(notif_id)
            except Exception:
                return JsonResponse({"success": False, "message": "Invalid notification id."}, status=400)
            updated = base_qs.filter(id=target_id).update(is_read=True)
            if not updated:
                return JsonResponse({"success": False, "message": "Notification not found."}, status=404)
            unread_count = base_qs.filter(is_read=False).count()
            return JsonResponse({"success": True, "unread_count": unread_count})
        return JsonResponse({"success": False, "message": "No notification action applied."}, status=400)

    rows = list(base_qs.order_by("-created_at")[:20])
    unread_count = base_qs.filter(is_read=False).count()
    return JsonResponse(
        {
            "success": True,
            "notifications": serialize_notification_rows(rows),
            "unread_count": unread_count,
        }
    )


@require_http_methods(["GET"])
def notification_open(request, notification_id):
    recipient = _resolve_notification_recipient(request)
    employee = recipient["employee"]
    guest_user = recipient["guest"]
    if not employee and not guest_user:
        return redirect("admin_app:login")
    scoped = InAppNotification.objects.filter(id=notification_id)
    if employee:
        scoped = scoped.filter(recipient_employee=employee)
    else:
        scoped = scoped.filter(recipient_guest=guest_user)
    row = scoped.first()
    if row is None:
        return redirect(request.GET.get("next") or request.META.get("HTTP_REFERER") or "/")
    if not row.is_read:
        row.is_read = True
        row.save(update_fields=["is_read"])
    target = str(request.GET.get("next") or row.url or request.META.get("HTTP_REFERER") or "/").strip()
    return redirect(target)


@require_http_methods(["GET"])
def notifications_mark_all_read(request):
    recipient = _resolve_notification_recipient(request)
    employee = recipient["employee"]
    guest_user = recipient["guest"]
    if not employee and not guest_user:
        return redirect("admin_app:login")
    if employee:
        InAppNotification.objects.filter(recipient_employee=employee, is_read=False).update(is_read=True)
    elif guest_user:
        InAppNotification.objects.filter(recipient_guest=guest_user, is_read=False).update(is_read=True)
    target = str(request.GET.get("next") or request.META.get("HTTP_REFERER") or "/").strip()
    return redirect(target)


def _validate_accommodation_booking_confirmation(booking):
    """
    Validate whether an accommodation booking is eligible to move from pending
    to confirmed.
    Returns: (is_valid: bool, reason: str)
    """
    if booking.status != "pending":
        return False, "Only pending bookings can be confirmed."
    if booking.room_id is None:
        return False, "Booking has no room assigned."
    if booking.check_out <= booking.check_in:
        return False, "Invalid stay dates: check-out must be after check-in."

    accommodation = booking.accommodation
    if not accommodation:
        return False, "Booking has no accommodation record."
    if str(accommodation.approval_status or "").lower() != "accepted" or not accommodation.is_active:
        return False, "Accommodation is not active/accepted for booking approval."

    room = booking.room
    if not room:
        return False, "Room record was not found."
    if room.accommodation_id != booking.accommodation_id:
        return False, "Selected room does not belong to this accommodation."
    if str(room.status or "").upper() != "AVAILABLE":
        return False, "Room is not currently marked as available."

    try:
        person_limit = int(room.person_limit or 0)
    except Exception:
        person_limit = 0
    if person_limit < 1 or int(booking.num_guests or 0) > person_limit:
        return False, "Room capacity is insufficient for this booking."

    overlapping_confirmed = AccommodationBooking.objects.filter(
        room=room,
        status="confirmed",
        check_in__lt=booking.check_out,
        check_out__gt=booking.check_in,
    ).exclude(booking_id=booking.booking_id).exists()
    if overlapping_confirmed:
        return False, "Room has an overlapping confirmed booking for the same date range."

    return True, ""


def is_accommodation_owner(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False

    role_value = str(getattr(user, "role", "") or "").strip().lower()
    if role_value in {"accommodation_owner", "accommodation owner"}:
        return True

    return user.groups.filter(name__iexact="accommodation_owner").exists()


@login_required
def owner_dashboard_entry(request):
    """
    Safe dashboard entry for accommodation owners.
    If an accepted accommodation is linked, open the accommodation dashboard.
    Otherwise keep owner in owner hub so they can register one.
    """
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if request.user.groups.filter(id=declined_group.id).exists():
        messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
        return redirect("admin_app:login")
    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:login")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Please sign up as an accommodation owner first from the admin login page.")
        return redirect("admin_app:login")

    owned_accom = (
        Accomodation.objects.filter(owner=request.user, approval_status="accepted")
        .order_by("accom_id")
        .first()
    )
    if owned_accom is None:
        messages.info(
            request,
            "No accepted accommodation is linked yet. Please submit an accommodation first.",
        )
        return redirect("admin_app:owner_hub")

    request.session["accom_id"] = owned_accom.accom_id
    request.session["company_name"] = owned_accom.company_name
    request.session["company_type"] = owned_accom.company_type
    request.session["user_type"] = "accomodation"
    return redirect("admin_app:accommodation_dashboard")

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
    
    hidden_places = getattr(
        settings,
        "TOURISM_MAP_GUEST_HIDDEN_PLACES",
        ["Bayawan City Hall"],
    )
    return render(request, 'map.html', {
        'map_mode': 'admin',
        'can_edit_bookmarks': True,
        'guest_hidden_place_names': hidden_places if isinstance(hidden_places, (list, tuple)) else [],
    })


def employee_map_view(request):
    """Map view for employee accounts."""
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')

    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')

    log_activity(request, employee, 'view_page', description='Viewed employee map page')
    hidden_places = getattr(
        settings,
        "TOURISM_MAP_GUEST_HIDDEN_PLACES",
        ["Bayawan City Hall"],
    )
    return render(request, 'map.html', {
        'map_mode': 'employee',
        'can_edit_bookmarks': True,
        'guest_hidden_place_names': hidden_places if isinstance(hidden_places, (list, tuple)) else [],
    })

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
    # Clear only panel-related session markers.
    _clear_admin_panel_session_state(request)

    if request.method == 'POST':
        recaptcha_ok, recaptcha_error = _verify_recaptcha_response(request)
        if not recaptcha_ok:
            messages.error(request, recaptcha_error)
            return redirect('admin_app:login')

        username_or_email = request.POST.get('username')  # could be an email
        password = request.POST.get('password')

        # Try authenticating as an Employee first.
        employee = Employee.objects.filter(
            Q(email__iexact=username_or_email) | Q(username__iexact=username_or_email)
        ).first()
        if employee:
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
        else:
            # Not found in Employee table; try Accommodation Owner account (Guest model).
            owner_candidate = _find_owner_login_candidate(username_or_email)
            if owner_candidate:
                auth_user = authenticate(
                    request,
                    username=owner_candidate.username,
                    password=password,
                )
                if auth_user is None:
                    messages.error(request, "Invalid username or password")
                    return redirect('admin_app:login')

                pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
                approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
                declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

                if auth_user.groups.filter(id=declined_group.id).exists():
                    messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
                    return redirect('admin_app:login')
                if auth_user.groups.filter(id=pending_group.id).exists() and not auth_user.groups.filter(id=approved_group.id).exists():
                    messages.error(request, "Your accommodation owner account is pending admin approval.")
                    return redirect('admin_app:login')
                if not auth_user.groups.filter(id=approved_group.id).exists():
                    messages.error(request, "Accommodation owner access is not approved yet.")
                    return redirect('admin_app:login')

                auth_login(request, auth_user)
                request.session['user_type'] = 'accomodation'
                request.session['company_name'] = (
                    f"{auth_user.first_name} {auth_user.last_name}".strip() or auth_user.username
                )

                owned_accom = (
                    Accomodation.objects.filter(owner=auth_user, approval_status="accepted")
                    .order_by("accom_id")
                    .first()
                )
                if owned_accom is not None:
                    request.session['accom_id'] = owned_accom.accom_id
                    request.session['company_name'] = owned_accom.company_name
                    request.session['company_type'] = owned_accom.company_type
                    return redirect('admin_app:accommodation_dashboard')

                messages.info(
                    request,
                    "Owner account login successful. Please register your accommodation to continue.",
                )
                return redirect("admin_app:owner_hub")

            # Fallback compatibility: legacy accommodation account credential login.
            try:
                accom = Accomodation.objects.get(email_address=username_or_email)
                if check_password(password, accom.password):
                    if (accom.approval_status or "").lower() != "accepted":
                        messages.error(request, "Your accommodation account is not approved yet.")
                        return redirect('admin_app:login')

                    owner_user = getattr(accom, "owner", None)
                    if owner_user is None:
                        messages.error(
                            request,
                            "This accommodation is not linked to an owner user account. "
                            "Please ask admin to link an owner first.",
                        )
                        return redirect('admin_app:login')
                    if not owner_user.groups.filter(name__iexact="accommodation_owner").exists():
                        messages.error(
                            request,
                            "Owner account is not approved for accommodation access yet.",
                        )
                        return redirect('admin_app:login')

                    auth_login(request, owner_user)

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

                        # Default owner landing is accommodation dashboard.
                        return redirect('admin_app:accommodation_dashboard')
                else:
                    messages.error(request, "Invalid username or password")
            except Accomodation.DoesNotExist:
                messages.error(request, "Invalid username or password")

    return render(
        request,
        'login.html',
        {
            'recaptcha_site_key': str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip(),
            'recaptcha_configured': bool(
                str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip()
                and str(getattr(settings, 'RECAPTCHA_SECRET_KEY', '') or '').strip()
            ),
        },
    )

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
    if not request.user.is_authenticated:
        messages.error(request, "Please log in first to register your accommodation.")
        login_url = f"{reverse('admin_app:login')}?next={quote(request.get_full_path(), safe='')}"
        return redirect(login_url)
    if not request.user.is_active:
        messages.error(request, "Your account is inactive. Please contact admin.")
        return redirect("admin_app:login")
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if request.user.groups.filter(id=declined_group.id).exists():
        messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
        return redirect("admin_app:login")
    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:login")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Please sign up as an accommodation owner first from the admin login page.")
        return redirect("admin_app:login")

    existing_pending_registration = Accomodation.objects.filter(
        owner=request.user,
        approval_status="pending",
    ).first()

    if request.method == 'POST':
        if existing_pending_registration:
            messages.error(
                request,
                "You already have a pending accommodation registration. Please wait for admin review.",
            )
            return redirect(request.path)

        form = AccommodationRegistrationForm(request.POST, request.FILES, owner=request.user)
        if form.is_valid():
            accommodation = form.save()
            notify_admins(
                title="New accommodation pending approval",
                message=f"{accommodation.company_name} was submitted and is awaiting review.",
                notification_type="approval",
                url=reverse("admin_app:pending_accommodation"),
                dedupe_key=f"accom-pending-{accommodation.accom_id}",
            )
            create_notification(
                recipient_guest=request.user,
                title="Accommodation submission received",
                message=f"{accommodation.company_name} is now pending admin approval.",
                notification_type="approval",
                url=reverse("admin_app:owner_hub"),
                dedupe_key=f"owner-submitted-accom-{accommodation.accom_id}",
                related_object_id=str(accommodation.accom_id),
            )
            messages.success(request, "Accommodation submitted successfully. Await admin approval.")
            return redirect("admin_app:accommodation_register")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AccommodationRegistrationForm(owner=request.user)

    # Add request object to context for template conditional rendering
    owner_accommodations = Accomodation.objects.filter(owner=request.user).order_by("-submitted_at")
    context = {
        'form': form,
        'request': request,
        'owner_accommodations': owner_accommodations,
    }
    return render(request, 'accommodation.html', context)


@login_required
def owner_hub(request):
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if request.user.groups.filter(id=declined_group.id).exists():
        messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
        return redirect("admin_app:login")
    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:login")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Please sign up as an accommodation owner first from the admin login page.")
        return redirect("admin_app:login")

    owner_accommodations = list(
        Accomodation.objects.filter(owner=request.user).order_by("-submitted_at")
    )

    from admin_app.models import Room
    room_map = {}
    for room in Room.objects.filter(accommodation__owner=request.user).order_by("room_name"):
        meta = room_map.setdefault(
            room.accommodation_id,
            {
                "total_count": 0,
                "available_count": 0,
                "total_pax": 0,
                "available_pax": 0,
                "available_names": [],
            },
        )
        room_pax = int(getattr(room, "person_limit", 0) or 0)
        meta["total_count"] += 1
        meta["total_pax"] += room_pax
        if str(room.status or "").upper() == "AVAILABLE":
            meta["available_count"] += 1
            meta["available_pax"] += room_pax
            if room.room_name:
                meta["available_names"].append(room.room_name)

    owner_rows = []
    accepted_count = 0
    pending_count = 0
    declined_count = 0

    for accom in owner_accommodations:
        status = str(accom.approval_status or "").lower()
        if status == "accepted":
            accepted_count += 1
        elif status == "pending":
            pending_count += 1
        elif status == "declined":
            declined_count += 1

        meta = room_map.get(
            accom.accom_id,
            {
                "total_count": 0,
                "available_count": 0,
                "total_pax": 0,
                "available_pax": 0,
                "available_names": [],
            },
        )
        owner_rows.append(
            {
                "accommodation": accom,
                "total_count": meta["total_count"],
                "available_count": meta["available_count"],
                "total_pax": meta["total_pax"],
                "available_pax": meta["available_pax"],
                "available_names": meta["available_names"],
            }
        )

    context = {
        "owner_rows": owner_rows,
        "accepted_count": accepted_count,
        "pending_count": pending_count,
        "declined_count": declined_count,
    }
    return render(request, "owner_hub.html", context)


@login_required
def owner_edit_accommodation(request, accom_id):
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if request.user.groups.filter(id=declined_group.id).exists():
        messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
        return redirect("admin_app:login")
    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:login")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Please sign up as an accommodation owner first from the admin login page.")
        return redirect("admin_app:login")

    accommodation = get_object_or_404(
        Accomodation,
        accom_id=accom_id,
        owner=request.user,
    )

    if request.method == "POST":
        form = OwnerAccommodationEditForm(
            request.POST,
            request.FILES,
            instance=accommodation,
        )
        if form.is_valid():
            updated = form.save()
            request.session["company_name"] = updated.company_name
            request.session["company_type"] = updated.company_type
            messages.success(request, "Accommodation profile updated successfully.")
            return redirect("admin_app:owner_hub")
        messages.error(request, "Please correct the highlighted fields.")
    else:
        form = OwnerAccommodationEditForm(instance=accommodation)

    return render(
        request,
        "owner_accommodation_edit.html",
        {
            "form": form,
            "accommodation": accommodation,
        },
    )


@login_required
def owner_accommodation_bookings(request):
    messages.info(
        request,
        "Accommodation booking operations are no longer handled in-system. "
        "Please use the Tourism Office reporting module.",
    )
    return redirect("admin_app:owner_report_submit")


@login_required
@require_POST
def owner_accommodation_booking_update(request, booking_id):
    messages.info(
        request,
        "Accommodation booking operations are disabled. "
        "Use the Tourism Office reporting module instead.",
    )
    return redirect("admin_app:owner_report_submit")


@login_required
def owner_report_submit(request):
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if request.user.groups.filter(id=declined_group.id).exists():
        messages.error(request, "Your accommodation owner account was declined. Please contact admin.")
        return redirect("admin_app:login")
    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:login")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Please sign up as an accommodation owner first from the admin login page.")
        return redirect("admin_app:login")

    def _parse_room_identifier_tokens(raw_text):
        text = str(raw_text or "").strip()
        if not text:
            return []
        chunks = re.split(r"[,;\n]+", text)
        tokens = []
        seen = set()
        for raw in chunks:
            token = str(raw or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token[:80])
        return tokens[:120]

    def _build_room_usage_statistics_text(*, guests_in, guests_out, rooms_used_value, rows):
        check_ins = max(int(guests_in or 0), 0)
        check_outs = max(int(guests_out or 0), 0)
        rooms_used_safe = max(int(rooms_used_value or 0), 0)
        peak_room = ""
        peak_score = -1
        touched_rows = 0
        room_totals = {}
        for row in rows or []:
            room_name = str(row.get("room_name_snapshot") or "").strip() or "Room"
            row_in = max(int(row.get("check_ins") or 0), 0)
            row_out = max(int(row.get("check_outs") or 0), 0)
            score = row_in + row_out
            if score > 0:
                touched_rows += 1
            room_totals[room_name] = int(room_totals.get(room_name, 0) or 0) + score
        for room_name, score in room_totals.items():
            if score > peak_score:
                peak_score = score
                peak_room = room_name

        movement = check_ins + check_outs
        balance_gap = abs(check_ins - check_outs)
        if movement <= 0:
            trend_text = "No room movement was reported for this period."
        elif balance_gap <= max(2, int(movement * 0.1)):
            trend_text = "Guest movement was balanced between check-ins and check-outs."
        elif check_ins > check_outs:
            trend_text = "Check-ins were higher than check-outs, indicating increasing occupancy."
        else:
            trend_text = "Check-outs were higher than check-ins, indicating turnover or reduced occupancy."

        lines = [
            f"Total guest movement: {movement} ({check_ins} check-ins, {check_outs} check-outs).",
            f"Rooms used/occupied: {rooms_used_safe}.",
            trend_text,
        ]
        if peak_room and peak_score > 0:
            lines.append(f"Most active room type: {peak_room} ({peak_score} total movements).")
        if touched_rows > 0:
            lines.append(f"Room-level rows with activity: {touched_rows}.")
        return " ".join(lines).strip()

    def _build_nationality_breakdown_text(*, guests_in, guests_out):
        check_ins = max(int(guests_in or 0), 0)
        check_outs = max(int(guests_out or 0), 0)
        if check_ins <= 0 and check_outs <= 0:
            return (
                "No guest movement was reported for this period. "
                "If there were guest stays, please encode the verified nationality totals."
            )
        return (
            f"Auto draft based on current totals: {check_ins} check-ins, {check_outs} check-outs. "
            f"Please confirm and update with verified counts: Filipino {check_ins}, Foreign 0 "
            "(e.g., JP 0, KR 0, US 0)."
        )

    owner_accommodations_qs = Accomodation.objects.filter(
        owner=request.user,
        approval_status="accepted",
        is_active=True,
    ).order_by("company_name").prefetch_related("rooms")
    owner_accommodations = list(owner_accommodations_qs)
    owner_accommodation_room_rows = []
    for accom in owner_accommodations:
        room_rows = []
        for room in accom.rooms.all().order_by("room_name", "room_id"):
            room_rows.append(
                {
                    "room_id": int(room.room_id),
                    "room_name": str(room.room_name or "").strip() or f"Room {room.room_id}",
                    "room_identifiers": str(getattr(room, "room_identifiers", "") or "").strip(),
                    "total_rooms": int(getattr(room, "total_rooms", 1) or 1),
                    "known_identifiers": _parse_room_identifier_tokens(getattr(room, "room_identifiers", "")),
                }
            )
        owner_accommodation_room_rows.append(
            {
                "accom_id": accom.accom_id,
                "rooms": room_rows,
            }
        )

    if request.method == "POST":
        accom_id = str(request.POST.get("accommodation_id") or "").strip()
        period_raw = str(request.POST.get("reporting_period") or "").strip()
        status_raw = str(request.POST.get("status") or "submitted").strip().lower()

        try:
            accommodation = owner_accommodations_qs.get(accom_id=accom_id)
        except Accomodation.DoesNotExist:
            messages.error(request, "Please select a valid accommodation listing.")
            return redirect("admin_app:owner_report_submit")

        try:
            period_value = dt.date.fromisoformat(f"{period_raw}-01")
        except Exception:
            messages.error(request, "Reporting period must use YYYY-MM format.")
            return redirect("admin_app:owner_report_submit")

        def _to_non_negative_int(raw_value):
            try:
                value = int(str(raw_value or "0").strip())
            except Exception:
                return 0
            return max(value, 0)

        room_rows = list(Room.objects.filter(accommodation=accommodation).order_by("room_name", "room_id"))
        room_map = {int(room.room_id): room for room in room_rows}

        posted_room_ids = request.POST.getlist("room_usage_room_id")
        posted_room_identifiers = request.POST.getlist("room_usage_identifier")
        posted_checkins = request.POST.getlist("room_usage_checkins")
        posted_checkouts = request.POST.getlist("room_usage_checkouts")
        posted_manual_identifiers = request.POST.getlist("room_usage_identifier_manual")

        row_count = max(
            len(posted_room_ids),
            len(posted_room_identifiers),
            len(posted_checkins),
            len(posted_checkouts),
            len(posted_manual_identifiers),
        )
        has_room_inputs = row_count > 0
        room_usage_payload = []
        for idx in range(row_count):
            room_id_raw = posted_room_ids[idx] if idx < len(posted_room_ids) else ""
            try:
                room_id = int(str(room_id_raw or "").strip())
            except Exception:
                continue
            room = room_map.get(room_id)
            if room is None:
                continue
            identifier_raw = posted_room_identifiers[idx] if idx < len(posted_room_identifiers) else ""
            manual_identifier_raw = posted_manual_identifiers[idx] if idx < len(posted_manual_identifiers) else ""
            selected_identifier = str(identifier_raw or "").strip()
            if selected_identifier == "__manual__":
                selected_identifier = str(manual_identifier_raw or "").strip()
            if not selected_identifier:
                selected_identifier = "Not specified"
            check_ins = _to_non_negative_int(posted_checkins[idx] if idx < len(posted_checkins) else 0)
            check_outs = _to_non_negative_int(posted_checkouts[idx] if idx < len(posted_checkouts) else 0)
            room_usage_payload.append(
                {
                    "room": room,
                    "room_name_snapshot": str(room.room_name or "").strip(),
                    "selected_room_identifier": selected_identifier[:120],
                    "room_identifiers_snapshot": str(getattr(room, "room_identifiers", "") or "").strip(),
                    "total_rooms_snapshot": max(1, int(getattr(room, "total_rooms", 1) or 1)),
                    "check_ins": check_ins,
                    "check_outs": check_outs,
                    "guests_count": 1 if (check_ins > 0 or check_outs > 0) else 0,
                }
            )

        if has_room_inputs and room_usage_payload:
            guests_checked_in = sum(int(row.get("check_ins") or 0) for row in room_usage_payload)
            guests_checked_out = sum(int(row.get("check_outs") or 0) for row in room_usage_payload)
            rooms_used = len(
                {
                    (int(row.get("room").room_id), str(row.get("selected_room_identifier") or "").strip().lower())
                    for row in room_usage_payload
                    if int(row.get("check_ins") or 0) > 0 or int(row.get("check_outs") or 0) > 0
                }
            )
        else:
            guests_checked_in = _to_non_negative_int(request.POST.get("guests_checked_in"))
            guests_checked_out = _to_non_negative_int(request.POST.get("guests_checked_out"))
            rooms_used = _to_non_negative_int(request.POST.get("rooms_used"))

        posted_room_usage_notes = str(request.POST.get("room_usage_notes") or "").strip()
        auto_room_usage_notes = _build_room_usage_statistics_text(
            guests_in=guests_checked_in,
            guests_out=guests_checked_out,
            rooms_used_value=rooms_used,
            rows=room_usage_payload,
        )
        use_manual_notes = bool(posted_room_usage_notes) and str(
            request.POST.get("room_usage_notes_manual") or ""
        ).strip() == "1"

        defaults = {
            "owner": request.user,
            "guests_checked_in": guests_checked_in,
            "guests_checked_out": guests_checked_out,
            "rooms_used": rooms_used,
            "room_usage_notes": posted_room_usage_notes if use_manual_notes else auto_room_usage_notes,
            "nationality_breakdown": (
                str(request.POST.get("nationality_breakdown") or "").strip()
                if (
                    str(request.POST.get("nationality_breakdown_manual") or "").strip() == "1"
                    and str(request.POST.get("nationality_breakdown") or "").strip()
                )
                else _build_nationality_breakdown_text(
                    guests_in=guests_checked_in,
                    guests_out=guests_checked_out,
                )
            ),
            "additional_remarks": str(request.POST.get("additional_remarks") or "").strip(),
            "status": status_raw if status_raw in {"draft", "submitted"} else "submitted",
            "review_notes": "",
            "reviewed_by": None,
        }
        with transaction.atomic():
            report, created = OwnerMonthlyReport.objects.update_or_create(
                accommodation=accommodation,
                reporting_period=period_value,
                defaults=defaults,
            )
            if has_room_inputs:
                MonthlyReportRoomUsage.objects.filter(monthly_report=report).delete()
                rows_to_create = []
                for row in room_usage_payload:
                    if (
                        int(row.get("check_ins") or 0) <= 0
                        and int(row.get("check_outs") or 0) <= 0
                        and int(row.get("guests_count") or 0) <= 0
                    ):
                        continue
                    rows_to_create.append(
                        MonthlyReportRoomUsage(
                            monthly_report=report,
                            room=row.get("room"),
                            room_name_snapshot=str(row.get("room_name_snapshot") or "").strip(),
                            selected_room_identifier=str(row.get("selected_room_identifier") or "").strip(),
                            room_identifiers_snapshot=str(row.get("room_identifiers_snapshot") or "").strip(),
                            total_rooms_snapshot=max(1, int(row.get("total_rooms_snapshot") or 1)),
                            check_ins=int(row.get("check_ins") or 0),
                            check_outs=int(row.get("check_outs") or 0),
                            guests_count=int(row.get("guests_count") or 0),
                        )
                    )
                if rows_to_create:
                    MonthlyReportRoomUsage.objects.bulk_create(rows_to_create)
        if created:
            messages.success(request, "Monthly report submitted successfully.")
        else:
            messages.success(request, "Monthly report updated successfully.")
        return redirect("admin_app:owner_report_submit")

    selected_report_status = str(request.GET.get("report_status") or "").strip().lower()
    selected_period = str(request.GET.get("report_period") or "").strip()
    selected_room_level = str(request.GET.get("room_level") or "").strip().lower()

    report_rows_qs = OwnerMonthlyReport.objects.select_related("accommodation").prefetch_related(
        "room_usage_rows",
        "room_usage_rows__room",
    ).filter(
        owner=request.user
    ).order_by("-reporting_period", "-submitted_at")

    period_choices_raw = (
        OwnerMonthlyReport.objects.filter(owner=request.user)
        .order_by("-reporting_period")
        .values_list("reporting_period", flat=True)
        .distinct()
    )
    period_choices = [value for value in period_choices_raw if value is not None]

    if selected_report_status in {"submitted", "reviewed", "returned", "draft"}:
        report_rows_qs = report_rows_qs.filter(status=selected_report_status)
    else:
        selected_report_status = ""

    if selected_period:
        try:
            selected_period_date = dt.date.fromisoformat(f"{selected_period}-01")
            report_rows_qs = report_rows_qs.filter(
                reporting_period__year=selected_period_date.year,
                reporting_period__month=selected_period_date.month,
            )
        except Exception:
            selected_period = ""

    if selected_room_level in {"detailed", "legacy"}:
        if selected_room_level == "detailed":
            report_rows_qs = report_rows_qs.filter(room_usage_rows__isnull=False).distinct()
        else:
            report_rows_qs = report_rows_qs.filter(room_usage_rows__isnull=True)
    else:
        selected_room_level = ""

    owner_summary = report_rows_qs.aggregate(
        total_reports=Count("report_id"),
        total_checkins=Sum("guests_checked_in"),
        total_checkouts=Sum("guests_checked_out"),
        total_rooms_used=Sum("rooms_used"),
    )

    report_paginator = Paginator(report_rows_qs, 12)
    report_page = request.GET.get("page")
    report_rows = report_paginator.get_page(report_page)
    return render(
        request,
        "owner_report_submit.html",
        {
            "owner_accommodations": owner_accommodations,
            "owner_accommodation_room_rows": owner_accommodation_room_rows,
            "report_rows": report_rows,
            "report_paginator": report_paginator,
            "selected_report_status": selected_report_status,
            "selected_period": selected_period,
            "selected_room_level": selected_room_level,
            "period_choices": period_choices,
            "owner_summary": owner_summary,
        },
    )


@login_required
def owner_manage_rooms(request, accom_id):
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")

    if request.user.groups.filter(id=pending_group.id).exists() and not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Your accommodation owner account is pending admin approval.")
        return redirect("admin_app:accommodation_register")
    if not request.user.groups.filter(id=approved_group.id).exists():
        messages.error(request, "Only approved accommodation owners can manage rooms.")
        return redirect("admin_app:accommodation_register")

    accommodation = get_object_or_404(
        Accomodation,
        accom_id=accom_id,
        owner=request.user,
        approval_status="accepted",
    )
    request.session["accom_id"] = accommodation.accom_id
    return redirect("accom_app:register_room")

@admin_required
def create_accommodation(request):
    reviewer = None
    employee_id = request.session.get("employee_id")
    if employee_id:
        reviewer = Employee.objects.filter(emp_id=employee_id).first()

    if request.method == "POST":
        form = AdminAccommodationEncodeForm(
            request.POST,
            request.FILES,
            reviewer=reviewer,
        )
        if form.is_valid():
            accommodation = form.save()
            messages.success(
                request,
                f'Accommodation "{accommodation.company_name}" was encoded successfully.',
            )
            return redirect("admin_app:create_accommodation")
        messages.error(request, "Please correct the highlighted fields.")
    else:
        form = AdminAccommodationEncodeForm(reviewer=reviewer)

    recent_accommodations = (
        Accomodation.objects.select_related("owner", "reviewed_by")
        .all()
        .order_by("-submitted_at")[:15]
    )
    return render(
        request,
        "accommodation_admin_create.html",
        {
            "form": form,
            "recent_accommodations": recent_accommodations,
        },
    )

@admin_required
def pending_accommodation(request):
    pending_accommodations = (
        Accomodation.objects.select_related("owner", "reviewed_by")
        .filter(approval_status="pending")
        .order_by("-submitted_at")
    )
    accepted_accommodations = (
        Accomodation.objects.select_related("owner", "reviewed_by")
        .filter(approval_status="accepted")
        .order_by("-reviewed_at", "-submitted_at")
    )
    declined_accommodations = (
        Accomodation.objects.select_related("owner", "reviewed_by")
        .filter(approval_status="declined")
        .order_by("-reviewed_at", "-submitted_at")
    )
    context = {
        'pending_accommodations': pending_accommodations,
        'accepted_accommodations': accepted_accommodations,
        'declined_accommodations': declined_accommodations,
    }
    return render(request, 'pending_accommodation.html', context)


@admin_required
def accommodation_reviews_moderation(request):
    selected_status = str(request.GET.get("status") or "pending").strip().lower()
    if selected_status not in {"pending", "approved", "rejected", "all"}:
        selected_status = "pending"

    reviewer = None
    employee_id = request.session.get("employee_id")
    if employee_id:
        reviewer = Employee.objects.filter(emp_id=employee_id).first()

    if request.method == "POST":
        review = get_object_or_404(
            AccommodationReview.objects.select_related("accommodation", "guest"),
            review_id=request.POST.get("review_id"),
        )
        action = str(request.POST.get("action") or "").strip().lower()
        notes = str(request.POST.get("moderation_notes") or "").strip()
        if action in {"approved", "rejected"}:
            review.status = action
            review.moderation_notes = notes
            review.reviewed_at = timezone.now()
            review.reviewed_by = reviewer
            review.save(update_fields=["status", "moderation_notes", "reviewed_at", "reviewed_by", "updated_at"])
            messages.success(request, f"Review for {review.accommodation.company_name} was marked {action}.")
        else:
            messages.error(request, "Invalid review moderation action.")
        return redirect(f"{reverse('admin_app:accommodation_reviews_moderation')}?status={selected_status}")

    reviews = AccommodationReview.objects.select_related("accommodation", "guest", "reviewed_by").order_by("-created_at")
    if selected_status != "all":
        reviews = reviews.filter(status=selected_status)

    return render(
        request,
        "accommodation_reviews_moderation.html",
        {
            "reviews": reviews[:100],
            "selected_status": selected_status,
            "status_options": ["pending", "approved", "rejected", "all"],
        },
    )


@admin_required
def pending_accommodation_owners(request):
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    pending_owners = Guest.objects.filter(groups=pending_group).order_by("-date_joined")
    approved_owners = Guest.objects.filter(groups=approved_group).order_by("-date_joined")
    declined_owners = Guest.objects.filter(groups=declined_group).order_by("-date_joined")

    context = {
        "pending_owners": pending_owners,
        "approved_owners": approved_owners,
        "declined_owners": declined_owners,
    }
    return render(request, "pending_accommodation_owners.html", context)


@admin_required
@require_POST
def accommodation_owner_update(request, user_id):
    owner_user = get_object_or_404(Guest, pk=user_id)
    action = str(request.POST.get("action") or "").strip().lower()

    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

    if action == "accept":
        owner_user.groups.remove(pending_group, declined_group)
        owner_user.groups.add(approved_group)
        create_notification(
            recipient_guest=owner_user,
            title="Owner account approved",
            message="Your accommodation owner account was approved. You may now access the Owner Hub.",
            notification_type="approval",
            url=reverse("admin_app:owner_hub"),
            dedupe_key=f"owner-account-accepted-{owner_user.pk}",
            related_object_id=str(owner_user.pk),
        )
        messages.success(request, f"Approved accommodation owner account: {owner_user.email}")
    elif action == "decline":
        owner_user.groups.remove(pending_group, approved_group)
        owner_user.groups.add(declined_group)
        Accomodation.objects.filter(owner=owner_user, approval_status="pending").update(
            approval_status="declined",
            status="declined",
            rejection_reason="Owner account was declined by admin.",
            reviewed_at=timezone.now(),
        )
        create_notification(
            recipient_guest=owner_user,
            title="Owner account declined",
            message="Your accommodation owner account request was declined. Please contact tourism office staff.",
            notification_type="approval",
            url=reverse("admin_app:login"),
            dedupe_key=f"owner-account-declined-{owner_user.pk}",
            related_object_id=str(owner_user.pk),
        )
        messages.success(request, f"Declined accommodation owner account: {owner_user.email}")
    else:
        messages.error(request, "Invalid owner approval action.")

    return redirect("admin_app:pending_accommodation_owners")


@admin_required
def accommodation_bookings(request):
    messages.info(
        request,
        "Accommodation booking operations are no longer in-system. "
        "You are now viewing owner-submitted monthly tourism reports.",
    )
    return redirect("admin_app:owner_reports_review")


@admin_required
@require_POST
def accommodation_booking_update(request, booking_id):
    messages.info(
        request,
        "Accommodation booking operations are disabled in this release scope.",
    )
    return redirect("admin_app:owner_reports_review")


@admin_required
def owner_reports_review(request):
    selected_accommodation_id = str(request.GET.get("accommodation_id") or "").strip()
    selected_accommodation_int = int(selected_accommodation_id) if selected_accommodation_id.isdigit() else None
    selected_period = str(request.GET.get("report_period") or "").strip()

    base_qs = OwnerMonthlyReport.objects.select_related("owner", "accommodation", "reviewed_by").prefetch_related(
        "room_usage_rows",
        "room_usage_rows__room",
    ).order_by(
        "-reporting_period",
        "-submitted_at",
    )

    if request.method == "POST":
        report_id = request.POST.get("report_id")
        action = str(request.POST.get("action") or "").strip().lower()
        report = get_object_or_404(OwnerMonthlyReport, report_id=report_id)
        reviewer = None
        employee_id = request.session.get("employee_id")
        if employee_id:
            reviewer = Employee.objects.filter(emp_id=employee_id).first()
        note = str(request.POST.get("review_notes") or "").strip()
        return_filter = str(request.POST.get("selected_accommodation_id") or "").strip()
        redirect_url = reverse("admin_app:owner_reports_review")
        if return_filter.isdigit():
            redirect_url = f"{redirect_url}?accommodation_id={return_filter}"

        if action == "mark_reviewed":
            report.status = "reviewed"
            report.review_notes = note
            report.reviewed_by = reviewer
            report.save(update_fields=["status", "review_notes", "reviewed_by", "updated_at"])
            try:
                create_notification(
                    recipient_guest=report.owner,
                    title="Monthly report reviewed",
                    message=(
                        f"Your monthly report for {report.accommodation.company_name} "
                        f"({report.reporting_period.strftime('%b %Y')}) was marked as Reviewed."
                    ),
                    notification_type="approval",
                    url=reverse("admin_app:owner_report_submit"),
                    dedupe_key=f"owner-report-reviewed-{report.report_id}",
                    related_object_id=str(report.report_id),
                )
            except Exception:
                pass
            messages.success(request, "Report marked as reviewed.")
        elif action == "return_revision":
            if not note:
                messages.error(request, "Please provide a revision reason before returning this report.")
                return redirect(redirect_url)
            report.status = "returned"
            report.review_notes = note
            report.reviewed_by = reviewer
            report.save(update_fields=["status", "review_notes", "reviewed_by", "updated_at"])
            try:
                create_notification(
                    recipient_guest=report.owner,
                    title="Monthly report returned for revision",
                    message=(
                        f"Your report for {report.accommodation.company_name} "
                        f"({report.reporting_period.strftime('%b %Y')}) was returned for revision. "
                        f"Reason: {note}"
                    ),
                    notification_type="approval",
                    url=reverse("admin_app:owner_report_submit"),
                    dedupe_key=f"owner-report-returned-{report.report_id}",
                    related_object_id=str(report.report_id),
                )
            except Exception:
                pass
            messages.success(request, "Report returned for revision.")
        else:
            messages.error(request, "Invalid report review action.")
        return redirect(redirect_url)

    rows_qs = base_qs
    if selected_accommodation_int is not None:
        rows_qs = rows_qs.filter(accommodation_id=selected_accommodation_int)
    if selected_period:
        try:
            selected_period_date = dt.date.fromisoformat(f"{selected_period}-01")
            rows_qs = rows_qs.filter(
                reporting_period__year=selected_period_date.year,
                reporting_period__month=selected_period_date.month,
            )
        except Exception:
            selected_period = ""

    review_summary = rows_qs.aggregate(
        total_reports=Count("report_id"),
        total_checkins=Sum("guests_checked_in"),
        total_checkouts=Sum("guests_checked_out"),
        total_rooms_used=Sum("rooms_used"),
        reviewed_reports=Count("report_id", filter=Q(status="reviewed")),
        submitted_reports=Count("report_id", filter=Q(status="submitted")),
        returned_reports=Count("report_id", filter=Q(status="returned")),
    )

    period_choices_raw = (
        base_qs.order_by("-reporting_period").values_list("reporting_period", flat=True).distinct()
    )
    period_choices = [value for value in period_choices_raw if value is not None]

    if str(request.GET.get("export") or "").strip().lower() == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="owner_monthly_reports.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Owner",
            "Accommodation",
            "Reporting Period",
            "Status",
            "Guests Checked In",
            "Guests Checked Out",
            "Rooms Used",
            "Room Type",
            "Room ID / Number",
            "Known Room IDs / Numbers",
            "Total Rooms Under Type",
            "Room Check-ins",
            "Room Check-outs",
            "Review Notes",
            "Reviewed By",
            "Submitted At",
            "Updated At",
        ])
        for row in rows_qs:
            reviewed_by_name = ""
            if row.reviewed_by:
                reviewed_by_name = f"{row.reviewed_by.first_name} {row.reviewed_by.last_name}".strip()
            usage_rows = list(row.room_usage_rows.all())
            if usage_rows:
                for usage in usage_rows:
                    writer.writerow([
                        getattr(row.owner, "username", "") or getattr(row.owner, "email", ""),
                        row.accommodation.company_name,
                        row.reporting_period.strftime("%B %Y"),
                        row.get_status_display(),
                        row.guests_checked_in,
                        row.guests_checked_out,
                        row.rooms_used,
                        usage.room_name_snapshot,
                        str(getattr(usage, "selected_room_identifier", "") or "").strip() or "Not specified",
                        str(getattr(usage, "room_identifiers_snapshot", "") or "").strip() or "Not specified",
                        int(getattr(usage, "total_rooms_snapshot", 1) or 1),
                        usage.check_ins,
                        usage.check_outs,
                        row.review_notes,
                        reviewed_by_name,
                        row.submitted_at.strftime("%Y-%m-%d") if row.submitted_at else "",
                        row.updated_at.strftime("%Y-%m-%d") if row.updated_at else "",
                    ])
            else:
                writer.writerow([
                    getattr(row.owner, "username", "") or getattr(row.owner, "email", ""),
                    row.accommodation.company_name,
                    row.reporting_period.strftime("%B %Y"),
                    row.get_status_display(),
                    row.guests_checked_in,
                    row.guests_checked_out,
                    row.rooms_used,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    row.review_notes,
                    reviewed_by_name,
                    row.submitted_at.strftime("%Y-%m-%d") if row.submitted_at else "",
                    row.updated_at.strftime("%Y-%m-%d") if row.updated_at else "",
                ])
        return response

    accommodation_choices = list(
        base_qs.values("accommodation_id", "accommodation__company_name")
        .annotate(report_count=Count("report_id"))
        .order_by("accommodation__company_name")
    )

    review_paginator = Paginator(rows_qs, 20)
    review_page = request.GET.get("page")
    rows = review_paginator.get_page(review_page)
    return render(
        request,
        "owner_reports_review.html",
        {
            "report_rows": rows,
            "review_paginator": review_paginator,
            "accommodation_choices": accommodation_choices,
            "selected_accommodation_id": str(selected_accommodation_int or ""),
            "selected_period": selected_period,
            "period_choices": period_choices,
            "review_summary": review_summary,
        },
    )

@admin_required
def accommodation_update(request, pk):
    try:
        accom = Accomodation.objects.get(pk=pk)
    except Accomodation.DoesNotExist:
        messages.error(request, "Accommodation not found.")
        return redirect('admin_app:pending_accommodation')

    if request.method == 'POST':
        new_status = str(request.POST.get('status') or "").strip().lower()
        rejection_reason = str(request.POST.get("rejection_reason") or "").strip()
        reviewer = None
        employee_id = request.session.get("employee_id")
        if employee_id:
            reviewer = Employee.objects.filter(emp_id=employee_id).first()
        # Directly store the lowercase status values.
        if new_status in ['accepted', 'declined']:
            if new_status == "declined" and not rejection_reason:
                rejection_reason = "Declined by admin."

            accom.mark_reviewed(
                status_value=new_status,
                reviewer=reviewer,
                rejection_reason=rejection_reason,
            )
            accom.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "reviewed_at",
                    "reviewed_by",
                    "rejection_reason",
                ]
            )
            if accom.owner_id:
                owner_title = "Accommodation listing approved" if new_status == "accepted" else "Accommodation listing declined"
                owner_message = (
                    f"{accom.company_name} has been approved and is now eligible for recommendations/bookings."
                    if new_status == "accepted"
                    else f"{accom.company_name} was declined. Review notes from admin for next steps."
                )
                create_notification(
                    recipient_guest=accom.owner,
                    title=owner_title,
                    message=owner_message,
                    notification_type="approval",
                    url=reverse("admin_app:owner_hub"),
                    dedupe_key=f"accom-review-{accom.accom_id}-{new_status}",
                    related_object_id=str(accom.accom_id),
                )
            messages.success(request, "Status updated successfully.")
        else:
            messages.error(request, "Invalid status selected.")
    return redirect('admin_app:pending_accommodation')




from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import Employee


@admin_required
def update_employees(request, emp_id):
    employee = get_object_or_404(Employee, emp_id=emp_id)
    if request.method == 'POST':
        # Update editable fields safely: do not overwrite required columns with NULL/blank.
        first_name = str(request.POST.get('first_name') or "").strip()
        last_name = str(request.POST.get('last_name') or "").strip()
        middle_name = str(request.POST.get('middle_name') or "").strip()
        phone_number = str(request.POST.get('phone_number') or "").strip()
        email = str(request.POST.get('email') or "").strip()
        age_raw = str(request.POST.get('age') or "").strip()
        sex_raw = str(request.POST.get('sex') or "").strip().upper()
        role_raw = str(request.POST.get('role') or "").strip()
        status_raw = str(request.POST.get('status') or "").strip().lower()

        if first_name:
            employee.first_name = first_name
        if last_name:
            employee.last_name = last_name
        # middle_name is optional; allow clearing to blank.
        employee.middle_name = middle_name or None
        if phone_number:
            employee.phone_number = phone_number
        if email:
            employee.email = email
        if age_raw:
            try:
                employee.age = int(age_raw)
            except (TypeError, ValueError):
                messages.error(request, "Invalid age value.")
                return redirect('admin_app:pending_employees')
        if sex_raw in {"M", "F"}:
            employee.sex = sex_raw
        if role_raw:
            employee.role = role_raw
        # status is required in DB; preserve existing status when missing/invalid.
        if status_raw in {"accepted", "declined", "pending"}:
            employee.status = status_raw
        else:
            employee.status = str(employee.status or "pending").strip().lower() or "pending"

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
    
    # Assigned tours (used for personal task context)
    assignments = TourAssignment.objects.filter(employee=employee).select_related('schedule', 'schedule__tour')

    # Dashboard metrics are scoped to the employee's assigned schedules.
    assigned_schedule_ids = list(assignments.values_list('schedule__sched_id', flat=True))
    schedules_qs = Tour_Schedule.objects.select_related('tour').filter(sched_id__in=assigned_schedule_ids)
    # Ensure stale schedule statuses are refreshed before counting.
    Tour_Schedule.get_tour_statistics()

    # Tour statistics (employee-assigned scope)
    completed_tours = schedules_qs.filter(status='completed').count()
    active_tours = schedules_qs.filter(status='active').count()
    cancelled_tours = schedules_qs.filter(status='cancelled').count()
    total_revenue = schedules_qs.filter(status__in=['active', 'completed']).aggregate(
        revenue=Sum(F('price') * F('slots_booked'))
    )['revenue'] or 0

    # Month-over-month growth percentages based on actual schedule data.
    now = timezone.now()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month_end = current_month_start - dt.timedelta(microseconds=1)
    previous_month_start = previous_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _range_stats(start, end):
        period_qs = schedules_qs.filter(end_time__gte=start, end_time__lte=end)
        period_revenue = period_qs.filter(status__in=['active', 'completed']).aggregate(
            revenue=Sum(F('price') * F('slots_booked'))
        )['revenue'] or 0
        return {
            'completed_tours': period_qs.filter(status='completed').count(),
            'active_tours': period_qs.filter(status='active').count(),
            'cancelled_tours': period_qs.filter(status='cancelled').count(),
            'total_revenue': float(period_revenue),
        }

    def _growth(current, previous):
        if previous == 0:
            return 100.0 if current > 0 else 0.0
        return ((current - previous) / previous) * 100.0

    def _growth_width(value):
        magnitude = abs(float(value))
        if magnitude == 0:
            return 10
        return max(12, min(100, int(round(magnitude))))

    current_month_stats = _range_stats(current_month_start, now)
    previous_month_stats = _range_stats(previous_month_start, previous_month_end)

    employee_growth = {
        'completed_tours': round(_growth(
            current_month_stats['completed_tours'],
            previous_month_stats['completed_tours'],
        ), 1),
        'active_tours': round(_growth(
            current_month_stats['active_tours'],
            previous_month_stats['active_tours'],
        ), 1),
        'cancelled_tours': round(_growth(
            current_month_stats['cancelled_tours'],
            previous_month_stats['cancelled_tours'],
        ), 1),
        'total_revenue': round(_growth(
            current_month_stats['total_revenue'],
            previous_month_stats['total_revenue'],
        ), 1),
    }
    employee_growth_width = {
        key: _growth_width(value) for key, value in employee_growth.items()
    }

    # Booking status summary
    booking_percentages = []
    fully_booked = almost_full = moderate_booking = low_booking = 0
    for sched in schedules_qs:
        total_slots = int((sched.slots_available or 0) + (sched.slots_booked or 0))
        pct = round((int(sched.slots_booked or 0) / total_slots) * 100) if total_slots > 0 else 0
        booking_percentages.append(pct)
        if pct >= 100:
            fully_booked += 1
        elif pct >= 75:
            almost_full += 1
        elif pct >= 40:
            moderate_booking += 1
        else:
            low_booking += 1
    average_percentage = round(sum(booking_percentages) / len(booking_percentages)) if booking_percentages else 0

    # Popular tour packs (by booked slots)
    popular_tours = list(
        schedules_qs.values('tour__tour_name')
        .annotate(total_booked=Sum('slots_booked'))
        .order_by('-total_booked')[:5]
    )
    total_booked_sum = sum(int(item.get("total_booked") or 0) for item in popular_tours)

    most_popular_tour = popular_tours[0] if popular_tours else None
    least_popular_tour = popular_tours[-1] if popular_tours else None
    fastest_growing_tour = popular_tours[0] if len(popular_tours) > 1 else None

    # Forecasts: data-based month-over-month trend per tour.
    booking_forecasts = []
    current_period_start = current_month_start
    previous_period_start = previous_month_start
    previous_period_end = previous_month_end
    for item in popular_tours[:5]:
        tour_name = item.get("tour__tour_name") or "Tour"
        tour_rows = schedules_qs.filter(tour__tour_name=tour_name)
        current = int(
            tour_rows.filter(end_time__gte=current_period_start, end_time__lte=now).aggregate(
                total=Sum('slots_booked')
            ).get('total') or 0
        )
        previous = int(
            tour_rows.filter(end_time__gte=previous_period_start, end_time__lte=previous_period_end).aggregate(
                total=Sum('slots_booked')
            ).get('total') or 0
        )
        if previous > 0:
            growth = round(((current - previous) / previous) * 100, 1)
            forecast = max(0, int(round(current * (1 + (growth / 100.0)))))
        else:
            growth = 100.0 if current > 0 else 0.0
            forecast = current
        booking_forecasts.append({
            "tour_name": tour_name,
            "current": current,
            "forecast": forecast,
            "growth": growth,
        })

    # Insights and recommendations
    booking_insights = []
    booking_recommendations = []
    if active_tours > 0:
        booking_insights.append({
            'type': 'up',
            'text': f"You currently handle {active_tours} active tour schedule(s).",
        })
    if cancelled_tours > 0:
        booking_insights.append({
            'type': 'down',
            'text': f"{cancelled_tours} assigned schedule(s) are cancelled and may need guest follow-ups.",
        })
    booking_insights.append({
        'type': 'forecast',
        'text': f"Expected booking load next period: {max(1, active_tours)} schedule focus area(s).",
    })

    if low_booking > 0:
        booking_recommendations.append("Consider promotion for low-booking tours to improve participation.")
    if almost_full + fully_booked > 0:
        booking_recommendations.append("Prepare staffing for high-demand tours and monitor available slots.")
    if not booking_recommendations:
        booking_recommendations.append("No recommendations available. Keep monitoring tour booking behavior.")

    # Mini calendar payload: only upcoming/active schedules.
    color_map = {
        "active": "#34a853",
        "completed": "#4285f4",
        "cancelled": "#ea4335",
    }
    actionable_schedules = (
        schedules_qs
        .filter(end_time__gte=now)
        .exclude(status__iexact='cancelled')
        .order_by('start_time')
    )
    calendar_tours = []
    for sched in actionable_schedules:
        local_start = timezone.localtime(sched.start_time) if timezone.is_aware(sched.start_time) else sched.start_time
        local_end = timezone.localtime(sched.end_time) if timezone.is_aware(sched.end_time) else sched.end_time
        calendar_tours.append({
            "schedId": sched.sched_id,
            "tourName": sched.tour.tour_name,
            "start": local_start.date().isoformat(),
            "end": local_end.date().isoformat(),
            "startDateTime": local_start.isoformat(),
            "endDateTime": local_end.isoformat(),
            "price": f"PHP {sched.price:.2f}",
            "confirmedBookings": int(sched.slots_booked or 0),
            "slotsAvailable": int(sched.slots_available or 0),
            "status": sched.status,
            "color": color_map.get(sched.status, "#fbbc05"),
            "description": sched.tour.description or "",
        })

    # Ongoing tours card area (employee-assigned active schedules)
    ongoing_tours = schedules_qs.filter(status='active').order_by('start_time')[:10]
    completed_history = (
        schedules_qs
        .filter(end_time__lt=now)
        .exclude(status__iexact='cancelled')
        .order_by('-end_time')[:20]
    )

    context = {
        'employee': employee,
        'assignments': assignments,
        'completed_tours': completed_tours,
        'active_tours': active_tours,
        'cancelled_tours': cancelled_tours,
        'total_revenue': total_revenue,
        'employee_growth': employee_growth,
        'employee_growth_width': employee_growth_width,
        'total_survey_responses': UsabilitySurveyResponse.objects.count(),
        'average_percentage': average_percentage,
        'fully_booked': fully_booked,
        'almost_full': almost_full,
        'moderate_booking': moderate_booking,
        'low_booking': low_booking,
        'popular_tours': popular_tours,
        'total_booked_sum': total_booked_sum,
        'most_popular_tour': most_popular_tour,
        'least_popular_tour': least_popular_tour,
        'fastest_growing_tour': fastest_growing_tour,
        'booking_forecasts': booking_forecasts,
        'booking_insights': booking_insights,
        'booking_recommendations': booking_recommendations,
        'calendar_tours': calendar_tours,
        'completed_history': completed_history,
        'ongoing_tours': ongoing_tours,
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
    
    # Keep statuses fresh, then hide expired schedules from actionable assigned lists.
    Tour_Schedule.get_tour_statistics()
    now = timezone.now()

    # Get assigned tours for this employee only (upcoming/ongoing)
    assignments = (
        TourAssignment.objects
        .filter(employee=employee, schedule__end_time__gte=now)
        .exclude(schedule__status__iexact='cancelled')
        .select_related('schedule', 'schedule__tour')
        .order_by('schedule__start_time')
    )
    assigned_schedule_ids = list(assignments.values_list("schedule_id", flat=True))
    pending_requests = (
        Pending.objects.select_related("guest_id", "tour_id", "sched_id")
        .filter(sched_id_id__in=assigned_schedule_ids, status__iexact="pending")
        .filter(sched_id__end_time__gte=now)
        .exclude(sched_id__status__iexact="cancelled")
        .order_by("sched_id__start_time", "-id")
    )
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed assigned tours')
    
    context = {
        'employee': employee,
        'assignments': assignments,
        'pending_requests': pending_requests,
    }
    return render(request, 'employee_assigned_tours.html', context)


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
    
    # Keep statuses fresh, then keep calendar focused on non-expired schedules.
    Tour_Schedule.get_tour_statistics()
    now = timezone.now()
    assignments = (
        TourAssignment.objects
        .filter(employee=employee, schedule__end_time__gte=now)
        .exclude(schedule__status__iexact='cancelled')
        .select_related('schedule', 'schedule__tour')
    )
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed tour calendar')
    
    color_map = {
        "active": "#34a853",
        "completed": "#4285f4",
        "cancelled": "#ea4335",
    }
    calendar_tours = []
    for assignment in assignments:
        sched = assignment.schedule
        local_start = timezone.localtime(sched.start_time) if timezone.is_aware(sched.start_time) else sched.start_time
        local_end = timezone.localtime(sched.end_time) if timezone.is_aware(sched.end_time) else sched.end_time
        calendar_tours.append({
            "schedId": sched.sched_id,
            "tourName": sched.tour.tour_name,
            "start": local_start.date().isoformat(),
            "end": local_end.date().isoformat(),
            "startDateTime": local_start.isoformat(),
            "endDateTime": local_end.isoformat(),
            "price": f"PHP {sched.price:.2f}",
            "confirmedBookings": int(sched.slots_booked or 0),
            "slotsAvailable": int(sched.slots_available or 0),
            "status": sched.status,
            "color": color_map.get(sched.status, "#fbbc05"),
            "description": sched.tour.description or "",
        })

    initial_calendar_date = ""

    context = {
        'employee': employee,
        'assignments': assignments,
        'page_title': 'Assigned Tour Calendar',
        'calendar_tours': calendar_tours,
        'initial_calendar_date': initial_calendar_date,
    }
    return render(request, 'tour_calendar.html', context)


def _employee_event_completion_map(session):
    payload = session.get("employee_event_completion")
    return payload if isinstance(payload, dict) else {}


def _save_employee_event_completion_map(session, payload):
    session["employee_event_completion"] = payload
    session.modified = True


def get_employee_itinerary(request, tour_id):
    """Return assigned schedule itinerary JSON for the logged-in employee."""
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return JsonResponse({'success': False, 'error': 'Authentication required.'}, status=401)

    employee = get_object_or_404(Employee, emp_id=request.session.get('employee_id'))
    assignment = (
        TourAssignment.objects.select_related('schedule', 'schedule__tour')
        .filter(employee=employee, schedule__sched_id=tour_id)
        .first()
    )
    if assignment is None:
        return JsonResponse({'success': False, 'error': 'Tour assignment not found.'}, status=404)

    schedule = assignment.schedule
    events = Tour_Event.objects.filter(sched_id=schedule).order_by('day_number', 'event_time')

    completion_map = _employee_event_completion_map(request.session)
    employee_key = str(employee.emp_id)
    assignment_key = str(assignment.id)
    completed_for_assignment = (
        completion_map.get(employee_key, {}).get(assignment_key, {})
        if isinstance(completion_map.get(employee_key, {}), dict)
        else {}
    )

    event_rows = []
    completed_count = 0
    for event in events:
        event_key = str(event.event_ID)
        is_completed = bool(completed_for_assignment.get(event_key, False))
        if is_completed:
            completed_count += 1
        event_rows.append({
            "id": event.event_ID,
            "day_number": int(event.day_number or 1),
            "event_time": event.event_time.isoformat() if event.event_time else "",
            "event_name": event.event_name,
            "event_description": event.event_description or "",
            "is_completed": is_completed,
        })

    total_events = len(event_rows)
    completion_percentage = round((completed_count / total_events) * 100, 2) if total_events else 0

    return JsonResponse({
        'success': True,
        'assignment_id': assignment.id,
        'tour_name': schedule.tour.tour_name,
        'duration_days': int(schedule.duration_days or 1),
        'completion_percentage': completion_percentage,
        'events': event_rows,
    })


@require_POST
def update_event_status(request):
    """Persist employee itinerary checklist status in session (no schema changes)."""
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return JsonResponse({'success': False, 'error': 'Authentication required.'}, status=401)

    employee = get_object_or_404(Employee, emp_id=request.session.get('employee_id'))
    try:
        payload = json.loads(request.body.decode('utf-8') or "{}")
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON payload.'}, status=400)

    event_id = str(payload.get("event_id") or "").strip()
    assignment_id = str(payload.get("assignment_id") or "").strip()
    is_completed = bool(payload.get("is_completed", False))
    if not event_id or not assignment_id:
        return JsonResponse({'success': False, 'error': 'event_id and assignment_id are required.'}, status=400)

    assignment = (
        TourAssignment.objects.select_related('schedule')
        .filter(id=assignment_id, employee=employee)
        .first()
    )
    if assignment is None:
        return JsonResponse({'success': False, 'error': 'Assignment not found.'}, status=404)

    event = Tour_Event.objects.filter(event_ID=event_id, sched_id=assignment.schedule).first()
    if event is None:
        return JsonResponse({'success': False, 'error': 'Event not found for this assignment.'}, status=404)

    completion_map = _employee_event_completion_map(request.session)
    employee_key = str(employee.emp_id)
    completion_map.setdefault(employee_key, {})
    employee_assignments = completion_map[employee_key]
    if not isinstance(employee_assignments, dict):
        employee_assignments = {}
        completion_map[employee_key] = employee_assignments

    employee_assignments.setdefault(assignment_id, {})
    assignment_events = employee_assignments[assignment_id]
    if not isinstance(assignment_events, dict):
        assignment_events = {}
        employee_assignments[assignment_id] = assignment_events

    assignment_events[event_id] = is_completed
    _save_employee_event_completion_map(request.session, completion_map)

    total_events = Tour_Event.objects.filter(sched_id=assignment.schedule).count()
    completed_count = sum(1 for value in assignment_events.values() if bool(value))
    completion_percentage = round((completed_count / total_events) * 100, 2) if total_events else 0

    return JsonResponse({
        'success': True,
        'completion_percentage': completion_percentage,
    })


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
    accommodations = Accomodation.objects.filter(approval_status="accepted", is_active=True).order_by("company_name")
    company_type_filter = str(request.GET.get("company_type", "") or "").strip().lower()
    if company_type_filter == "hotel":
        accommodations = accommodations.filter(company_type__icontains="hotel")
    elif company_type_filter == "attraction":
        accommodations = accommodations.filter(
            Q(company_type__icontains="attraction")
            | Q(company_type__icontains="tourist")
            | Q(company_type__icontains="spot")
        )
    elif company_type_filter == "mie":
        accommodations = accommodations.filter(
            Q(company_type__icontains="mie")
            | Q(company_type__icontains="meeting")
            | Q(company_type__icontains="event")
            | Q(company_type__icontains="incentive")
        )
    
    # Log the activity
    log_activity(request, employee, 'view_page', description='Viewed accommodations')
    
    context = {
        'employee': employee,
        'accommodations': accommodations,
        'company_type_filter': company_type_filter,
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


def employee_notifications(request):
    """Show employee notifications based on account activity and tour assignments."""
    if request.session.get('user_type') != 'employee' or not request.session.get('employee_id'):
        return redirect('admin_app:login')

    try:
        employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
    except Employee.DoesNotExist:
        return redirect('admin_app:login')

    activity_rows = (
        UserActivity.objects
        .filter(employee=employee)
        .order_by('-timestamp')[:60]
    )
    assignment_rows = (
        TourAssignment.objects
        .filter(employee=employee)
        .select_related('schedule', 'schedule__tour')
        .order_by('-assigned_date')[:30]
    )

    notifications = []

    for assignment in assignment_rows:
        sched = assignment.schedule
        notifications.append({
            'kind': 'assignment',
            'title': 'Tour Assignment',
            'message': f"You were assigned to {sched.tour.tour_name} (Schedule {sched.sched_id}).",
            'timestamp': assignment.assigned_date,
            'status': str(getattr(sched, 'status', '') or '').title(),
            'cta_label': 'View Itinerary',
            'cta_url': reverse('tour_app:itinerary', kwargs={'sched_id': sched.sched_id}),
        })

    for row in activity_rows:
        notifications.append({
            'kind': 'activity',
            'title': row.get_activity_type_display(),
            'message': row.description or 'Activity recorded.',
            'timestamp': row.timestamp,
            'status': '',
            'cta_label': '',
            'cta_url': '',
        })

    notifications.sort(key=lambda item: item.get('timestamp') or timezone.now(), reverse=True)

    log_activity(request, employee, 'view_page', description='Viewed employee notifications')
    context = {
        'employee': employee,
        'notifications': notifications[:80],
    }
    return render(request, 'employee_notifications.html', context)


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

    # Keep schedule statuses synchronized before dashboard queries.
    Tour_Schedule.get_tour_statistics()

    # Active tours list for assignment widget (hide expired/cancelled tours)
    active_tours = Tour_Schedule.objects.filter(
        status='active',
        end_time__gte=timezone.now(),
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

    # Build dashboard stat blocks expected by template
    tour_stats = Tour_Schedule.get_tour_statistics()
    weekly_stats = Tour_Schedule.get_tour_statistics(period='weekly')
    monthly_stats = Tour_Schedule.get_tour_statistics(period='monthly')
    yearly_stats = Tour_Schedule.get_tour_statistics(period='yearly')

    def _growth(current, previous):
        if previous == 0:
            return 100.0 if current > 0 else 0.0
        return ((current - previous) / previous) * 100.0

    # Previous period references for growth
    now = timezone.now()
    prev_week_start = now - dt.timedelta(days=14)
    prev_week_end = now - dt.timedelta(days=7)
    prev_month_start = (now.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    prev_month_end = now.replace(day=1) - dt.timedelta(seconds=1)
    prev_year_start = now.replace(year=now.year - 1, month=1, day=1)
    prev_year_end = now.replace(year=now.year - 1, month=12, day=31, hour=23, minute=59, second=59)

    prev_weekly = Tour_Schedule.get_tour_statistics(custom_start=prev_week_start, custom_end=prev_week_end)
    prev_monthly = Tour_Schedule.get_tour_statistics(custom_start=prev_month_start, custom_end=prev_month_end)
    prev_yearly = Tour_Schedule.get_tour_statistics(custom_start=prev_year_start, custom_end=prev_year_end)

    # All-time growth baseline:
    # compare current cumulative stats vs an equivalent previous time window.
    earliest_end = Tour_Schedule.objects.order_by('end_time').values_list('end_time', flat=True).first()
    if earliest_end:
        span_days = max(1, int((now - earliest_end).days) + 1)
        prev_all_start = earliest_end - dt.timedelta(days=span_days)
        prev_all_end = earliest_end
        prev_all_time = Tour_Schedule.get_tour_statistics(
            custom_start=prev_all_start,
            custom_end=prev_all_end,
        )
    else:
        prev_all_time = {
            'completed_tours': 0,
            'active_tours': 0,
            'cancelled_tours': 0,
            'total_revenue': 0,
        }

    all_time_growth = {
        'completed_tours': _growth(tour_stats['completed_tours'], prev_all_time['completed_tours']),
        'active_tours': _growth(tour_stats['active_tours'], prev_all_time['active_tours']),
        'cancelled_tours': _growth(tour_stats['cancelled_tours'], prev_all_time['cancelled_tours']),
        'total_revenue': _growth(float(tour_stats['total_revenue']), float(prev_all_time['total_revenue'])),
    }
    weekly_growth = {
        'completed_tours': _growth(weekly_stats['completed_tours'], prev_weekly['completed_tours']),
        'active_tours': _growth(weekly_stats['active_tours'], prev_weekly['active_tours']),
        'cancelled_tours': _growth(weekly_stats['cancelled_tours'], prev_weekly['cancelled_tours']),
        'total_revenue': _growth(float(weekly_stats['total_revenue']), float(prev_weekly['total_revenue'])),
    }
    monthly_growth = {
        'completed_tours': _growth(monthly_stats['completed_tours'], prev_monthly['completed_tours']),
        'active_tours': _growth(monthly_stats['active_tours'], prev_monthly['active_tours']),
        'cancelled_tours': _growth(monthly_stats['cancelled_tours'], prev_monthly['cancelled_tours']),
        'total_revenue': _growth(float(monthly_stats['total_revenue']), float(prev_monthly['total_revenue'])),
    }
    yearly_growth = {
        'completed_tours': _growth(yearly_stats['completed_tours'], prev_yearly['completed_tours']),
        'active_tours': _growth(yearly_stats['active_tours'], prev_yearly['active_tours']),
        'cancelled_tours': _growth(yearly_stats['cancelled_tours'], prev_yearly['cancelled_tours']),
        'total_revenue': _growth(float(yearly_stats['total_revenue']), float(prev_yearly['total_revenue'])),
    }

    def _stat_card_widths(stats, revenue_baseline=0):
        total_tours = int(
            (stats.get('completed_tours') or 0)
            + (stats.get('active_tours') or 0)
            + (stats.get('cancelled_tours') or 0)
        )
        total_tours = max(total_tours, 1)
        completed_width = max(8, min(100, round(((stats.get('completed_tours') or 0) / total_tours) * 100)))
        active_width = max(8, min(100, round(((stats.get('active_tours') or 0) / total_tours) * 100)))
        cancelled_width = max(8, min(100, round(((stats.get('cancelled_tours') or 0) / total_tours) * 100)))
        baseline = float(revenue_baseline or 0)
        revenue_value = float(stats.get('total_revenue') or 0)
        if baseline <= 0:
            revenue_width = 8 if revenue_value > 0 else 0
        else:
            revenue_width = max(8, min(100, round((revenue_value / baseline) * 100))) if revenue_value > 0 else 0
        return {
            'completed': int(completed_width),
            'active': int(active_width),
            'cancelled': int(cancelled_width),
            'revenue': int(revenue_width),
        }

    # Booking status visualization stats (active/upcoming only)
    schedules = (
        Tour_Schedule.objects.select_related('tour')
        .filter(status='active', end_time__gte=timezone.now())
        .order_by('start_time')
    )
    tour_bookings = []
    full_tours = almost_full_tours = moderate_tours = low_tours = 0
    percentage_values = []

    aggregated_by_tour = {}
    for sched in schedules:
        total_slots = int((sched.slots_booked or 0) + (sched.slots_available or 0))
        if total_slots > 0:
            percentage = round((int(sched.slots_booked or 0) / total_slots) * 100)
        else:
            percentage = 0

        if percentage >= 100:
            status = 'full'
            full_tours += 1
        elif percentage >= 75:
            status = 'almost-full'
            almost_full_tours += 1
        elif percentage >= 40:
            status = 'moderate'
            moderate_tours += 1
        else:
            status = 'low'
            low_tours += 1

        percentage_values.append(percentage)
        # Simple projection for the next month based on current fill ratio.
        forecast_next_month = max(
            int(sched.slots_booked or 0),
            min(
                total_slots,
                round(int(sched.slots_booked or 0) + (total_slots * 0.12))
            ),
        )
        if int(sched.slots_booked or 0) > 0:
            monthly_growth_pct = round(((forecast_next_month - int(sched.slots_booked or 0)) / int(sched.slots_booked or 0)) * 100, 1)
        else:
            monthly_growth_pct = 0.0
        yearly_growth_pct = round(monthly_growth_pct * 3, 1)
        tour_key = (sched.tour_id, getattr(sched.tour, 'tour_name', ''))
        row = aggregated_by_tour.get(tour_key)
        if row is None:
            aggregated_by_tour[tour_key] = {
                'tour': sched.tour,
                'schedule': sched,
                'total_slots': total_slots,
                'booked_slots': int(sched.slots_booked or 0),
                'forecast_next_month': int(forecast_next_month or 0),
                'yearly_growth_total': float(yearly_growth_pct or 0.0),
                'count': 1,
            }
        else:
            row['total_slots'] += total_slots
            row['booked_slots'] += int(sched.slots_booked or 0)
            row['forecast_next_month'] += int(forecast_next_month or 0)
            row['yearly_growth_total'] += float(yearly_growth_pct or 0.0)
            row['count'] += 1

    # Rebuild normalized rows after aggregation
    full_tours = almost_full_tours = moderate_tours = low_tours = 0
    percentage_values = []
    for row in aggregated_by_tour.values():
        total_slots = int(row['total_slots'] or 0)
        booked_slots = int(row['booked_slots'] or 0)
        if total_slots > 0:
            percentage = round((booked_slots / total_slots) * 100)
        else:
            percentage = 0

        if percentage >= 100:
            status = 'full'
            full_tours += 1
        elif percentage >= 75:
            status = 'almost-full'
            almost_full_tours += 1
        elif percentage >= 40:
            status = 'moderate'
            moderate_tours += 1
        else:
            status = 'low'
            low_tours += 1

        percentage_values.append(percentage)

        forecast_next_month = int(row['forecast_next_month'] or booked_slots)
        if booked_slots > 0:
            monthly_growth_pct = round(((forecast_next_month - booked_slots) / booked_slots) * 100, 1)
        else:
            monthly_growth_pct = 0.0
        yearly_growth_pct = round((row['yearly_growth_total'] / max(1, row['count'])), 1)

        tour_bookings.append({
            'tour': row['tour'],
            'schedule': row['schedule'],
            'percentage': percentage,
            'status': status,
            'remaining': max(0, 100 - percentage),
            'booked_slots': booked_slots,
            'forecast_next_month': forecast_next_month,
            'monthly_growth': monthly_growth_pct,
            'yearly_growth': yearly_growth_pct,
        })

    # Keep rankings stable for "most/least popular" sections.
    tour_bookings.sort(key=lambda x: x['percentage'], reverse=True)

    average_percentage = round(sum(percentage_values) / len(percentage_values)) if percentage_values else 0
    weekly_average = average_percentage
    monthly_average = average_percentage
    yearly_average = average_percentage

    # Insights/recommendations cards
    booking_insights = []
    if monthly_growth['completed_tours'] > 0:
        booking_insights.append({
            'type': 'positive',
            'text': f"Tour bookings increased by {monthly_growth['completed_tours']:.1f}% over the last period."
        })
    if monthly_growth['cancelled_tours'] > 0:
        booking_insights.append({
            'type': 'negative',
            'text': f"Cancellations increased by {monthly_growth['cancelled_tours']:.1f}%."
        })
    booking_insights.append({
        'type': 'forecast',
        'text': f"Expected booking volume for next month: {sum(x['forecast_next_month'] for x in tour_bookings[:4])} bookings."
    })

    booking_recommendations = []
    if low_tours > 0:
        booking_recommendations.append({
            'type': 'improve',
            'text': "Boost low-booking tours with promo bundles and schedule visibility."
        })
    if full_tours > 0:
        booking_recommendations.append({
            'type': 'leverage',
            'text': "Open additional schedules for fully booked tours to capture demand."
        })

    # Survey summary for dashboard panel
    total_survey_responses = UsabilitySurveyResponse.objects.count()

    # Tourist influx from owner monthly reports (turnover-critical monitoring source)
    selected_influx_period = str(request.GET.get("influx_period") or "").strip()
    default_month_anchor = now.date().replace(day=1)
    month_anchor = default_month_anchor
    if selected_influx_period:
        try:
            month_anchor = dt.date.fromisoformat(f"{selected_influx_period}-01")
        except Exception:
            selected_influx_period = ""
            month_anchor = default_month_anchor
    selected_influx_period = month_anchor.strftime("%Y-%m")
    prev_month_anchor = (month_anchor - dt.timedelta(days=1)).replace(day=1)
    influx_statuses = ["submitted", "reviewed", "returned"]

    monthly_influx_qs = OwnerMonthlyReport.objects.filter(
        reporting_period=month_anchor,
        status__in=influx_statuses,
    )
    prev_month_influx_qs = OwnerMonthlyReport.objects.filter(
        reporting_period=prev_month_anchor,
        status__in=influx_statuses,
    )
    all_influx_qs = OwnerMonthlyReport.objects.filter(status__in=influx_statuses)

    monthly_influx = monthly_influx_qs.aggregate(
        report_count=Count("report_id"),
        checkins=Sum("guests_checked_in"),
        checkouts=Sum("guests_checked_out"),
        rooms_used=Sum("rooms_used"),
    )
    prev_month_influx = prev_month_influx_qs.aggregate(
        report_count=Count("report_id"),
        checkins=Sum("guests_checked_in"),
        checkouts=Sum("guests_checked_out"),
        rooms_used=Sum("rooms_used"),
    )
    all_time_influx = all_influx_qs.aggregate(
        report_count=Count("report_id"),
        checkins=Sum("guests_checked_in"),
        checkouts=Sum("guests_checked_out"),
        rooms_used=Sum("rooms_used"),
    )
    influx_period_choices = [
        period_value
        for period_value in all_influx_qs.order_by("-reporting_period")
        .values_list("reporting_period", flat=True)
        .distinct()
        if period_value is not None
    ]

    top_influx_accommodations = list(
        monthly_influx_qs.values("accommodation__company_name")
        .annotate(
            total_checkins=Sum("guests_checked_in"),
            total_checkouts=Sum("guests_checked_out"),
        )
        .order_by("-total_checkins", "-total_checkouts", "accommodation__company_name")[:5]
    )

    tourist_influx_summary = {
        "month_label": month_anchor.strftime("%B %Y"),
        "report_count": int(monthly_influx.get("report_count") or 0),
        "checkins": int(monthly_influx.get("checkins") or 0),
        "checkouts": int(monthly_influx.get("checkouts") or 0),
        "rooms_used": int(monthly_influx.get("rooms_used") or 0),
        "growth_checkins": _growth(
            int(monthly_influx.get("checkins") or 0),
            int(prev_month_influx.get("checkins") or 0),
        ),
        "growth_checkouts": _growth(
            int(monthly_influx.get("checkouts") or 0),
            int(prev_month_influx.get("checkouts") or 0),
        ),
        "growth_reports": _growth(
            int(monthly_influx.get("report_count") or 0),
            int(prev_month_influx.get("report_count") or 0),
        ),
        "all_time_reports": int(all_time_influx.get("report_count") or 0),
        "all_time_checkins": int(all_time_influx.get("checkins") or 0),
        "all_time_checkouts": int(all_time_influx.get("checkouts") or 0),
        "top_accommodations": top_influx_accommodations,
        "selected_period": selected_influx_period,
        "period_choices": influx_period_choices,
    }

    revenue_baseline = max(
        float(tour_stats.get('total_revenue') or 0),
        float(weekly_stats.get('total_revenue') or 0),
        float(monthly_stats.get('total_revenue') or 0),
        float(yearly_stats.get('total_revenue') or 0),
    )
    all_time_card_widths = _stat_card_widths(tour_stats, revenue_baseline=revenue_baseline)
    weekly_card_widths = _stat_card_widths(weekly_stats, revenue_baseline=revenue_baseline)
    monthly_card_widths = _stat_card_widths(monthly_stats, revenue_baseline=revenue_baseline)
    yearly_card_widths = _stat_card_widths(yearly_stats, revenue_baseline=revenue_baseline)

    # Add dashboard data to context
    context = {
        'active_tours_count': active_tours.count(),
        'active_tours': active_tours,
        'pending_bookings': Pending.objects.filter(status='Pending').count(),
        'total_users': Guest.objects.count(),
        'employees': employees,
        'tour_assignments': tour_assignments,
        'tour_stats': tour_stats,
        'weekly_stats': weekly_stats,
        'monthly_stats': monthly_stats,
        'yearly_stats': yearly_stats,
        'all_time_growth': all_time_growth,
        'weekly_growth': weekly_growth,
        'monthly_growth': monthly_growth,
        'yearly_growth': yearly_growth,
        'tour_bookings': tour_bookings,
        'average_percentage': average_percentage,
        'full_tours': full_tours,
        'almost_full_tours': almost_full_tours,
        'moderate_tours': moderate_tours,
        'low_tours': low_tours,
        'total_survey_responses': total_survey_responses,
        'weekly_average': weekly_average,
        'monthly_average': monthly_average,
        'yearly_average': yearly_average,
        'booking_insights': booking_insights,
        'booking_recommendations': booking_recommendations,
        'tourist_influx_summary': tourist_influx_summary,
        'all_time_card_widths': all_time_card_widths,
        'weekly_card_widths': weekly_card_widths,
        'monthly_card_widths': monthly_card_widths,
        'yearly_card_widths': yearly_card_widths,
    }
    
    return render(request, 'admin_dashboard.html', context)


@accomodation_required
def accommodation_dashboard(request):
    accom = getattr(request, "current_accommodation", None)
    if accom is None:
        return redirect('admin_app:login')
    
    username = getattr(accom, 'name', accom.email_address)
    # Set is_hotel to True if the company_type (case-insensitive) is "hotel".
    is_hotel = (accom.company_type.lower() == "hotel")
    
    context = {
        'username': username,
        'is_hotel': is_hotel,
        'company_name': getattr(accom, "company_name", None) or request.session.get("company_name") or "Accommodation",
        'company_location': (
            getattr(accom, "location", None)
            or getattr(accom, "address", None)
            or request.session.get("company_location")
            or request.session.get("location")
            or request.session.get("address")
            or "Bayawan City"
        ),
    }

    from admin_app.models import Room
    available_rooms = Room.objects.filter(
        accommodation=accom,
        status='AVAILABLE'
    )
    hotel_rooms = Room.objects.filter(
        accommodation=accom
    ).order_by("room_name")
    context['available_rooms'] = available_rooms
    context['hotel_rooms'] = hotel_rooms
    
    return render(request, 'admin_app/accommodation_dashboard.html', context)


@accomodation_required
def owner_room_bookings_json(request, room_id):
    """Return owner-scoped per-room booking rows for the dashboard guest panel."""
    accom = getattr(request, "current_accommodation", None)
    if accom is None:
        return JsonResponse({"status": "error", "message": "Unauthorized."}, status=403)

    from admin_app.models import Room

    room = get_object_or_404(Room, room_id=room_id, accommodation=accom)
    today = timezone.localdate()

    bookings = (
        AccommodationBooking.objects.select_related("guest")
        .filter(accommodation=accom, room=room)
        .exclude(status="cancelled")
        .order_by("check_in", "booking_id")
    )

    guest_rows = []
    for booking in bookings:
        guest = booking.guest
        status_raw = str(booking.status or "").strip().lower()
        if status_raw == "confirmed":
            if booking.check_in <= today < booking.check_out:
                display_status = "Checked-in"
            elif today < booking.check_in:
                display_status = "Confirmed (Not Yet Checked-in)"
            else:
                display_status = "Completed"
        elif status_raw == "pending":
            display_status = "Pending Confirmation"
        elif status_raw == "declined":
            display_status = "Declined"
        else:
            display_status = status_raw.title() if status_raw else "Unknown"

        photo_url = ""
        try:
            if getattr(guest, "picture", None):
                photo_url = guest.picture.url
        except Exception:
            photo_url = ""

        guest_rows.append(
            {
                "id": int(booking.booking_id),
                "booking_id": int(booking.booking_id),
                "first_name": str(getattr(guest, "first_name", "") or ""),
                "last_name": str(getattr(guest, "last_name", "") or ""),
                "photo_url": photo_url,
                "role": f"Booking #{booking.booking_id}",
                "group_type": "ALL",
                "status": display_status,
                "status_raw": status_raw,
                "check_in": booking.check_in.strftime("%b %d, %Y"),
                "check_out": booking.check_out.strftime("%b %d, %Y"),
                "guests": int(booking.num_guests or 0),
            }
        )

    return JsonResponse(
        {
            "status": "success",
            "room": {"id": int(room.room_id), "name": room.room_name},
            "guests": guest_rows,
            "count": len(guest_rows),
        }
    )


@require_POST
@accomodation_required
def owner_room_bookings_check_in(request):
    """
    Owner-side room check-in action for selected booking cards.
    Valid only for confirmed bookings whose stay includes today.
    """
    accom = getattr(request, "current_accommodation", None)
    if accom is None:
        return JsonResponse({"status": "error", "message": "Unauthorized."}, status=403)

    room_id_raw = request.POST.get("room_id")
    booking_ids_raw = request.POST.getlist("booking_ids[]") or request.POST.getlist("booking_ids")

    if not booking_ids_raw:
        return JsonResponse(
            {"status": "error", "message": "Select at least one booking to check in."},
            status=400,
        )

    booking_ids = []
    for raw in booking_ids_raw:
        try:
            booking_ids.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    if not booking_ids:
        return JsonResponse(
            {"status": "error", "message": "No valid booking IDs were provided."},
            status=400,
        )

    room_filter = {}
    if room_id_raw not in (None, ""):
        try:
            room_filter["room_id"] = int(str(room_id_raw).strip())
        except (TypeError, ValueError):
            return JsonResponse({"status": "error", "message": "Invalid room selected."}, status=400)

    selected_bookings = (
        AccommodationBooking.objects.select_related("room")
        .filter(accommodation=accom, booking_id__in=booking_ids, **room_filter)
        .order_by("booking_id")
    )

    today = timezone.localdate()
    checked_in_ids = []
    skipped = []
    touched_room_ids = set()

    for booking in selected_bookings:
        status_value = str(booking.status or "").strip().lower()
        if status_value != "confirmed":
            skipped.append(
                {
                    "booking_id": int(booking.booking_id),
                    "reason": "not_confirmed",
                    "message": "Booking is not confirmed yet.",
                }
            )
            continue
        if not (booking.check_in <= today < booking.check_out):
            skipped.append(
                {
                    "booking_id": int(booking.booking_id),
                    "reason": "outside_checkin_window",
                    "message": "Check-in is only available from check-in date until before check-out date.",
                }
            )
            continue

        checked_in_ids.append(int(booking.booking_id))
        if booking.room_id:
            touched_room_ids.add(int(booking.room_id))
            sync_room_current_availability(booking.room)

    if checked_in_ids:
        return JsonResponse(
            {
                "status": "success",
                "checked_in_count": len(checked_in_ids),
                "checked_in_ids": checked_in_ids,
                "skipped": skipped,
                "room_ids": sorted(touched_room_ids),
                "message": f"Checked in {len(checked_in_ids)} booking(s).",
            }
        )

    skip_reasons = {
        str(item.get("reason") or "").strip().lower()
        for item in skipped
        if isinstance(item, dict)
    }
    if skip_reasons == {"not_confirmed"}:
        message_text = (
            "Selected booking(s) are still pending admin confirmation. "
            "Check-in is disabled until the booking status becomes Confirmed."
        )
    elif skip_reasons == {"outside_checkin_window"}:
        message_text = (
            "Selected booking(s) are outside the check-in window. "
            "Check-in is only allowed from check-in date until before check-out date."
        )
    else:
        message_text = "No selected bookings are currently eligible for check-in."

    return JsonResponse(
        {
            "status": "error",
            "checked_in_count": 0,
            "checked_in_ids": [],
            "skipped": skipped,
            "message": message_text,
        },
        status=400,
    )



# ------------------------------------------------------------------------------
# Standard Form Processing (if needed for batch updates)
# ------------------------------------------------------------------------------
@ensure_csrf_cookie
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
    if request.session.get('user_type') != 'establishment' or not request.session.get('accom_id'):
        return redirect('admin_app:login')

    try:
        accom = Accomodation.objects.get(accom_id=request.session.get('accom_id'))
    except Accomodation.DoesNotExist:
        return redirect('admin_app:login')

    is_hotel = (accom.company_type or "").lower() == "hotel"
    context = {
        'username': accom.company_name or accom.email_address,
        'is_hotel': is_hotel,
        'submissions_count': 0,
        'pending_forms': 0,
        'completed_forms': 0,
        'recent_activities': [],
    }

    if is_hotel:
        from admin_app.models import Room
        context['available_rooms'] = Room.objects.filter(
            accommodation=accom,
            status='AVAILABLE'
        )

    return render(request, 'estab_dashboard.html', context)


@admin_required
def tourism_information_manage(request):
    query = str(request.GET.get("q", "") or "").strip()
    status = str(request.GET.get("status", "") or "").strip().lower()

    rows = TourismInformation.objects.all().order_by("spot_name", "-updated_at")
    if query:
        rows = rows.filter(
            Q(spot_name__icontains=query)
            | Q(description__icontains=query)
            | Q(location__icontains=query)
            | Q(contact_information__icontains=query)
        )
    if status in {"draft", "published", "archived"}:
        rows = rows.filter(publication_status=status)

    context = {
        "tourism_rows": rows,
        "query": query,
        "status_filter": status,
    }
    return render(request, "tourism_information_manage.html", context)


@admin_required
def mainpage_photos(request):
    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip().lower()
        try:
            if action == "upload_logo":
                image_file = request.FILES.get("logo_image")
                if not image_file:
                    messages.error(request, "Please choose a logo image to upload.")
                else:
                    upload_logo(
                        image_file=image_file,
                        title=request.POST.get("logo_title", ""),
                        set_active=bool(request.POST.get("logo_set_active")),
                    )
                    messages.success(request, "Logo uploaded successfully.")
            elif action == "set_active_logo":
                logo_id = request.POST.get("logo_id")
                if logo_id:
                    set_active_logo(logo_id)
                    messages.success(request, "Active logo updated.")
            elif action == "delete_logo":
                logo_id = request.POST.get("logo_id")
                if logo_id:
                    delete_logo(logo_id)
                    messages.success(request, "Logo deleted.")
            elif action == "upload_hero":
                image_file = request.FILES.get("hero_image")
                if not image_file:
                    messages.error(request, "Please choose a hero image to upload.")
                else:
                    upload_hero(
                        image_file=image_file,
                        title=request.POST.get("hero_title", ""),
                        display_order=request.POST.get("hero_display_order", 1),
                        set_active=bool(request.POST.get("hero_set_active")),
                    )
                    messages.success(request, "Hero image uploaded successfully.")
            elif action == "set_active_hero":
                hero_id = request.POST.get("hero_id")
                if hero_id:
                    set_active_hero(hero_id)
                    messages.success(request, "Active hero image updated.")
            elif action == "delete_hero":
                hero_id = request.POST.get("hero_id")
                if hero_id:
                    delete_hero(hero_id)
                    messages.success(request, "Hero image deleted.")
        except Exception:
            messages.error(request, "Unable to process request right now. Please try again.")
        return redirect("admin_app:mainpage_photos")

    context = get_mainpage_media_admin_context()
    return render(request, "mainpage_photos.html", context)


@admin_required
def tourism_information_create(request):
    if request.method == "POST":
        form = TourismInformationForm(request.POST, request.FILES)
        if form.is_valid():
            row = form.save(commit=False)
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                row.created_by = user
                row.updated_by = user
            row.save()
            messages.success(request, "Tourism information created successfully.")
            return redirect("admin_app:tourism_information_manage")
    else:
        form = TourismInformationForm()

    return render(
        request,
        "tourism_information_form.html",
        {"form": form, "page_title": "Add Tourism Information", "is_edit": False},
    )


@admin_required
def tourism_information_edit(request, tourism_info_id):
    row = get_object_or_404(TourismInformation, tourism_info_id=tourism_info_id)
    if request.method == "POST":
        form = TourismInformationForm(request.POST, request.FILES, instance=row)
        if form.is_valid():
            updated_row = form.save(commit=False)
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                updated_row.updated_by = user
            updated_row.save()
            messages.success(request, "Tourism information updated successfully.")
            return redirect("admin_app:tourism_information_manage")
    else:
        form = TourismInformationForm(instance=row)

    return render(
        request,
        "tourism_information_form.html",
        {
            "form": form,
            "page_title": f"Edit Tourism Information: {row.spot_name}",
            "is_edit": True,
            "tourism_row": row,
        },
    )


@admin_required
@require_POST
def tourism_information_publish(request, tourism_info_id):
    row = get_object_or_404(TourismInformation, tourism_info_id=tourism_info_id)
    row.publication_status = "published"
    row.is_active = True
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        row.updated_by = user
        row.save(update_fields=["publication_status", "is_active", "updated_by", "updated_at"])
    else:
        row.save(update_fields=["publication_status", "is_active", "updated_at"])
    messages.success(request, f"Published tourism information: {row.spot_name}")
    return redirect("admin_app:tourism_information_manage")


@admin_required
@require_POST
def tourism_information_archive(request, tourism_info_id):
    row = get_object_or_404(TourismInformation, tourism_info_id=tourism_info_id)
    row.publication_status = "archived"
    row.is_active = False
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        row.updated_by = user
        row.save(update_fields=["publication_status", "is_active", "updated_by", "updated_at"])
    else:
        row.save(update_fields=["publication_status", "is_active", "updated_at"])
    messages.success(request, f"Archived tourism information: {row.spot_name}")
    return redirect("admin_app:tourism_information_manage")

# ------------------------------------------------------------------------------
# AJAX Endpoints for Immediate (Auto) Save
# ------------------------------------------------------------------------------

# ----- REGION Endpoints -----
@require_POST
@admin_required
def ajax_add_region(request):
    region_name = request.POST.get('name', '').strip()
    if region_name:
        region, created = Region.objects.get_or_create(name=region_name)
        return JsonResponse({'status': 'success', 'region_id': region.id, 'region_name': region.name})
    return JsonResponse({'status': 'error', 'error': 'Missing region name'}, status=400)

@require_POST
@admin_required
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
@admin_required
def ajax_delete_region(request):
    region_id = request.POST.get('region_id')
    if region_id:
        region = get_object_or_404(Region, id=region_id)
        region.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid region id'}, status=400)

# ----- COUNTRY Endpoints -----
@require_POST
@admin_required
def ajax_add_country(request):
    region_id = request.POST.get('region_id')
    country_name = request.POST.get('name', '').strip()
    if region_id and country_name:
        region = get_object_or_404(Region, id=region_id)
        country, created = Country.objects.get_or_create(name=country_name, region=region)
        return JsonResponse({'status': 'success', 'country_id': country.id, 'country_name': country.name})
    return JsonResponse({'status': 'error', 'error': 'Invalid parameters'}, status=400)

@require_POST
@admin_required
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
@admin_required
def ajax_delete_country(request):
    country_id = request.POST.get('country_id')
    if country_id:
        country = get_object_or_404(Country, id=country_id)
        country.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid country id'}, status=400)

# ----- ENTRY Endpoints -----
@require_POST
@admin_required
def ajax_add_entry(request):
    entry_title = request.POST.get('title', '').strip()
    if entry_title:
        entry, created = Entry.objects.get_or_create(title=entry_title)
        return JsonResponse({'status': 'success', 'entry_id': entry.id, 'entry_title': entry.title})
    return JsonResponse({'status': 'error', 'error': 'Missing entry title'}, status=400)

@require_POST
@admin_required
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
@admin_required
def ajax_delete_entry(request):
    entry_id = request.POST.get('entry_id')
    if entry_id:
        entry = get_object_or_404(Entry, id=entry_id)
        entry.delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error', 'error': 'Invalid entry id'}, status=400)

@require_POST
@admin_required
def ajax_mark_as_hotel(request):
    entry_id = request.POST.get("entry_id")
    status = request.POST.get("status")  # "yes" for marking as hotel; "no" for unmarking
    try:
        entry = Entry.objects.get(id=entry_id)
        entry.is_hotel = status == "yes"
        entry.save()
        return JsonResponse({"status": "success", "message": "Updated successfully."})
    except Entry.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Entry not found."}, status=404)

@require_POST
@admin_required
def ajax_mark_summary_as_hotel(request):
    summary_id = request.POST.get("summary_id")
    status = request.POST.get("status")  # "1" for highlighted, "0" for unhighlighted
    try:
        summary = Summary.objects.get(id=summary_id)
        # Mimic the is_hotel logic from the Entry form
        summary.hotel = "1" if status == "1" else "0"
        summary.save()
        return JsonResponse({"status": "success", "hotel": summary.hotel})
    except Summary.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Summary not found"}, status=404)


@admin_required
def tour_calendar(request):
    """
    View function for displaying the tour calendar interface.
    """
    Tour_Schedule.get_tour_statistics()

    schedules = (
        Tour_Schedule.objects.select_related("tour")
        .filter(
            status="active",
            end_time__gte=timezone.now(),
        )
        .order_by("start_time")
    )

    color_map = {
        "active": "#34a853",
        "completed": "#4285f4",
        "cancelled": "#ea4335",
    }

    calendar_tours = []
    for sched in schedules:
        local_start = timezone.localtime(sched.start_time) if timezone.is_aware(sched.start_time) else sched.start_time
        local_end = timezone.localtime(sched.end_time) if timezone.is_aware(sched.end_time) else sched.end_time
        calendar_tours.append({
            "schedId": sched.sched_id,
            "tourName": sched.tour.tour_name,
            "start": local_start.date().isoformat(),
            "end": local_end.date().isoformat(),
            "startDateTime": local_start.isoformat(),
            "endDateTime": local_end.isoformat(),
            "price": f"PHP {sched.price:.2f}",
            "confirmedBookings": int(sched.slots_booked or 0),
            "slotsAvailable": int(sched.slots_available or 0),
            "status": sched.status,
            "color": color_map.get(sched.status, "#fbbc05"),
            "description": sched.tour.description or "",
        })

    initial_calendar_date = timezone.localdate().isoformat()
    if calendar_tours:
        initial_calendar_date = calendar_tours[0]["start"]

    completed_history = (
        Tour_Schedule.objects.select_related("tour")
        .filter(
            status="completed",
            end_time__lt=timezone.now(),
        )
        .order_by("-end_time")[:20]
    )

    context = {
        'page_title': 'Tour Calendar',
        'calendar_tours': calendar_tours,
        'initial_calendar_date': initial_calendar_date,
        'completed_history': completed_history,
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


SURVEY_SOURCE_OPTIONS = ("all", "unlabeled", "demo_seeded", "pilot_test", "real_world")


def _normalize_survey_source(raw_value):
    source = str(raw_value or "all").strip().lower()
    if source not in SURVEY_SOURCE_OPTIONS:
        return "all"
    return source


def _to_positive_int(raw_value, default_value, max_value):
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        parsed = default_value
    if parsed < 1:
        parsed = default_value
    return min(parsed, max_value)


def _build_survey_results_payload(days=30, source="all", limit=100):
    selected_source = _normalize_survey_source(source)
    days_window = _to_positive_int(days, 30, 3650)
    row_limit = _to_positive_int(limit, 100, 1000)

    since = timezone.now() - dt.timedelta(days=days_window)
    qs = UsabilitySurveyResponse.objects.filter(submitted_at__gte=since)
    if selected_source != "all":
        qs = qs.filter(data_source=selected_source)

    total_responses = qs.count()
    total_batches = qs.exclude(survey_batch_id="").values("survey_batch_id").distinct().count()
    total_users = qs.exclude(user_id=None).values("user_id").distinct().count()

    quick_helpfulness_avg = qs.filter(statement_code="CHAT_UX_HELPFULNESS").aggregate(
        avg=Avg("likert_score")
    )["avg"]

    per_statement = list(
        qs.values("statement_code")
        .annotate(response_count=Count("response_id"), avg_score=Avg("likert_score"))
        .order_by("statement_code")
    )
    for row in per_statement:
        if row.get("avg_score") is not None:
            row["avg_score"] = round(float(row["avg_score"]), 3)

    recent_rows = qs.select_related("user").order_by("-submitted_at")[:row_limit]
    recent_data = []
    for row in recent_rows:
        user_obj = getattr(row, "user", None)
        recent_data.append(
            {
                "response_id": row.response_id,
                "submitted_at": row.submitted_at.isoformat() if row.submitted_at else "",
                "statement_code": row.statement_code,
                "likert_score": row.likert_score,
                "survey_batch_id": row.survey_batch_id or "",
                "data_source": row.data_source,
                "user_id": getattr(user_obj, "pk", None),
                "username": getattr(user_obj, "username", "") if user_obj else "",
            }
        )

    return {
        "filters": {
            "days": days_window,
            "source": selected_source,
            "limit": row_limit,
        },
        "summary": {
            "total_responses": total_responses,
            "total_batches": total_batches,
            "distinct_users": total_users,
            "chat_ux_helpfulness_avg": round(float(quick_helpfulness_avg), 3)
            if quick_helpfulness_avg is not None
            else None,
        },
        "per_statement": per_statement,
        "recent_rows": recent_data,
    }


@admin_required
def survey_results_dashboard(request):
    month_raw = str(request.GET.get("month", "all") or "all").strip().lower()
    year_raw = str(request.GET.get("year", "all") or "all").strip().lower()
    establishment_type = str(request.GET.get("establishment_type", "all") or "all").strip().lower()

    month_num = None
    if month_raw not in {"", "all"}:
        try:
            parsed_month = int(month_raw)
            if 1 <= parsed_month <= 12:
                month_num = parsed_month
        except (TypeError, ValueError):
            month_num = None

    year_num = None
    if year_raw not in {"", "all"}:
        try:
            parsed_year = int(year_raw)
            if 1900 <= parsed_year <= 2200:
                year_num = parsed_year
        except (TypeError, ValueError):
            year_num = None

    def _apply_month_year(qs, field_name):
        if month_num is not None:
            qs = qs.filter(**{f"{field_name}__month": month_num})
        if year_num is not None:
            qs = qs.filter(**{f"{field_name}__year": year_num})
        return qs

    # MICE event reports
    mice_qs = _apply_month_year(
        mies_table.objects.select_related("accom_id").all().order_by("-time_start"),
        "time_start",
    )
    mice_reports = []
    for row in mice_qs[:60]:
        mice_reports.append(
            {
                "title": row.event_name or "Untitled MICE Event",
                "location": row.meeting_place or getattr(row.accom_id, "location", "") or "N/A",
                "date": timezone.localtime(row.time_start).strftime("%b %d, %Y") if row.time_start else "N/A",
                "participants": int(row.grandtotal or row.total or row.subtotal or 0),
                "tag": "MICE",
            }
        )

    # Accommodation reports
    accommodation_qs = _apply_month_year(
        Accomodation.objects.filter(approval_status="accepted").order_by("-submitted_at"),
        "submitted_at",
    )
    accommodation_reports = []
    for accom in accommodation_qs[:80]:
        participants = (
            AccommodationBooking.objects.filter(
                accommodation=accom,
                status__in=["pending", "confirmed"],
            ).aggregate(total=Sum("num_guests")).get("total")
            or 0
        )
        accommodation_reports.append(
            {
                "title": accom.company_name,
                "location": accom.location or "N/A",
                "date": timezone.localtime(accom.submitted_at).strftime("%b %d, %Y") if accom.submitted_at else "N/A",
                "participants": int(participants),
                "tag": "Accommodation",
            }
        )

    # Attractions reports
    attractions_qs = _apply_month_year(
        TourismInformation.objects.filter(is_active=True, publication_status="published").order_by("-updated_at"),
        "updated_at",
    )
    attractions_reports = []
    for spot in attractions_qs[:80]:
        attractions_reports.append(
            {
                "title": spot.spot_name,
                "location": spot.location or "N/A",
                "date": timezone.localtime(spot.updated_at).strftime("%b %d, %Y") if spot.updated_at else "N/A",
                "participants": 0,
                "tag": "Attraction",
            }
        )

    # Establishment type filter behavior
    if establishment_type == "mice":
        accommodation_reports = []
        attractions_reports = []
    elif establishment_type == "accommodation":
        mice_reports = []
        attractions_reports = []
    elif establishment_type == "attraction":
        mice_reports = []
        accommodation_reports = []

    current_year = timezone.localdate().year
    month_options = [
        (1, "January"),
        (2, "February"),
        (3, "March"),
        (4, "April"),
        (5, "May"),
        (6, "June"),
        (7, "July"),
        (8, "August"),
        (9, "September"),
        (10, "October"),
        (11, "November"),
        (12, "December"),
    ]
    year_options = list(range(current_year, current_year - 10, -1))

    context = {
        "month_options": month_options,
        "year_options": year_options,
        "selected_month": str(month_num) if month_num is not None else "all",
        "selected_year": str(year_num) if year_num is not None else "all",
        "selected_establishment_type": establishment_type if establishment_type in {"all", "mice", "accommodation", "attraction"} else "all",
        "mice_reports": mice_reports,
        "accommodation_reports": accommodation_reports,
        "attractions_reports": attractions_reports,
    }
    return render(request, "survey_results.html", context)


@admin_required
def survey_results_api(request):
    source = _normalize_survey_source(request.GET.get("source", "all"))
    days = _to_positive_int(request.GET.get("days", 30), 30, 3650)
    limit = _to_positive_int(request.GET.get("limit", 100), 100, 1000)

    payload = _build_survey_results_payload(days=days, source=source, limit=limit)
    return JsonResponse(payload)


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
            create_notification(
                recipient_employee=employee,
                title="New tour assignment",
                message=f"You were assigned to {tour_schedule.tour.tour_name} ({tour_schedule.sched_id}).",
                notification_type="assignment",
                url=reverse("tour_app:pending_view"),
                dedupe_key=f"tour-assignment-{tour_schedule.sched_id}-emp-{employee.emp_id}",
                related_object_id=str(tour_schedule.sched_id),
            )
            messages.success(request, f"Successfully assigned {employee.first_name} {employee.last_name} to {tour_schedule.tour.tour_name}.")

            # Log the activity
            try:
                admin_employee = Employee.objects.get(emp_id=request.session.get('employee_id'))
                log_activity(
                    request,
                    admin_employee,
                    'create',
                    description=f'Assigned employee {employee.first_name} {employee.last_name} to tour {tour_schedule.tour.tour_name}'
                )
            except Employee.DoesNotExist:
                pass

        return redirect('admin_app:admin_dashboard')

    # If not POST, redirect to dashboard
    return redirect('admin_app:admin_dashboard')
