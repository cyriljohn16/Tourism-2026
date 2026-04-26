# admin_app/forms.py

import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Employee
from .models import Accomodation, AccommodationCertification, TourismInformation


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True
class EmployeeRegistrationForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput(), label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput(), label="Confirm Password")

    class Meta:
        model = Employee
        fields = [
            'first_name',
            'last_name',
            'middle_name',
            'username',
            'age',
            'phone_number',
            'email',
            'sex',
            'profile_picture',
        ]

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Passwords do not match.')
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        # if password1 is valid, hash it before saving
        password = self.cleaned_data.get('password1')
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user



class AccommodationRegistrationForm(forms.ModelForm):
    THESIS_SAFE_COMPANY_TYPES = (
        ("Hotel", "Hotel"),
        ("Inn", "Inn"),
    )
    _LOCATION_ALIAS_MAP = {
        "city proper": "Bayawan City Proper",
        "bayawan city proper": "Bayawan City Proper",
        "poblacion": "Poblacion, Bayawan City",
        "bayawan": "Bayawan City",
        "bayawan city": "Bayawan City",
        "terminal area": "Terminal Area, Bayawan City",
        "tinago": "Tinago, Bayawan City",
        "ubos": "Ubos, Bayawan City",
        "villareal": "Villareal, Bayawan City",
        "villarreal": "Villareal, Bayawan City",
        "suba": "Suba, Bayawan City",
        "boyco": "Boyco, Bayawan City",
    }
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(),
        label="Confirm Accommodation Account Password",
    )
    certifications = forms.FileField(
        required=False,
        widget=MultipleFileInput(),
        help_text="Upload one or more business permits/certifications.",
    )

    def __init__(self, *args, **kwargs):
        self.owner = kwargs.pop("owner", None)
        super().__init__(*args, **kwargs)
        self.fields["company_type"].choices = self.THESIS_SAFE_COMPANY_TYPES
        self.fields["company_type"].help_text = "Thesis-safe scope: Hotel or Inn only."
        self.fields["location"].help_text = (
            "Use Bayawan City locations. Broad labels are normalized "
            "(e.g., poblacion -> Poblacion, Bayawan City)."
        )
        self.fields["description"].help_text = "Required for listing context and thesis documentation."
        self.fields["accommodation_amenities"].help_text = (
            "Required comma-separated amenities used for recommendation context."
        )
        self.fields["official_booking_url"].help_text = (
            "Optional: official external booking page for this property."
        )
        self.fields["official_contact_url"].help_text = (
            "Optional: official contact/inquiry page for this property."
        )

    class Meta:
        model = Accomodation
        fields = [
            "company_name",
            "company_type",
            "location",
            "phone_number",
            "email_address",
            "description",
            "accommodation_amenities",
            "official_booking_url",
            "official_contact_url",
            "password",
            "profile_picture",
        ]
        labels = {
            "company_name": "Business Name",
            "company_type": "Business Type",
            "location": "Address",
            "phone_number": "Contact Number",
            "email_address": "Contact Email",
            "description": "Business Description",
            "accommodation_amenities": "Accommodation Amenities",
            "official_booking_url": "Official Booking Page URL",
            "official_contact_url": "Official Contact/Inquiry URL",
            "password": "Accommodation Account Password",
        }
        widgets = {
            "company_type": forms.Select(
                choices=[("Hotel", "Hotel"), ("Inn", "Inn")]
            ),
            "location": forms.TextInput(
                attrs={
                    "placeholder": "e.g., Poblacion, Bayawan City",
                    "list": "canonical-bayawan-locations",
                }
            ),
            "description": forms.Textarea(attrs={"rows": 4}),
            "accommodation_amenities": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "e.g., WiFi, Parking, 24/7 Front Desk, Restaurant",
                }
            ),
            "password": forms.PasswordInput(),
        }

    def clean_company_name(self):
        company_name = str(self.cleaned_data.get("company_name") or "").strip()
        if not company_name:
            raise forms.ValidationError("Business name is required.")
        qs = Accomodation.objects.filter(company_name__iexact=company_name)
        if self.owner is not None:
            qs = qs.filter(owner=self.owner).exclude(approval_status="declined")
        if qs.exists():
            raise forms.ValidationError(
                "You already submitted this business name. Please use a different name."
            )
        return company_name

    def clean_email_address(self):
        email_address = str(self.cleaned_data.get("email_address") or "").strip().lower()
        qs = Accomodation.objects.filter(email_address__iexact=email_address).exclude(
            approval_status="declined"
        )
        if self.owner is not None:
            qs = qs.exclude(owner=self.owner)
        if qs.exists():
            raise forms.ValidationError(
                "This accommodation email is already used in an active or pending registration."
            )
        return email_address

    def clean_phone_number(self):
        phone = str(self.cleaned_data.get("phone_number") or "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 7:
            raise forms.ValidationError("Enter a valid contact number.")
        return phone

    def clean_company_type(self):
        company_type = str(self.cleaned_data.get("company_type") or "").strip().lower()
        allowed = {value.lower() for value, _ in self.THESIS_SAFE_COMPANY_TYPES}
        if company_type not in allowed:
            raise forms.ValidationError("Company type must be Hotel or Inn.")
        return company_type.title()

    def clean_location(self):
        raw_location = " ".join(str(self.cleaned_data.get("location") or "").split()).strip()
        if not raw_location:
            raise forms.ValidationError("Location is required.")
        lowered = raw_location.lower()
        normalized = self._LOCATION_ALIAS_MAP.get(lowered, raw_location)
        if "bayawan" not in normalized.lower():
            raise forms.ValidationError("Location must be within Bayawan City scope.")
        return normalized[:300]

    def clean_description(self):
        description = str(self.cleaned_data.get("description") or "").strip()
        if not description:
            raise forms.ValidationError("Description is required.")
        return description

    def clean_accommodation_amenities(self):
        raw = str(self.cleaned_data.get("accommodation_amenities") or "").strip()
        if not raw:
            raise forms.ValidationError("Accommodation amenities are required.")
        items = []
        seen = set()
        for token in re.split(r"[,\n]", raw):
            cleaned = " ".join(str(token or "").split()).strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(cleaned[:60])
        if not items:
            raise forms.ValidationError("Provide at least one amenity.")
        return ", ".join(items[:20])

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")
        if password and password_confirm and password != password_confirm:
            self.add_error("password_confirm", "Passwords do not match.")
        if password:
            try:
                validate_password(password)
            except ValidationError as exc:
                self.add_error("password", exc)
        if self.owner is not None:
            if not bool(getattr(self.owner, "is_active", False)):
                raise forms.ValidationError("Owner account must be active.")
            if not self.owner.groups.filter(name__iexact="accommodation_owner").exists():
                raise forms.ValidationError("Owner must belong to the accommodation_owner group.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.owner is not None:
            instance.owner = self.owner
        instance.approval_status = "pending"
        instance.status = "pending"
        instance.reviewed_at = None
        instance.reviewed_by = None
        instance.rejection_reason = ""
        if commit:
            instance.save()

            certification_files = self.files.getlist("certifications")
            for cert_file in certification_files:
                AccommodationCertification.objects.create(
                    accommodation=instance,
                    image=cert_file,
                )

        return instance


class AdminAccommodationEncodeForm(AccommodationRegistrationForm):
    owner_user = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        label="Link to Owner Account (Optional)",
        help_text="Leave blank for admin-encoded demo records not tied to an owner login.",
    )
    approval_status = forms.ChoiceField(
        choices=Accomodation.APPROVAL_STATUS_CHOICES,
        initial="accepted",
        label="Initial Approval Status",
    )
    rejection_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Decline Reason (Optional)",
    )

    def __init__(self, *args, **kwargs):
        self.reviewer = kwargs.pop("reviewer", None)
        super().__init__(*args, **kwargs)
        self.fields["owner_user"].queryset = get_user_model().objects.all().order_by("username")
        self.fields["approval_status"].help_text = "Use Accepted for quick demo-ready accommodations."

    def clean(self):
        cleaned_data = super().clean()
        approval_status = str(cleaned_data.get("approval_status") or "pending").strip().lower()
        rejection_reason = str(cleaned_data.get("rejection_reason") or "").strip()
        selected_owner = cleaned_data.get("owner_user")
        company_type = str(cleaned_data.get("company_type") or "").strip().lower()

        if company_type in {"hotel", "inn"}:
            if selected_owner is None:
                self.add_error(
                    "owner_user",
                    "Hotel/Inn records must be linked to an active accommodation owner.",
                )
            else:
                if not bool(getattr(selected_owner, "is_active", False)):
                    self.add_error("owner_user", "Selected owner account is inactive.")
                if not selected_owner.groups.filter(name__iexact="accommodation_owner").exists():
                    self.add_error(
                        "owner_user",
                        "Selected owner must be in accommodation_owner group.",
                    )
                if selected_owner.groups.filter(name__iexact="accommodation_owner_pending").exists():
                    self.add_error("owner_user", "Selected owner is still pending approval.")
                if selected_owner.groups.filter(name__iexact="accommodation_owner_declined").exists():
                    self.add_error("owner_user", "Selected owner is marked declined.")

        if approval_status == "declined" and not rejection_reason:
            self.add_error("rejection_reason", "Decline reason is required when status is Declined.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        selected_owner = self.cleaned_data.get("owner_user")
        instance.owner = selected_owner

        approval_status = str(self.cleaned_data.get("approval_status") or "pending").strip().lower()
        rejection_reason = str(self.cleaned_data.get("rejection_reason") or "").strip()

        instance.approval_status = approval_status
        instance.status = approval_status

        if approval_status in {"accepted", "declined"}:
            instance.reviewed_at = timezone.now()
            instance.reviewed_by = self.reviewer
            instance.rejection_reason = rejection_reason if approval_status == "declined" else ""
        else:
            instance.reviewed_at = None
            instance.reviewed_by = None
            instance.rejection_reason = ""

        if commit:
            instance.save()
            certification_files = self.files.getlist("certifications")
            for cert_file in certification_files:
                AccommodationCertification.objects.create(
                    accommodation=instance,
                    image=cert_file,
                )

        return instance


class OwnerAccommodationEditForm(forms.ModelForm):
    class Meta:
        model = Accomodation
        fields = [
            "company_name",
            "company_type",
            "location",
            "phone_number",
            "email_address",
            "description",
            "accommodation_amenities",
            "official_booking_url",
            "official_contact_url",
            "profile_picture",
        ]
        labels = {
            "company_name": "Business Name",
            "company_type": "Business Type",
            "location": "Address",
            "phone_number": "Contact Number",
            "email_address": "Contact Email",
            "description": "Business Description",
            "accommodation_amenities": "Accommodation Amenities",
            "official_booking_url": "Official Booking Page URL",
            "official_contact_url": "Official Contact/Inquiry URL",
            "profile_picture": "Accommodation Photo",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "accommodation_amenities": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "e.g., WiFi, Parking, 24/7 Front Desk, Restaurant",
                }
            ),
        }

    def clean_company_name(self):
        company_name = str(self.cleaned_data.get("company_name") or "").strip()
        if not company_name:
            raise forms.ValidationError("Business name is required.")
        qs = Accomodation.objects.filter(company_name__iexact=company_name).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This business name is already used by another accommodation.")
        return company_name

    def clean_email_address(self):
        email_address = str(self.cleaned_data.get("email_address") or "").strip().lower()
        if not email_address:
            raise forms.ValidationError("Contact email is required.")
        qs = Accomodation.objects.filter(email_address__iexact=email_address).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This contact email is already used by another accommodation.")
        return email_address


