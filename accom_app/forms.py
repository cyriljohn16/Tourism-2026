from django import forms
from .models import Other_Estab



class OtherEstabForm(forms.ModelForm):
    class Meta:
        model = Other_Estab
        fields = ['month', 'region', 'country', 'local', 'residences',
                  'total_foreign_travelers', 'overseas', 'domestic']

