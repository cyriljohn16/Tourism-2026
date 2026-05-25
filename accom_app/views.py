import json
import datetime
import json
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
# Import models from admin_app since that's where they're defined
from admin_app.models import (
    Region,
    Country,
    Entry,
    Accomodation,
    Room as AdminRoom,
    RoomAssignment as AdminRoomAssignment,
)
# Import the Answer model from accom_app to store submitted form data
from .models import Summary
from .models import (
    HotelRooms,
    RoomsGuestAdd,
    Room as AccomRoom,
    RoomAssignment as AccomRoomAssignment,
    AuthoritativeRoomDetails,
)
from guest_app.models import Guest
from guest_app.models import AccommodationBooking
from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings
from django.views.decorators.http import require_POST
import calendar
from django.db.models.functions import ExtractMonth
from django.db import transaction


def _is_accommodation_owner_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False

    role_value = str(getattr(user, "role", "") or "").strip().lower()
    if role_value in {"accommodation_owner", "accommodation owner"}:
        return True

    try:
        return user.groups.filter(name__iexact="accommodation_owner").exists()
    except Exception:
        return False


def _resolve_room_management_accommodation(request):
    """
    Compatibility-aware authorization resolver for owner room management.

    Primary path:
    - authenticated accommodation owner
    - accommodation.owner must match request.user

    Transitional fallback path:
    - allows legacy session-only accommodation accounts only when
      accommodation.owner is NULL (unlinked historical records).
    """
    session_accom_id = request.session.get("accom_id")
    approval_required = "accepted"

    if not getattr(request.user, "is_authenticated", False):
        return None, False, "Authentication required for room management."
    if not _is_accommodation_owner_user(request.user):
        return None, False, "Only accommodation owners can manage rooms."

    owned_qs = Accomodation.objects.filter(owner=request.user)
    accommodation = None
    if session_accom_id:
        accommodation = owned_qs.filter(accom_id=session_accom_id).first()
    if accommodation is None:
        accommodation = owned_qs.order_by("accom_id").first()
    if accommodation is None:
        return None, False, "You do not own any accommodation record."

    if str(accommodation.approval_status or "").lower() != approval_required:
        return None, False, "Your accommodation account is not approved yet."
    return accommodation, False, ""


ROOM_STATUS_SET = {"AVAILABLE", "OCCUPIED", "UNAVAILABLE"}


def _normalize_status(raw_status, *, default="AVAILABLE"):
    status_value = str(raw_status or "").strip().upper()
    if not status_value:
        return default
    if status_value in ROOM_STATUS_SET:
        return status_value
    raise ValueError("Room status must be AVAILABLE, OCCUPIED, or UNAVAILABLE.")


def _parse_positive_int(raw_value, *, field_label, minimum=0):
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_label} must be a valid number.")
    if value < minimum:
        if minimum <= 0:
            raise ValueError(f"{field_label} cannot be negative.")
        raise ValueError(f"{field_label} must be at least {minimum}.")
    return value


def _parse_price(raw_value):
    raw = str(raw_value if raw_value is not None else "").strip()
    if raw == "":
        raise ValueError("Price per night is required.")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("Price per night must be a valid amount.")
    if value <= 0:
        raise ValueError("Price per night must be greater than 0.")
    return value


def _normalize_amenities(raw_value):
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, list):
        raw_list = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                raw_list = parsed
            else:
                raw_list = [text]
        except Exception:
            raw_list = [part.strip() for part in text.split(",")]
    seen = set()
    normalized = []
    for item in raw_list:
        cleaned = str(item or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned[:60])
    return normalized[:20]


