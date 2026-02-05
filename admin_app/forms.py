# admin_app/forms.py

from django import forms
from .models import Employee
from .models import Accomodation, AccommodationCertification
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



class AccomodationForm(forms.ModelForm):
    certifications = forms.FileField(required=False)
    
    class Meta:
        model = Accomodation
        # Exclude the 'status' field so it won't be validated or expected in the form.
        exclude = ['status']
        widgets = {
            'company_type': forms.Select(choices=[
                ('Hotel', 'Hotel'),
                ('Establishment', 'Establishment'),
                # Add more options as needed
            ]),
            'password': forms.PasswordInput(),
        }
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            instance.save()
            
            # Handle certifications if provided
            certification_files = self.files.getlist('certifications')
            if certification_files:
                for cert_file in certification_files:
                    AccommodationCertification.objects.create(
                        accommodation=instance,
                        image=cert_file
                    )
                    
        return instance


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
