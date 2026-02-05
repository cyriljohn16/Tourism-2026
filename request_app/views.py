from django.shortcuts import render

# Create your views here.

def main_admin(request):
    context = {}
    return render(request, 'admin/main_admin.html', context)