def _room_field_payload(request):
    room_name = str(request.POST.get("room_name") or "").strip()
    room_type = str(request.POST.get("room_type") or "").strip()
    status_raw = request.POST.get("availability_status", request.POST.get("status"))
    amenities_raw = request.POST.get("amenities")

    if not room_name:
        raise ValueError("Room name is required.")
    if not room_type:
        raise ValueError("Room type is required.")

    person_limit_raw = request.POST.get("person_limit", request.POST.get("capacity", 0))
    price_raw = request.POST.get("price_per_night", request.POST.get("price", "0"))
    current_availability_raw = request.POST.get("current_availability")
    room_identifiers = str(request.POST.get("room_identifiers") or "").strip()
    total_rooms_raw = request.POST.get("total_rooms")

    person_limit = _parse_positive_int(person_limit_raw, field_label="Capacity", minimum=1)
    price_per_night = _parse_price(price_raw)
    status_value = _normalize_status(status_raw, default="AVAILABLE")
    amenities = _normalize_amenities(amenities_raw)
    room_type_value = room_type

    if current_availability_raw in (None, ""):
        current_availability = person_limit
    else:
        current_availability = _parse_positive_int(
            current_availability_raw,
            field_label="Current availability",
            minimum=0,
        )
        if current_availability > person_limit:
            raise ValueError("Current availability cannot exceed room capacity.")

    if total_rooms_raw in (None, ""):
        total_rooms = 1
    else:
        total_rooms = _parse_positive_int(
            total_rooms_raw,
            field_label="Total rooms",
            minimum=1,
        )

    return {
        "room_name": room_name,
        "room_type": room_type_value,
        "room_identifiers": room_identifiers[:500],
        "total_rooms": total_rooms,
        "person_limit": person_limit,
        "price_per_night": price_per_night,
        "status": status_value,
        "amenities": amenities,
        "current_availability": current_availability,
    }


def _resolve_uploaded_room_image(request):
    room_image = request.FILES.get("room_image")
    return room_image if room_image is not None else None


def _set_room_details(room, *, room_type, amenities):
    details, _created = AuthoritativeRoomDetails.objects.get_or_create(room=room)
    details.room_type = str(room_type or "").strip()[:100]
    details.amenities = json.dumps(amenities or [], ensure_ascii=True)
    details.save(update_fields=["room_type", "amenities", "updated_at"])
    return details


def _serialize_room(room):
    details = getattr(room, "owner_details", None)
    amenities = []
    room_type = room.room_name
    if details is not None:
        room_type = details.room_type or room.room_name
        try:
            parsed = json.loads(details.amenities or "[]")
            if isinstance(parsed, list):
                amenities = [str(item) for item in parsed]
        except Exception:
            amenities = []

    room_image_url = ""
    room_image = getattr(room, "room_image", None)
    if room_image:
        try:
            room_image_url = str(room_image.url or "").strip()
        except Exception:
            room_image_url = ""

    return {
        "id": room.room_id,
        "room_id": room.room_id,
        "name": room.room_name,
        "room_name": room.room_name,
        "room_type": room_type,
        "room_identifiers": str(getattr(room, "room_identifiers", "") or "").strip(),
        "total_rooms": int(getattr(room, "total_rooms", 1) or 1),
        "capacity": int(room.person_limit or 0),
        "person_limit": int(room.person_limit or 0),
        "price_per_night": f"{Decimal(str(room.price_per_night or 0)):.2f}",
        "status": str(room.status or "AVAILABLE").upper(),
        "availability_status": str(room.status or "AVAILABLE").upper(),
        "availability": int(room.current_availability or 0),
        "current_availability": int(room.current_availability or 0),
        "amenities": amenities,
        "room_image_url": room_image_url,
    }

