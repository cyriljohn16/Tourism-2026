from django import forms
from .models import Guest, Pending
from django.core.exceptions import ValidationError
from django.forms.widgets import Widget
from django.utils.html import format_html
from django.forms.utils import flatatt
import random
import string

class MultipleFileInput(Widget):
    """Custom widget for uploading multiple files"""
    template_name = 'django/forms/widgets/file.html'
    needs_multipart_form = True
    
    def __init__(self, attrs=None):
        super().__init__(attrs)
        self.attrs = {'multiple': 'multiple'}
        if attrs:
            self.attrs.update(attrs)
    
    def render(self, name, value, attrs=None, renderer=None):
        final_attrs = self.build_attrs(self.attrs, attrs)
        final_attrs['name'] = name
        final_attrs['type'] = 'file'
        return format_html('<input{}>', flatatt(final_attrs))

class MultipleFileField(forms.FileField):
    """Custom field for uploading multiple files"""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)
    
    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result

class BookingForm(forms.ModelForm):
    class Meta:
        model = Pending
        fields = ['total_guests', 'num_adults', 'num_children', 'your_email', 'your_name', 'your_phone']

class GuestRegistrationForm(forms.ModelForm):
    confirm_password = forms.CharField(widget=forms.PasswordInput, required=True, label="Confirm Password")
    picture = forms.ImageField(required=False)
    birthday = forms.DateField(required=True, widget=forms.DateInput(attrs={'type': 'date'}))
    credentials = MultipleFileField(required=False)
    disability_documents = MultipleFileField(required=False)
    company_name = forms.CharField(max_length=100, required=False, label="Company Name (Optional)")

    class Meta:
        model = Guest
        fields = ['first_name', 'middle_initial', 'last_name', 'birthday', 
                 'age', 'age_label', 'country_of_origin', 'city', 'phone_number', 
                 'email', 'company_name', 'sex', 'password', 'picture', 
                 'has_disability', 'disability_type']

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password != confirm_password:
            raise ValidationError("Passwords do not match.")
            
        # Calculate age from birthday if provided
        birthday = cleaned_data.get("birthday")
        if birthday:
            from datetime import date
            today = date.today()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
            cleaned_data['age'] = age
            
            # Set age_label based on age
            if age <= 1:
                cleaned_data['age_label'] = 'Infant'
            elif 2 <= age <= 4:
                cleaned_data['age_label'] = 'Toddler'
            elif 5 <= age <= 12:
                cleaned_data['age_label'] = 'Child'
            elif 13 <= age <= 19:
                cleaned_data['age_label'] = 'Teen'
            elif 20 <= age <= 39:
                cleaned_data['age_label'] = 'Adult'
            elif 40 <= age <= 59:
                cleaned_data['age_label'] = 'Middle Adult'
            elif age >= 60:
                cleaned_data['age_label'] = 'Senior'

        return cleaned_data

class CompanionForm(forms.ModelForm):
    """Form for adding companions to a user's account"""
    picture = forms.ImageField(required=False)
    birthday = forms.DateField(required=True, widget=forms.DateInput(attrs={'type': 'date'}))
    credentials = MultipleFileField(required=False)
    disability_documents = MultipleFileField(required=False)
    company_name = forms.CharField(max_length=100, required=False, label="Company Name (Optional)")

    class Meta:
        model = Guest
        fields = ['first_name', 'middle_initial', 'last_name', 'birthday',
                 'country_of_origin', 'city', 'phone_number', 
                 'email', 'company_name', 'sex', 'picture',
                 'has_disability', 'disability_type']
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make email unique but not required as we'll generate it
        self.fields['email'].required = True
        
    def clean(self):
        cleaned_data = super().clean()
        
        # Calculate age from birthday if provided
        birthday = cleaned_data.get("birthday")
        if birthday:
            from datetime import date
            today = date.today()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
            cleaned_data['age'] = age
            
            # Set age_label based on age
            if age <= 1:
                cleaned_data['age_label'] = 'Infant'
            elif 2 <= age <= 4:
                cleaned_data['age_label'] = 'Toddler'
            elif 5 <= age <= 12:
                cleaned_data['age_label'] = 'Child'
            elif 13 <= age <= 19:
                cleaned_data['age_label'] = 'Teen'
            elif 20 <= age <= 39:
                cleaned_data['age_label'] = 'Adult'
            elif 40 <= age <= 59:
                cleaned_data['age_label'] = 'Middle Adult'
            elif age >= 60:
                cleaned_data['age_label'] = 'Senior'

        return cleaned_data
        
    def save(self, commit=True, made_by=None):
        companion = super().save(commit=False)
        
        # Generate a random password for the companion
        random_password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        companion.password = random_password
        
        # Set the made_by field to link to the owner
        if made_by:
            companion.made_by = made_by
        
        if commit:
            companion.save()
            
        return companion

class BookingForm(forms.Form):
    total_guests = forms.IntegerField(min_value=1, required=True, label="Total Guests")
    num_adults = forms.IntegerField(min_value=0, required=True, label="Number of Adults")
    num_children = forms.IntegerField(min_value=0, required=True, label="Number of Children")
    your_email = forms.EmailField(required=True, label="Your Email")
    your_name = forms.CharField(max_length=255, required=True, label="Your Name")
    your_phone = forms.CharField(max_length=15, required=True, label="Your Phone Number")

class ProfileUpdateForm(forms.ModelForm):
    """Form for updating user profile information"""
    picture = forms.ImageField(required=False, label="Profile Picture")
    company_name = forms.CharField(max_length=100, required=False, label="Company Name (Optional)")
    
    class Meta:
        model = Guest
        fields = [
            'first_name', 'middle_initial', 'last_name',
            'country_of_origin', 'city', 'phone_number', 'age',
            'company_name', 'sex', 'picture'
        ]
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make all fields optional for partial updates
        for field in self.fields:
            self.fields[field].required = False
        
        # Except these key fields which should remain required
        required_fields = ['first_name', 'last_name', 'country_of_origin', 'city', 'phone_number', 'sex']
        for field in required_fields:
            self.fields[field].required = True