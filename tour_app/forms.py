from .models import Tour_Add, Tour_Event, Tour_Schedule, Tour_Admission
from django import forms
from django.core.exceptions import ValidationError
from datetime import timedelta

class TourAddForm(forms.ModelForm):
    class Meta:
        model = Tour_Add
        fields = ('tour_name', 'description', 'image')


class TourScheduleForm(forms.ModelForm):
    tour_id = forms.ModelChoiceField(queryset=Tour_Add.objects.all(), widget=forms.HiddenInput())
    
    # Add duration_days as a read-only field
    duration_days = forms.IntegerField(required=False, disabled=True, 
                                      label="Duration (Days)",
                                      help_text="Calculated automatically based on start and end times.")
    
    slots_available = forms.IntegerField(min_value=1, required=True, label="Slots Available")
    slots_remaining = forms.IntegerField(required=False, disabled=True, label="Slots Remaining")

    class Meta:
        model = Tour_Schedule
        fields = ['start_time', 'end_time', 'duration_days', 'price', 'slots_available']
        widgets = {
            'start_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'end_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        slots_available = cleaned_data.get("slots_available")

        if start_time is None:
            self.add_error("start_time", "Start time is required.")
        if end_time is None:
            self.add_error("end_time", "End time is required.")
        if start_time and end_time and start_time >= end_time:
            self.add_error("end_time", "End time must be after start time.")
        
        # Calculate duration_days
        if start_time and end_time and start_time < end_time:
            delta = end_time - start_time
            # Calculate days, rounding up (if there's any hours/minutes, count as a full day)
            duration_days = delta.days + (1 if delta.seconds > 0 else 0)
            # Update the cleaned_data with the calculated duration
            cleaned_data['duration_days'] = max(1, duration_days)  # At least 1 day

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Set slots information
        slots_available = self.cleaned_data.get('slots_available')
        instance.slots_booked = getattr(instance, 'slots_booked', 0)
        instance.slots_remaining = slots_available - instance.slots_booked
        
        # Set duration days if calculated in clean()
        if 'duration_days' in self.cleaned_data and self.cleaned_data['duration_days']:
            instance.duration_days = self.cleaned_data['duration_days']
        
        if commit:
            instance.save()

        return instance


class TourAdmissionForm(forms.ModelForm):
    class Meta:
        model = Tour_Admission
        fields = ['payables', 'amount']


class TourEventForm(forms.ModelForm):
    day_number = forms.IntegerField(
        min_value=1,
        required=True,
        label="Day of Tour",
        widget=forms.Select(),  # Will populate choices dynamically in the view
        help_text="Select which day of the tour this event belongs to"
    )
    
    class Meta:
        model = Tour_Event
        fields = ('sched_id', 'day_number', 'event_time', 'event_name', 'event_description', 'image')
        widgets = {
            'event_time': forms.TimeInput(attrs={'type': 'time'}),
            'sched_id': forms.HiddenInput(),
        }

