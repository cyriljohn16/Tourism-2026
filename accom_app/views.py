import json
import datetime
from django.shortcuts import render, redirect, get_object_or_404
# Import models from admin_app since that's where they're defined
from admin_app.models import Region, Country, Entry, Accomodation
# Import the Answer model from accom_app to store submitted form data
from .models import Summary
from .models import HotelRooms, RoomsGuestAdd
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
import calendar
from django.db.models.functions import ExtractMonth

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
    # Ensure the user is logged in as an accommodation or establishment account.
    if not request.session.get('accom_id'):
        return redirect('admin_app:login')
    
    from admin_app.models import Accomodation, Room
    current_accom_id = request.session.get('accom_id')
    
    try:
        accommodation = Accomodation.objects.get(accom_id=current_accom_id)
    except Accomodation.DoesNotExist:
        return redirect('admin_app:login')
    
    # Only allow access for Hotel accounts.
    if accommodation.company_type.lower() != "hotel":
        return HttpResponseForbidden("You do not have permission to access this page.")
    
    # Get hotel rooms and enhance with availability information
    hotel_rooms = Room.objects.filter(accommodation=accommodation)
    
    # Add current_availability to each room
    for room in hotel_rooms:
        # Try to find corresponding Room model for availability
        try:
            room_model = Room.objects.get(accommodation=accommodation, room_name=room.room_name)
            room.current_availability = room_model.current_availability or room.person_limit
        except Room.DoesNotExist:
            # If no matching Room model exists, use person_limit as fallback
            room.current_availability = room.person_limit
    
    context = {
        'hotel_rooms': hotel_rooms,
    }
    return render(request, 'register_room.html', context)

def add_room_ajax(request):
    """Function to add a new room via AJAX"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Only POST method is allowed'}, status=405)
        
    accom_id = request.session.get('accom_id')
    room_name = request.POST.get('room_name')
    person_limit = request.POST.get('person_limit', 4)  # Default person_limit to 4 if not provided
    
    if not accom_id:
        return JsonResponse({'status': 'error', 'message': 'Not logged in as accommodation or establishment.'}, status=403)
    if not room_name:
        return JsonResponse({'status': 'error', 'message': 'Room name is required.'}, status=400)
    
    # Validate person_limit is a non-negative integer
    try:
        person_limit = int(person_limit)
        if person_limit < 0:
            return JsonResponse({'status': 'error', 'message': 'Person limit cannot be negative.'}, status=400)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Person limit must be a valid number.'}, status=400)
    
    try:
        # Get accommodation object
        from admin_app.models import Accomodation, Room
        accom = Accomodation.objects.get(accom_id=accom_id)
        
        # Check if a room with this name already exists for this accommodation
        if Room.objects.filter(accommodation=accom, room_name=room_name).exists():
            return JsonResponse({
                'status': 'error', 
                'message': f'Room with name "{room_name}" already exists.'
            }, status=400)
        
        # Create Room instance
        new_room = Room.objects.create(
            accommodation=accom, 
            room_name=room_name,
            person_limit=person_limit,
            current_availability=person_limit,
            status='AVAILABLE'
        )
        
        return JsonResponse({
            'status': 'success',
            'room_id': new_room.room_id,
            'room_name': new_room.room_name,
            'person_limit': new_room.person_limit,
            'current_availability': new_room.current_availability,
            'status': new_room.status
        })
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error adding room: {str(e)}\n{error_details}")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@require_POST
def register_guest_to_room(request):
    """Register a guest to a room with check-in and check-out dates"""
    accom_id = request.session.get('accom_id')
    room_id = request.POST.get('room_id')
    guest_first_name = request.POST.get('guest_first_name')
    guest_last_name = request.POST.get('guest_last_name')
    checked_in = request.POST.get('checked_in')
    checked_out = request.POST.get('checked_out')
    num_guests = request.POST.get('num_guests', 1)
    
    # Validate inputs
    if not all([accom_id, room_id, guest_first_name, guest_last_name, checked_in, checked_out]):
        return JsonResponse({'status': 'error', 'message': 'All fields are required.'}, status=400)
    
    try:
        # Get accommodation and room
        accom = Accomodation.objects.get(accom_id=accom_id)
        room = Room.objects.get(room_id=room_id, accommodation=accom)
        
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
        
        # Create or get a guest
        guest, created = Guest.objects.get_or_create(
            first_name=guest_first_name,
            last_name=guest_last_name,
            defaults={
                'status': 'PENDING'
            }
        )
        
        # Create RoomsGuestAdd record for legacy data
        booking = RoomsGuestAdd.objects.create(
            room_id=room,
            accom_id=accom,
            checked_in=checked_in_date,
            checked_out=checked_out_date,
            no_of_nights=no_of_nights,
            month=month,
            num_guests=num_guests
        )
        
        # Create a RoomAssignment record for the new schema
        assignment = RoomAssignment.objects.create(
            room=room,
            guest=guest,
            is_owner=True,
            checked_in=timezone.make_aware(datetime.datetime.combine(checked_in_date, datetime.time.min)),
            checked_out=timezone.make_aware(datetime.datetime.combine(checked_out_date, datetime.time.min)),
        )
        
        # Update room status and availability
        room.status = 'OCCUPIED'
        room.current_availability = max(0, room.current_availability - int(num_guests))
        room.last_check_in = timezone.now()
        room.save()
        
        return JsonResponse({
            'status': 'success',
            'booking_id': booking.id,
            'assignment_id': assignment.assignment_id,
            'guest_name': f"{guest.first_name} {guest.last_name}",
            'checked_in': checked_in,
            'checked_out': checked_out,
            'nights': no_of_nights,
            'guests': num_guests
        })
    except Room.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Room not found or not authorized.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@require_POST
def delete_room_ajax(request):
    """
    AJAX view to delete a hotel room and all associated guest registrations.
    """
    accom_id = request.session.get('accom_id')
    room_id = request.POST.get('room_id')
    
    if not accom_id:
        return JsonResponse({'status': 'error', 'message': 'Not logged in as accommodation or establishment.'}, status=403)
    
    if not room_id:
        return JsonResponse({'status': 'error', 'message': 'Room ID is required.'}, status=400)
    
    try:
        # Get the room and verify it belongs to this accommodation
        from admin_app.models import Accomodation, Room  # Import Room model
        accom = Accomodation.objects.get(accom_id=accom_id)
        hotel_room = Room.objects.get(room_id=room_id, accommodation=accom)
        
        # First delete all guest registrations for this room
        RoomsGuestAdd.objects.filter(room_id=hotel_room).delete()
        
        # Delete any corresponding Room instances with the same name
        Room.objects.filter(accommodation=accom, room_name=hotel_room.room_name).delete()
        
        # Then delete the HotelRooms instance itself
        hotel_room.delete()
        
        return JsonResponse({'status': 'success'})
    except Room.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Room not found or not authorized to delete.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)