def other_estab_create(request):
    """
    A view that handles the creation of Summary records.
    For accommodation accounts with company_type equal to "Hotel",
    the summary record will store '1' in the hotel field.
    """
    message = ""

    # Ensure that a logged in accommodation or establishment account exists.
    if request.session.get('user_type') not in ['accomodation', 'establishment'] or not request.session.get('accom_id'):
        from django.contrib import messages
        messages.error(request, "You must be logged in as an accommodation or establishment account to submit the form.")
        return redirect('admin_app:login')
    
    try:
        accommodation = Accomodation.objects.get(accom_id=request.session.get('accom_id'))
    except Accomodation.DoesNotExist:
        from django.contrib import messages
        messages.error(request, "Accommodation account not found.")
        return redirect('admin_app:login')
    
    # Determine if the account is a hotel (using your convention where a hotel is indicated by 1)
    is_hotel_account = (accommodation.company_type.lower() == "hotel")

    # Ensure the accommodation user is logged in
    accom_id = request.session.get('accom_id')
    if not accom_id:
        return redirect('admin_app:login')
    
    # Determine the selected month:
    if request.method == "POST":
        selected_month = request.POST.get("month", "January")
        # Process form submission here if needed (e.g. save new Other_Estab record)
        # For simplicity, we only re-render the page.
    else:
        selected_month = request.GET.get("filter_month", "January")
    
    # Convert selected month (e.g., "January") to month number (e.g., 1)
    try:
        month_number = list(calendar.month_name).index(selected_month)
    except ValueError:
        month_number = 1

    # Query Summary records for this accommodation and the selected month:
    summary_data = Summary.objects.filter(accom_id=accom_id).annotate(
        month=ExtractMonth('month_submitted')
    ).filter(month=month_number)
    
    # Query RoomGuestAdd records for this accommodation and selected month.
    roomguestadds = RoomsGuestAdd.objects.filter(accom_id=accom_id, month=selected_month)
    
    # Aggregate the required values:
    total_guest_night = 0          # Sum of (no_of_nights * num_guests)
    total_checkin = 0              # Total guest check-in (sum of num_guests for all records)
    number_stayed_overnight = 0    # Sum of num_guests for records where no_of_nights is > 0
    occupied_room_ids = set()      # To count distinct rooms occupied by guests
    
    for record in roomguestadds:
        # Compute total guest nights for this record.
        guest_nights = (record.no_of_nights or 0) * (record.num_guests or 0)
        total_guest_night += guest_nights
        
        # Total check-in is sum of num_guests (each record indicates a check-in).
        total_checkin += record.num_guests or 0
        
        # If no_of_nights is provided (> 0), count these guests as having stayed overnight.
        if record.no_of_nights and record.no_of_nights > 0:
            number_stayed_overnight += record.num_guests or 0
        
        # If there are guests (num_guests > 0), add the room to the set
        if record.num_guests and record.num_guests > 0:
            occupied_room_ids.add(record.room_id.room_id)
    total_rooms = len(occupied_room_ids)
    
    # NEW: Compute the total number of nights from all RoomGuestAdd records.
    total_nights = sum(record.no_of_nights or 0 for record in roomguestadds)
    
    # --- Aggregate Accom App Summary Data ---
    total_overall_total = sum(s.overall_total or 0 for s in summary_data)
    total_guest_num_summary = sum(s.guest_num or 0 for s in summary_data)
    total_sub_total = sum(s.sub_total or 0 for s in summary_data)
    summary_count = summary_data.count()  # total number of summary records
    
    # --- New: Aggregate RoomsGuestAdd data grouped by room ---
    aggregated_by_room = {}
    for record in roomguestadds:
        room = record.room_id  # This is a HotelRooms instance
        room_key = room.room_id  # Use the room_id as the grouping key
        if room_key not in aggregated_by_room:
            aggregated_by_room[room_key] = {
                'room_id': room.room_id,
                'room_name': room.room_name,
                'total_bookings': 0,
                'total_nights': 0,
                'total_guests': 0,
            }
        aggregated_by_room[room_key]['total_bookings'] += 1
        aggregated_by_room[room_key]['total_nights'] += record.no_of_nights or 0
        aggregated_by_room[room_key]['total_guests'] += record.num_guests or 0

    aggregated_rooms = list(aggregated_by_room.values())
    
    if request.method == 'POST':
        try:
            # Convert month name to a date (first day of the month)
            month_name = request.POST.get('month')
            current_year = datetime.date.today().year
            date_str = f"{month_name} 1, {current_year}"
            month_submitted = datetime.datetime.strptime(date_str, '%B %d, %Y').date()
            month_actual = datetime.date.today()

            # Process country entries
            for key, value in request.POST.items():
                if key.startswith('country_') and value and int(value) > 0:
                    country_id = int(key.replace('country_', ''))
                    guest_num = int(value)
                    
                    Summary.objects.create(
                        accom_id=accommodation,
                        country_id_id=country_id,
                        guest_num=guest_num,
                        month_submitted=month_submitted,
                        month_actual=month_actual,
                        hotel="1" if is_hotel_account else None
                    )

            # Process region subtotals
            for key, value in request.POST.items():
                if key.startswith('subtotal_'):
                    region_id = int(key.replace('subtotal_', ''))
                    sub_total = int(value)
                    
                    Summary.objects.create(
                        accom_id=accommodation,
                        region_id_id=region_id,
                        sub_total=sub_total,
                        month_submitted=month_submitted,
                        month_actual=month_actual,
                        hotel="1" if is_hotel_account else None
                    )
                    
            # Process overall total.
            overall_total = int(request.POST.get('overall_total', 0))
            Summary.objects.create(
                accom_id=accommodation,
                overall_total=overall_total,
                month_submitted=month_submitted,
                month_actual=month_actual,
                hotel="1" if is_hotel_account else None
            )

            # Process entries.
            for key, value in request.POST.items():
                if key.startswith('entry_') and value:
                    entry_id = int(key.replace('entry_', ''))
                    Summary.objects.create(
                        accom_id=accommodation,
                        entry_id_id=entry_id,
                        entry_ans=value,
                        month_submitted=month_submitted,
                        month_actual=month_actual,
                        hotel="1" if is_hotel_account else None
                    )

            message = "Your answers have been saved successfully!"

            regions = Region.objects.all()
            countries = Country.objects.all()
            entries = Entry.objects.all()
            context = {
                'regions': regions,
                'countries': countries,
                'entries': entries,
                'message': message,
                'accommodation': accommodation,
                'selected_month': selected_month,
                'filter_month': selected_month,
                'summary_data': summary_data,
                'roomguestadds': roomguestadds,
                'total_guest_night': total_guest_night,
                'total_checkin': total_checkin,
                'number_stayed_overnight': number_stayed_overnight,
                'total_rooms': total_rooms,
                'total_nights': total_nights,
                'total_overall_total': total_overall_total,
                'total_guest_num_summary': total_guest_num_summary,
                'total_sub_total': total_sub_total,
                'summary_count': summary_count,
                'aggregated_rooms': aggregated_rooms,  # New variable for template use
            }

            if is_hotel_account:
                return render(request, 'other_estab_form_pt2.html', context)
            else:
                return render(request, 'other_estab_form.html', context)

        except Exception as e:
            message = f"Error saving data: {str(e)}"

    regions = Region.objects.all()
    countries = Country.objects.all()
    entries = Entry.objects.all()
    
    context = {
        'regions': regions,
        'countries': countries,
        'entries': entries,
        'message': message,
        'accommodation': accommodation,
        'selected_month': selected_month,
        'filter_month': selected_month,
        'summary_data': summary_data,
        'roomguestadds': roomguestadds,
        'total_guest_night': total_guest_night,
        'total_checkin': total_checkin,
        'number_stayed_overnight': number_stayed_overnight,
        'total_rooms': total_rooms,
        'total_nights': total_nights,
        'total_overall_total': total_overall_total,
        'total_guest_num_summary': total_guest_num_summary,
        'total_sub_total': total_sub_total,
        'summary_count': summary_count,
        'aggregated_rooms': aggregated_rooms,  # New variable for template use
    }
    return render(request, 'other_estab_form.html', context)

def submit_answers(request):
    """
    Process the submitted answerable form. Extract and process the submitted data.
    """
    if request.method == 'POST':
        submitted_data = request.POST.dict()
        print("Submitted Answer Data:", submitted_data)
        # TODO: Add processing logic here (e.g. store answers in a database)

        # Redirect after processing; adjust as necessary
        return redirect('accom_app:other_estab_create')
    else:
        return redirect('accom_app:other_estab_create')


def other_estab_create_pt2(request):
    return render(request, 'other_estab_form_pt2.html')

def register_room(request):
    accommodation, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accommodation is None:
        return HttpResponseForbidden(auth_error or "Unauthorized.")

    hotel_rooms = (
        AdminRoom.objects.select_related("owner_details")
        .filter(accommodation=accommodation)
        .order_by("room_name")
    )

    context = {
        'hotel_rooms': hotel_rooms,
        'rooms_payload': [_serialize_room(room) for room in hotel_rooms],
    }
    return render(request, 'register_room.html', context)

def add_room_ajax(request):
    """Function to add a new room via AJAX"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Only POST method is allowed'}, status=405)

    accom, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accom is None:
        return JsonResponse({'status': 'error', 'message': auth_error}, status=403)

    try:
        payload = _room_field_payload(request)
        if not payload["room_name"]:
            return JsonResponse(
                {'status': 'error', 'message': 'Room name or room type is required.'},
                status=400,
            )

        # Check if a room with this name already exists for this accommodation
        if AdminRoom.objects.filter(accommodation=accom, room_name=payload["room_name"]).exists():
            return JsonResponse({
                'status': 'error', 
                'message': f'Room with name "{payload["room_name"]}" already exists.'
            }, status=400)

        # Authoritative write path: admin_app.Room only.
        room_image = _resolve_uploaded_room_image(request)
        new_room = AdminRoom.objects.create(
            accommodation=accom,
            room_name=payload["room_name"],
            room_identifiers=payload["room_identifiers"],
            total_rooms=payload["total_rooms"],
            person_limit=payload["person_limit"],
            current_availability=payload["current_availability"],
            price_per_night=payload["price_per_night"],
            status=payload["status"],
            room_image=room_image,
        )
        _set_room_details(
            new_room,
            room_type=payload["room_type"],
            amenities=payload["amenities"],
        )
        new_room = AdminRoom.objects.select_related("owner_details").get(pk=new_room.pk)

        return JsonResponse({
            'status': 'success',
            'room': _serialize_room(new_room),
        })
    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error adding room: {str(e)}\n{error_details}")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def get_rooms_json(request):
    accom, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accom is None:
        return JsonResponse({'status': 'error', 'message': auth_error}, status=403)

    rooms = (
        AdminRoom.objects.select_related("owner_details")
        .filter(accommodation=accom)
        .order_by("room_name")
    )
    return JsonResponse({
        "status": "success",
        "rooms": [_serialize_room(room) for room in rooms],
    })


@require_POST
def update_room_ajax(request):
    accom, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accom is None:
        return JsonResponse({'status': 'error', 'message': auth_error}, status=403)

    room_id_raw = request.POST.get("room_id")
    if room_id_raw in (None, ""):
        return JsonResponse({'status': 'error', 'message': 'Room ID is required.'}, status=400)

    try:
        room_id = int(str(room_id_raw).strip())
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid room ID.'}, status=400)

    try:
        payload = _room_field_payload(request)
        if not payload["room_name"]:
            return JsonResponse(
                {'status': 'error', 'message': 'Room name or room type is required.'},
                status=400,
            )

        with transaction.atomic():
            room = AdminRoom.objects.select_for_update().filter(
                room_id=room_id,
                accommodation=accom,
            ).first()
            if room is None:
                return JsonResponse(
                    {'status': 'error', 'message': 'Room not found or not authorized.'},
                    status=404,
                )

            duplicate_exists = AdminRoom.objects.filter(
                accommodation=accom,
                room_name=payload["room_name"],
            ).exclude(room_id=room.room_id).exists()
            if duplicate_exists:
                return JsonResponse(
                    {'status': 'error', 'message': f'Room with name "{payload["room_name"]}" already exists.'},
                    status=400,
                )

            room.room_name = payload["room_name"]
            room.room_identifiers = payload["room_identifiers"]
            room.total_rooms = payload["total_rooms"]
            room.person_limit = payload["person_limit"]
            room.price_per_night = payload["price_per_night"]
            room.status = payload["status"]
            remove_room_image = str(request.POST.get("remove_room_image") or "").strip().lower() in {"1", "true", "yes", "on"}
            uploaded_room_image = _resolve_uploaded_room_image(request)
            if remove_room_image:
                room.room_image = None
            if uploaded_room_image is not None:
                room.room_image = uploaded_room_image

            desired_availability = min(payload["current_availability"], payload["person_limit"])
            room.current_availability = desired_availability
            room.save(update_fields=[
                "room_name",
                "room_identifiers",
                "total_rooms",
                "person_limit",
                "price_per_night",
                "status",
                "current_availability",
                "room_image",
                "updated_at",
            ])

            _set_room_details(
                room,
                room_type=payload["room_type"],
                amenities=payload["amenities"],
            )

        room = AdminRoom.objects.select_related("owner_details").get(room_id=room_id)
        return JsonResponse({"status": "success", "room": _serialize_room(room)})
    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@require_POST
def register_guest_to_room(request):
    """Register a guest to a room with check-in and check-out dates"""
    accom, legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accom is None:
        return JsonResponse({'status': 'error', 'message': auth_error}, status=403)

    if not bool(getattr(settings, "ALLOW_LEGACY_OWNER_ROOM_ASSIGNMENT", False)):
        return JsonResponse(
            {
                "status": "error",
                "message": (
                    "Direct owner guest-to-room assignment is disabled in the revised booking architecture. "
                    "Use the guest reservation flow so booking integrity, overlap checks, billing, and companions stay consistent."
                ),
            },
            status=409,
        )

    room_id = request.POST.get('room_id')
    guest_first_name = request.POST.get('guest_first_name')
    guest_last_name = request.POST.get('guest_last_name')
    checked_in = request.POST.get('checked_in')
    checked_out = request.POST.get('checked_out')
    num_guests = request.POST.get('num_guests', 1)
    
    # Validate inputs
    if not all([room_id, guest_first_name, guest_last_name, checked_in, checked_out]):
        return JsonResponse({'status': 'error', 'message': 'All fields are required.'}, status=400)
    
    try:
        try:
            num_guests_int = int(num_guests)
            if num_guests_int <= 0:
                return JsonResponse({'status': 'error', 'message': 'Guest count must be at least 1.'}, status=400)
        except (TypeError, ValueError):
            return JsonResponse({'status': 'error', 'message': 'Invalid guest count.'}, status=400)

        admin_room = AdminRoom.objects.get(room_id=room_id, accommodation=accom)
        
        # Convert dates to datetime objects
        checked_in_date = datetime.datetime.strptime(checked_in, '%Y-%m-%d').date()
        checked_out_date = datetime.datetime.strptime(checked_out, '%Y-%m-%d').date()
        
        # Calculate number of nights
        delta = checked_out_date - checked_in_date
        no_of_nights = delta.days
        
        if no_of_nights < 1:
            return JsonResponse({'status': 'error', 'message': 'Check-out date must be after check-in date.'}, status=400)
        
        # Determine the month (for reporting)
        month = checked_in_date.strftime('%B')
        
        # Create or reuse a lightweight walk-in guest profile for room assignment tracking.
        guest = Guest.objects.filter(
            first_name__iexact=guest_first_name,
            last_name__iexact=guest_last_name,
            email__iendswith='@walkin.local',
        ).first()

        if guest is None:
            safe_first = ''.join(ch.lower() for ch in guest_first_name if ch.isalnum()) or "guest"
            safe_last = ''.join(ch.lower() for ch in guest_last_name if ch.isalnum()) or "walkin"
            unique_token = timezone.now().strftime("%Y%m%d%H%M%S%f")
            base_username = f"{safe_first}.{safe_last}"
            username = f"{base_username}.{unique_token}"[:100]
            email = f"{base_username}.{unique_token}@walkin.local"
            phone_seed = ''.join(ch for ch in unique_token if ch.isdigit())[-11:]
            phone_number = phone_seed if phone_seed else "00000000000"

            guest = Guest.objects.create(
                first_name=guest_first_name,
                last_name=guest_last_name,
                username=username,
                email=email,
                country_of_origin="Walk-in",
                phone_number=phone_number,
                sex='M',
                password='walkin-temporary-password',
            )
        
        # Authoritative assignment write path.
        assignment = AdminRoomAssignment.objects.create(
            room=admin_room,
            guest=guest,
            is_owner=True,
            checked_in=timezone.make_aware(datetime.datetime.combine(checked_in_date, datetime.time.min)),
            checked_out=timezone.make_aware(datetime.datetime.combine(checked_out_date, datetime.time.min)),
        )

        booking_id = None
        # Transitional legacy persistence only for pre-existing mapped legacy rooms.
        # This avoids creating new parallel-room records.
        if legacy_mode:
            legacy_room = AccomRoom.objects.filter(
                accom_id=accom,
                room_name=admin_room.room_name,
            ).first()
            if legacy_room is not None:
                legacy_booking = RoomsGuestAdd.objects.create(
                    room_id=legacy_room,
                    accom_id=accom,
                    checked_in=checked_in_date,
                    checked_out=checked_out_date,
                    no_of_nights=no_of_nights,
                    month=month,
                    num_guests=num_guests_int,
                )
                AccomRoomAssignment.objects.create(
                    room=legacy_room,
                    guest=guest,
                    is_owner=True,
                    checked_in=timezone.make_aware(datetime.datetime.combine(checked_in_date, datetime.time.min)),
                    checked_out=timezone.make_aware(datetime.datetime.combine(checked_out_date, datetime.time.min)),
                )
                booking_id = legacy_booking.id
        
        return JsonResponse({
            'status': 'success',
            'booking_id': booking_id,
            'assignment_id': assignment.assignment_id,
            'guest_name': f"{guest.first_name} {guest.last_name}",
            'checked_in': checked_in,
            'checked_out': checked_out,
            'nights': no_of_nights,
            'guests': num_guests_int
        })
    except AdminRoom.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Room not found or not authorized.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@require_POST
def delete_room_ajax(request):
    """
    AJAX view to delete a hotel room and all associated guest registrations.
    """
    accom, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    room_id = request.POST.get('room_id')

    if accom is None:
        return JsonResponse({'status': 'error', 'message': auth_error}, status=403)
    
    if not room_id:
        return JsonResponse({'status': 'error', 'message': 'Room ID is required.'}, status=400)

    try:
        with transaction.atomic():
            hotel_room = AdminRoom.objects.select_for_update().get(room_id=room_id, accommodation=accom)

            # Transitional cleanup for legacy rows that mirror this authoritative room.
            legacy_rooms = AccomRoom.objects.filter(accom_id=accom, room_name=hotel_room.room_name)
            if legacy_rooms.exists():
                RoomsGuestAdd.objects.filter(room_id__in=legacy_rooms).delete()
                AccomRoomAssignment.objects.filter(room__in=legacy_rooms).delete()
                legacy_rooms.delete()

            hotel_room.delete()

        return JsonResponse({'status': 'success'})
    except AdminRoom.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Room not found or not authorized to delete.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def _classify_guest_nationality(country_text):
    value = str(country_text or "").strip().lower()
    if not value:
        return "Unknown"
    local_tokens = {
        "philippines",
        "philippine",
        "ph",
        "filipino",
        "pilipinas",
    }
    if value in local_tokens or "philipp" in value:
        return "Philippine Residents (Filipino)"
    return "Foreign Residents"


def owner_reports_analytics(request):
    accommodation, _legacy_mode, auth_error = _resolve_room_management_accommodation(request)
    if accommodation is None:
        return HttpResponseForbidden(auth_error or "Unauthorized.")

    today = timezone.localdate()
    month_raw = request.GET.get("month")
    year_raw = request.GET.get("year")
    trend_window_raw = str(request.GET.get("trend_window", "6") or "6").strip()
    trend_metric_raw = str(request.GET.get("trend_metric", "all") or "all").strip().lower()
    has_month_filter = str(month_raw or "").strip() != ""
    has_year_filter = str(year_raw or "").strip() != ""

    default_month = today.month
    default_year = today.year
    if not has_month_filter and not has_year_filter:
        latest_confirmed = (
            AccommodationBooking.objects.filter(
                accommodation=accommodation,
                status="confirmed",
            )
            .order_by("-check_in")
            .values_list("check_in", flat=True)
            .first()
        )
        if latest_confirmed:
            default_month = latest_confirmed.month
            default_year = latest_confirmed.year

    try:
        selected_month = int(month_raw) if has_month_filter else default_month
    except (TypeError, ValueError):
        selected_month = default_month
    if selected_month < 1 or selected_month > 12:
        selected_month = default_month

    try:
        selected_year = int(year_raw) if has_year_filter else default_year
    except (TypeError, ValueError):
        selected_year = default_year
    if selected_year < 2000 or selected_year > 2100:
        selected_year = default_year

    allowed_trend_windows = {"6": 6, "12": 12, "24": 24}
    selected_trend_window = trend_window_raw if trend_window_raw in allowed_trend_windows else "6"
    trend_window_months = allowed_trend_windows[selected_trend_window]

    allowed_trend_metrics = {"guests", "nights", "rooms", "all"}
    selected_trend_metric = trend_metric_raw if trend_metric_raw in allowed_trend_metrics else "all"

    first_day = datetime.date(selected_year, selected_month, 1)
    _, last_day_num = calendar.monthrange(selected_year, selected_month)
    last_day = datetime.date(selected_year, selected_month, last_day_num)

    room_count = AdminRoom.objects.filter(accommodation=accommodation).count()

    period_bookings = (
        AccommodationBooking.objects.select_related("guest", "room")
        .filter(
            accommodation=accommodation,
            status="confirmed",
            check_in__lte=last_day,
            check_out__gte=first_day,
        )
        .order_by("check_in")
    )
    period_bookings_list = list(period_bookings)

    total_checkins = len(period_bookings_list)
    total_guest_nights = sum((booking.nights() or 0) * (booking.num_guests or 0) for booking in period_bookings_list)
    occupied_room_ids = {booking.room_id for booking in period_bookings_list if booking.room_id}
    rooms_occupied = len(occupied_room_ids)
    occupancy_rate = (rooms_occupied / room_count * 100.0) if room_count > 0 else 0.0

    # Trend data for selectable time window.
    def _shift_month(year_value, month_value, diff):
        absolute = (year_value * 12 + (month_value - 1)) + diff
        return absolute // 12, (absolute % 12) + 1

    trend_labels = []
    trend_checkins = []
    trend_guest_nights = []
    trend_rooms_occupied = []
    for offset in range(-(trend_window_months - 1), 1):
        y, m = _shift_month(selected_year, selected_month, offset)
        month_cursor = datetime.date(y, m, 1)
        _, end_day_num = calendar.monthrange(y, m)
        month_end = datetime.date(y, m, end_day_num)

        month_bookings = AccommodationBooking.objects.filter(
            accommodation=accommodation,
            status="confirmed",
            check_in__lte=month_end,
            check_out__gte=month_cursor,
        )
        month_list = list(month_bookings)
        trend_labels.append(month_cursor.strftime("%b %Y"))
        trend_checkins.append(len(month_list))
        trend_guest_nights.append(sum((b.nights() or 0) * (b.num_guests or 0) for b in month_list))
        trend_rooms_occupied.append(len({b.room_id for b in month_list if b.room_id}))
    trend_rows = list(zip(trend_labels, trend_checkins, trend_guest_nights, trend_rooms_occupied))
    if selected_trend_metric == "guests":
        trend_max = max(trend_checkins + [1])
    elif selected_trend_metric == "nights":
        trend_max = max(trend_guest_nights + [1])
    elif selected_trend_metric == "rooms":
        trend_max = max(trend_rooms_occupied + [1])
    else:
        trend_max = max(trend_checkins + trend_guest_nights + trend_rooms_occupied + [1])

    # Nationality breakdown (selected period).
    nationality_counts = {
        "Philippine Residents (Filipino)": 0,
        "Philippine Residents (Foreign)": 0,
        "Foreign Residents": 0,
        "Unknown": 0,
    }
    for booking in period_bookings_list:
        category = _classify_guest_nationality(getattr(booking.guest, "country_of_origin", ""))
        nationality_counts[category] = nationality_counts.get(category, 0) + 1

    # Keep the middle bucket visible for parity with original table.
    if nationality_counts["Philippine Residents (Filipino)"] > 0:
        nationality_counts["Philippine Residents (Foreign)"] = 0

    nationality_total = sum(nationality_counts.values())
    percentage_denominator = nationality_total if nationality_total > 0 else 1
    nationality_labels = list(nationality_counts.keys())
    nationality_values = [nationality_counts[k] for k in nationality_labels]
    nationality_percentages = [round((v / percentage_denominator) * 100.0, 2) for v in nationality_values]
    pie_colors = ["#3b82f6", "#22c55e", "#f59e0b", "#94a3b8"]
    if nationality_total > 0:
        pie_parts = []
        running = 0.0
        for idx, pct in enumerate(nationality_percentages):
            next_value = running + pct
            pie_parts.append(f"{pie_colors[idx % len(pie_colors)]} {running:.2f}% {next_value:.2f}%")
            running = next_value
        nationality_pie_style = "conic-gradient(" + ", ".join(pie_parts) + ")"
    else:
        nationality_pie_style = "conic-gradient(#cbd5e1 0% 100%)"

    # Daily room occupancy for selected month.
    days_in_month = list(range(1, last_day_num + 1))
    daily_room_occupancy = []
    for day in days_in_month:
        day_date = datetime.date(selected_year, selected_month, day)
        day_rooms = set()
        for booking in period_bookings_list:
            if booking.room_id and booking.check_in <= day_date < booking.check_out:
                day_rooms.add(booking.room_id)
        daily_room_occupancy.append(len(day_rooms))
    daily_rows = list(zip(days_in_month, daily_room_occupancy))
    daily_max = max(daily_room_occupancy + [1])
    chart_y_max = max(room_count, daily_max, 2)
    guide_values = [chart_y_max * step / 5 for step in range(5, -1, -1)]
    daily_guides = [
        {
            "label": f"{value:.1f}".rstrip("0").rstrip("."),
            "percent": (value / chart_y_max) * 100 if chart_y_max else 0,
        }
        for value in guide_values
    ]

    context = {
        "accommodation": accommodation,
        "selected_month": selected_month,
        "selected_year": selected_year,
        "month_options": [(i, calendar.month_name[i]) for i in range(1, 13)],
        "year_options": list(range(today.year - 2, today.year + 2)),
        "first_day": first_day,
        "last_day": last_day,
        "total_checkins": total_checkins,
        "total_guest_nights": total_guest_nights,
        "rooms_occupied": rooms_occupied,
        "room_count": room_count,
        "occupancy_rate": round(occupancy_rate, 2),
        "trend_labels_json": json.dumps(trend_labels),
        "trend_checkins_json": json.dumps(trend_checkins),
        "trend_guest_nights_json": json.dumps(trend_guest_nights),
        "trend_rooms_occupied_json": json.dumps(trend_rooms_occupied),
        "trend_rows": trend_rows,
        "trend_max": trend_max,
        "selected_trend_window": selected_trend_window,
        "selected_trend_metric": selected_trend_metric,
        "nationality_labels_json": json.dumps(nationality_labels),
        "nationality_values_json": json.dumps(nationality_values),
        "nationality_rows": list(zip(nationality_labels, nationality_values, nationality_percentages)),
        "nationality_pie_style": nationality_pie_style,
        "nationality_total": nationality_total,
        "daily_days_json": json.dumps(days_in_month),
        "daily_occupancy_json": json.dumps(daily_room_occupancy),
        "daily_rows": daily_rows,
        "daily_max": daily_max,
        "chart_y_max": chart_y_max,
        "daily_guides": daily_guides,
    }
    return render(request, "owner_reports_analytics.html", context)