# Backward-compatible alias for existing imports/usages.
AccomodationForm = AccommodationRegistrationForm


from django import forms
from .models import EstablishmentForm, Region, Country, Entry

class EstablishmentFormAdmin(forms.ModelForm):
    class Meta:
        model = EstablishmentForm
        fields = ['regions', 'countries', 'entries']

    # Fields to allow text input for new regions, countries, and entries
    new_region = forms.CharField(max_length=255, required=False, widget=forms.TextInput(attrs={
        'placeholder': 'Enter new region',
        'class': 'form-control',
        'style': 'width: 100%; padding: 10px; border-radius: 5px;'
    }))
    new_country = forms.CharField(max_length=255, required=False, widget=forms.TextInput(attrs={
        'placeholder': 'Enter new country',
        'class': 'form-control',
        'style': 'width: 100%; padding: 10px; border-radius: 5px;'
    }))
    new_entry = forms.CharField(max_length=255, required=False, widget=forms.TextInput(attrs={
        'placeholder': 'Enter new entry',
        'class': 'form-control',
        'style': 'width: 100%; padding: 10px; border-radius: 5px;'
    }))

    # Use ModelMultipleChoiceField for selecting existing regions, countries, and entries
    regions = forms.ModelMultipleChoiceField(queryset=Region.objects.all(), widget=forms.CheckboxSelectMultiple, required=False)
    countries = forms.ModelMultipleChoiceField(queryset=Country.objects.all(), widget=forms.CheckboxSelectMultiple, required=False)
    entries = forms.ModelMultipleChoiceField(queryset=Entry.objects.all(), widget=forms.CheckboxSelectMultiple, required=False)

    def clean(self):
        cleaned_data = super().clean()

        # Handle adding new regions, countries, or entries
        new_region = cleaned_data.get('new_region')
        new_country = cleaned_data.get('new_country')
        new_entry = cleaned_data.get('new_entry')

        # If new values are provided, create new records in the database
        if new_region:
            region, created = Region.objects.get_or_create(name=new_region)
            cleaned_data['regions'] = Region.objects.filter(name=new_region)

        if new_country:
            country, created = Country.objects.get_or_create(name=new_country)
            cleaned_data['countries'] = Country.objects.filter(name=new_country)

        if new_entry:
            entry, created = Entry.objects.get_or_create(title=new_entry)
            cleaned_data['entries'] = Entry.objects.filter(title=new_entry)

        return cleaned_data


class TourismInformationForm(forms.ModelForm):
    class Meta:
        model = TourismInformation
        fields = [
            "spot_name",
            "description",
            "location",
            "contact_information",
            "operating_hours",
            "publication_status",
            "is_active",
            "image",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }
