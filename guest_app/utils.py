import calendar  # For getting the number of days in a month
from django.utils.translation import gettext as _
from django.utils import translation
import json
import os
from django.db import models
from .models import Guest, FriendGroup, CompanionRequest, Friendship

# Dictionary to store all translations
TRANSLATIONS = {
    'en': {},  # English (default)
    'tl': {},  # Tagalog
    'ceb': {}, # Cebuano
    'es': {}   # Spanish
}

# Basic translations for system text
SYSTEM_TRANSLATIONS = {
    # Navigation and basic UI
    'ibayaw_tour': {
        'en': 'Ibayaw Tour',
        'tl': 'Ibayaw Tour',
        'ceb': 'Ibayaw Tour',
        'es': 'Tour Ibayaw'
    },
    'nav_tour_packs': {
        'en': 'Tour Packs',
        'tl': 'Mga Tour Pack', 
        'ceb': 'Mga Tour Pack',
        'es': 'Paquetes de Tour'
    },
    'nav_programs': {
        'en': 'Programs',
        'tl': 'Mga Programa',
        'ceb': 'Mga Programa',
        'es': 'Programas'
    },
    'nav_city_map': {
        'en': 'City Map',
        'tl': 'Mapa ng Lungsod',
        'ceb': 'Mapa sa Syudad',
        'es': 'Mapa de la Ciudad'
    },
    'nav_about_us': {
        'en': 'About Us',
        'tl': 'Tungkol sa Amin',
        'ceb': 'Mahitungod Kanamo',
        'es': 'Sobre Nosotros'
    },
    'nav_contact': {
        'en': 'Contact',
        'tl': 'Kontak',
        'ceb': 'Kontak',
        'es': 'Contacto'
    },
    
    # Hero section
    'hero_title': {
        'en': 'RIVER TOUR',
        'tl': 'TOUR SA ILOG',
        'ceb': 'TOUR SA SUBA',
        'es': 'TOUR DEL RÍO'
    },
    'initiative': {
        'en': 'Initiative',
        'tl': 'Inisyatiba',
        'ceb': 'Inisyatiba',
        'es': 'Iniciativa'
    },
    'bayawan_city': {
        'en': 'BAYAWAN CITY',
        'tl': 'LUNGSOD NG BAYAWAN',
        'ceb': 'SYUDAD SA BAYAWAN',
        'es': 'CIUDAD DE BAYAWAN'
    },
    'hero_description': {
        'en': 'We exist to improve the quality of Bayawanons, maximizing its tourism potential, using a website that will help elevate the already existing tours',
        'tl': 'Kami ay naririto upang mapabuti ang kalidad ng buhay ng mga Bayawanon, pinapakinabangan ang potensyal nito sa turismo, gamit ang website na tutulong na maangat ang mga umiiral na turing',
        'ceb': 'Ania kami aron pagpalambo sa kalidad sa mga Bayawanon, pagpaayo sa potensyal sa turismo, gamit ang website nga motabang sa pagpalambo sa mga tour nga anaa na',
        'es': 'Existimos para mejorar la calidad de los Bayawanones, maximizando su potencial turístico, utilizando un sitio web que ayudará a elevar los tours ya existentes'
    },
    
    # Tour packages section
    'ibayaw': {
        'en': 'Ibayaw',
        'tl': 'Ibayaw',
        'ceb': 'Ibayaw',
        'es': 'Ibayaw'
    },
    'tour_packs': {
        'en': 'TOUR PACKS',
        'tl': 'MGA PACKAGE NG TOUR',
        'ceb': 'MGA TOUR PACK',
        'es': 'PAQUETES DE TOUR'
    },
    'tour_packs_description': {
        'en': 'Now, let\'s see what activities can be seen in Bayawan, foods and its Hidden gems!',
        'tl': 'Ngayon, tingnan natin ang mga aktibidad na makikita sa Bayawan, mga pagkain at mga hidden gems nito!',
        'ceb': 'Karon, tan-awon nato unsa nga mga kalihokan ang makita sa Bayawan, mga pagkaon ug mga tinago nga bahandi niini!',
        'es': '¡Ahora, veamos qué actividades se pueden ver en Bayawan, comidas y sus joyas escondidas!'
    },
    'explore_all_tour': {
        'en': 'EXPLORE ALL TOUR',
        'tl': 'GALUGARIN ANG LAHAT NG TOUR',
        'ceb': 'SUSI-A ANG TANANG TOUR',
        'es': 'EXPLORAR TODOS LOS TOURS'
    },
    
    # Tour card elements
    'duration_based': {
        'en': 'Duration based on schedule',
        'tl': 'Tagal batay sa iskedyul',
        'ceb': 'Gidugayon base sa iskedyul',
        'es': 'Duración según horario'
    },
    'day_singular': {
        'en': 'day',
        'tl': 'araw',
        'ceb': 'adlaw',
        'es': 'día'
    },
    'day_plural': {
        'en': 'days',
        'tl': 'araw',
        'ceb': 'adlaw',
        'es': 'días'
    },
    'no_schedules_made': {
        'en': 'No schedules made for this tour yet',
        'tl': 'Wala pang iskedyul na ginawa para sa tour na ito',
        'ceb': 'Wala pay iskedyul nga gihimo alang niini nga tour',
        'es': 'Aún no se han hecho horarios para este tour'
    },
    
    # Demo tours
    'forest_tour': {
        'en': 'FOREST',
        'tl': 'GUBAT',
        'ceb': 'KALASANGAN',
        'es': 'BOSQUE'
    },
    'river_tour': {
        'en': 'RIVER',
        'tl': 'ILOG',
        'ceb': 'SUBA',
        'es': 'RÍO'
    },
    'sea_tour': {
        'en': 'SEA',
        'tl': 'DAGAT',
        'ceb': 'DAGAT',
        'es': 'MAR'
    },
    'mountain_tour': {
        'en': 'MOUNTAIN',
        'tl': 'BUNDOK',
        'ceb': 'BUKID',
        'es': 'MONTAÑA'
    },
    'sunset_tour': {
        'en': 'SUNSET',
        'tl': 'PAGLUBOG NG ARAW',
        'ceb': 'PAGSALOP SA ADLAW',
        'es': 'ATARDECER'
    },
    
    # User dropdown
    'my_profile': {
        'en': 'My Profile',
        'tl': 'Aking Profile',
        'ceb': 'Akong Profile',
        'es': 'Mi Perfil'
    },
    'update_profile': {
        'en': 'Update Profile',
        'tl': 'I-update ang Profile',
        'ceb': 'I-update ang Profile',
        'es': 'Actualizar Perfil'
    },
    'logout': {
        'en': 'Logout',
        'tl': 'Mag-logout',
        'ceb': 'Pag-logout',
        'es': 'Cerrar Sesión'
    },
    'language': {
        'en': 'Language',
        'tl': 'Wika',
        'ceb': 'Pinulongan',
        'es': 'Idioma'
    },
    
    # Login/signup
    'login': {
        'en': 'Login',
        'tl': 'Mag-login',
        'ceb': 'Pag-login',
        'es': 'Iniciar Sesión'
    },
    'signup_to_continue': {
        'en': 'Sign Up to Continue',
        'tl': 'Mag-sign Up para Magpatuloy',
        'ceb': 'Pag-sign Up aron Mopadayon',
        'es': 'Regístrese para Continuar'
    },
    'login_to_continue': {
        'en': 'Login to Continue',
        'tl': 'Mag-login para Magpatuloy',
        'ceb': 'Pag-login aron Mopadayon',
        'es': 'Inicie Sesión para Continuar'
    },
    'update_your_profile': {
        'en': 'Update Your Profile',
        'tl': 'I-update ang Iyong Profile',
        'ceb': 'I-update ang Imong Profile',
        'es': 'Actualice Su Perfil'
    },
    'sign_up': {
        'en': 'Sign Up',
        'tl': 'Mag-sign Up',
        'ceb': 'Pag-sign Up',
        'es': 'Registrarse'
    },
    'log_in': {
        'en': 'Login',
        'tl': 'Mag-login',
        'ceb': 'Pag-login',
        'es': 'Iniciar Sesión'
    },
    'already_have_account': {
        'en': 'Already have an account? ',
        'tl': 'May account ka na? ',
        'ceb': 'Aduna na kay account? ',
        'es': '¿Ya tiene una cuenta? '
    },
    'dont_have_account': {
        'en': 'Don\'t have an account? ',
        'tl': 'Wala kang account? ',
        'ceb': 'Wala kay account? ',
        'es': '¿No tiene una cuenta? '
    },
    'log_in_here': {
        'en': 'Log in here',
        'tl': 'Mag-login dito',
        'ceb': 'Pag-login dinhi',
        'es': 'Inicie sesión aquí'
    },
    'sign_up_here': {
        'en': 'Sign up here',
        'tl': 'Mag-sign up dito',
        'ceb': 'Pag-sign up dinhi',
        'es': 'Regístrese aquí'
    },
    
    # Success messages
    'registration_successful': {
        'en': 'Registration successful! Logging you in...',
        'tl': 'Matagumpay ang pagpaparehistro! Ini-login ka...',
        'ceb': 'Malamposong pagparehistro! Gipasulod ka...',
        'es': '¡Registro exitoso! Iniciando sesión...'
    },
    'login_successful': {
        'en': 'Login successful! Redirecting...',
        'tl': 'Matagumpay ang pag-login! Idinidirekta...',
        'ceb': 'Malamposong pag-login! Gituy-od...',
        'es': '¡Inicio de sesión exitoso! Redirigiendo...'
    },
    'profile_updated': {
        'en': 'Profile updated successfully!',
        'tl': 'Matagumpay na na-update ang profile!',
        'ceb': 'Malamposong na-update ang profile!',
        'es': '¡Perfil actualizado con éxito!'
    },
    
    # Form labels - add as many as needed
    'label_first_name': {
        'en': 'First Name',
        'tl': 'Pangalan',
        'ceb': 'Unang Ngalan',
        'es': 'Nombre'
    },
    'label_middle_initial': {
        'en': 'M.I.',
        'tl': 'Gitnang Inisyal',
        'ceb': 'Tungatunga nga Inisyal',
        'es': 'Inicial'
    },
    'label_last_name': {
        'en': 'Last Name',
        'tl': 'Apelyido',
        'ceb': 'Apelyido',
        'es': 'Apellido'
    },
    'label_username': {
        'en': 'Username',
        'tl': 'Username',
        'ceb': 'Username',
        'es': 'Nombre de Usuario'
    },
    'label_email': {
        'en': 'Email Address',
        'tl': 'Email Address',
        'ceb': 'Email Address',
        'es': 'Correo Electrónico'
    },
    'label_password': {
        'en': 'Password',
        'tl': 'Password',
        'ceb': 'Password',
        'es': 'Contraseña'
    },
    'label_confirm_password': {
        'en': 'Confirm Password',
        'tl': 'Kumpirmahin ang Password',
        'ceb': 'Kompirma sa Password',
        'es': 'Confirmar Contraseña'
    },
    'label_country_of_origin': {
        'en': 'Country of Origin',
        'tl': 'Bansang Pinagmulan',
        'ceb': 'Nasud nga Gigikanan',
        'es': 'País de Origen'
    },
    'label_city': {
        'en': 'City',
        'tl': 'Lungsod',
        'ceb': 'Syudad',
        'es': 'Ciudad'
    },
    'label_phone_number': {
        'en': 'Phone Number',
        'tl': 'Numero ng Telepono',
        'ceb': 'Numero sa Telepono',
        'es': 'Número de Teléfono'
    },
    'label_age': {
        'en': 'Age',
        'tl': 'Edad',
        'ceb': 'Edad',
        'es': 'Edad'
    },
    'label_company_name': {
        'en': 'Company Name',
        'tl': 'Pangalan ng Kumpanya',
        'ceb': 'Ngalan sa Kompaniya',
        'es': 'Nombre de la Compañía'
    },
    'label_sex': {
        'en': 'Sex',
        'tl': 'Kasarian',
        'ceb': 'Sekso',
        'es': 'Sexo'
    },
    'label_profile_picture': {
        'en': 'Profile Picture',
        'tl': 'Larawan ng Profile',
        'ceb': 'Hulagway sa Profile',
        'es': 'Foto de Perfil'
    },
    'label_picture': {
        'en': 'Profile Picture',
        'tl': 'Larawan ng Profile',
        'ceb': 'Hulagway sa Profile',
        'es': 'Foto de Perfil'
    },
    
    # Booking related
    'book_now': {
        'en': 'Book Now',
        'tl': 'Mag-book Ngayon',
        'ceb': 'Pagbook Karon',
        'es': 'Reservar Ahora'
    },
    'available': {
        'en': 'Available',
        'tl': 'Magagamit',
        'ceb': 'Magamit',
        'es': 'Disponible'
    },
    'booked': {
        'en': 'Booked',
        'tl': 'Na-book na',
        'ceb': 'Nabook na',
        'es': 'Reservado'
    },
    'adults': {
        'en': 'Adults',
        'tl': 'Mga Adulto',
        'ceb': 'Mga Hamtong',
        'es': 'Adultos'
    },
    'children': {
        'en': 'Children',
        'tl': 'Mga Bata',
        'ceb': 'Mga Bata',
        'es': 'Niños'
    },
    'total': {
        'en': 'Total',
        'tl': 'Kabuuan',
        'ceb': 'Kinatibuk-an',
        'es': 'Total'
    },
    'schedule_id': {
        'en': 'Schedule ID',
        'tl': 'ID ng Iskedyul',
        'ceb': 'ID sa Iskedyul',
        'es': 'ID de Horario'
    },
    'start_time': {
        'en': 'Start Time',
        'tl': 'Oras ng Simula',
        'ceb': 'Oras sa Pagsugod',
        'es': 'Hora de Inicio'
    },
    'end_time': {
        'en': 'End Time',
        'tl': 'Oras ng Pagtatapos',
        'ceb': 'Oras sa Pagtapos',
        'es': 'Hora de Finalización'
    },
    'price': {
        'en': 'Price',
        'tl': 'Presyo',
        'ceb': 'Presyo',
        'es': 'Precio'
    },
    'available_slots': {
        'en': 'Available Slots',
        'tl': 'Magagamit na Slots',
        'ceb': 'Bakante nga Slots',
        'es': 'Espacios Disponibles'
    },
    'booked_slots': {
        'en': 'Booked Slots',
        'tl': 'Nakubang Slots',
        'ceb': 'Napalgan nga Slots',
        'es': 'Espacios Reservados'
    },
    'book_this_schedule': {
        'en': 'Book This Schedule',
        'tl': 'I-book ang Iskedyul na Ito',
        'ceb': 'I-book Kini nga Iskedyul',
        'es': 'Reservar Este Horario'
    },
    'no_more_slots': {
        'en': 'No more available slots',
        'tl': 'Wala nang magagamit na slots',
        'ceb': 'Wala nay magamit nga slots',
        'es': 'No hay más espacios disponibles'
    },
    'no_schedules': {
        'en': 'No schedules available for this tour',
        'tl': 'Walang magagamit na iskedyul para sa tour na ito',
        'ceb': 'Walay magamit nga iskedyul para sa kini nga tour',
        'es': 'No hay horarios disponibles para este tour'
    },
    'back_to_main': {
        'en': 'Back to Main Page',
        'tl': 'Bumalik sa Main Page',
        'ceb': 'Balik sa Main Page',
        'es': 'Volver a la Página Principal'
    },
    'book_tour': {
        'en': 'Book Tour',
        'tl': 'Mag-book ng Tour',
        'ceb': 'Pag-book sa Tour',
        'es': 'Reservar Tour'
    },
    # Booking form translations
    'guests': {
        'en': 'Guests',
        'tl': 'Mga Bisita',
        'ceb': 'Mga Bisita',
        'es': 'Huéspedes'
    },
    'payment': {
        'en': 'Payment',
        'tl': 'Bayad',
        'ceb': 'Bayad',
        'es': 'Pago'
    },
    'confirm_booking': {
        'en': 'Confirm Booking',
        'tl': 'Kumpirmahin ang Booking',
        'ceb': 'Kumpirmaha ang Booking',
        'es': 'Confirmar Reserva'
    },
    'close': {
        'en': 'Close',
        'tl': 'Isara',
        'ceb': 'Isira',
        'es': 'Cerrar'
    },
    'not_enough_slots': {
        'en': 'Not enough available slots! Maximum allowed is {0}.',
        'tl': 'Hindi sapat ang magagamit na slots! Pinakamataas na pinapayagan ay {0}.',
        'ceb': 'Dili igo ang magamit nga slots! Pinakataas nga gitugotan mao ang {0}.',
        'es': '¡No hay suficientes espacios disponibles! El máximo permitido es {0}.'
    },
    'booking_success': {
        'en': 'Booking successful!',
        'tl': 'Matagumpay ang booking!',
        'ceb': 'Malamposong booking!',
        'es': '¡Reserva exitosa!'
    },
    'booking_error': {
        'en': 'Error occurred during booking. Please try again.',
        'tl': 'May error na naganap sa booking. Pakisubukan ulit.',
        'ceb': 'May error nga nahitabo sa booking. Palihog suwayi usab.',
        'es': 'Ocurrió un error durante la reserva. Por favor, inténtelo de nuevo.'
    }
}

# Initialize the basic translations
for key, translations in SYSTEM_TRANSLATIONS.items():
    for lang, text in translations.items():
        TRANSLATIONS[lang][key] = text

def translate(key, lang='en'):
    """
    Translate a key to the specified language
    
    Args:
        key (str): The translation key
        lang (str): Language code ('en', 'tl', 'ceb', 'es')
        
    Returns:
        str: Translated text or the key itself if translation not found
    """
    if lang not in TRANSLATIONS:
        lang = 'en'  # Default to English if language not supported
    
    return TRANSLATIONS[lang].get(key, key)

def get_translations_json(lang='en'):
    """
    Get all translations for a language as JSON for use in JavaScript
    
    Args:
        lang (str): Language code
        
    Returns:
        str: JSON string with all translations
    """
    if lang not in TRANSLATIONS:
        lang = 'en'
        
    return json.dumps(TRANSLATIONS[lang])

# Define our own language session key constant
LANGUAGE_SESSION_KEY = 'django_language'

def set_language(request, lang_code):
    """
    Set the language for the current user session
    
    Args:
        request: The HTTP request object
        lang_code (str): Language code
        
    Returns:
        None
    """
    if lang_code not in ['en', 'tl', 'ceb', 'es']:
        lang_code = 'en'
        
    # Set Django's translation language
    translation.activate(lang_code)
    
    # Save to session
    request.session[LANGUAGE_SESSION_KEY] = lang_code
    
    # Save the language preference cookie
    request.LANGUAGE_CODE = lang_code
    
def get_current_language(request):
    """
    Get the current language from the user session
    
    Args:
        request: The HTTP request object
        
    Returns:
        str: Current language code
    """
    # Try to get from session
    lang = request.session.get(LANGUAGE_SESSION_KEY)
    
    # If not in session, try to get from cookies or request
    if not lang:
        lang = getattr(request, 'LANGUAGE_CODE', 'en')
    
    # Ensure it's a valid language
    if lang not in ['en', 'tl', 'ceb', 'es']:
        lang = 'en'
        
    return lang

# Function to translate a database object's fields
def translate_object(obj, fields, lang='en'):
    """
    Translate specified fields of a database object
    
    Args:
        obj: Database object to translate
        fields (list): List of field names to translate
        lang (str): Target language
        
    Returns:
        dict: Dictionary with translated fields
    """
    result = {}
    for field in fields:
        # Get original value
        value = getattr(obj, field, '')
        
        # Check if there's a translated field
        trans_field = f"{field}_{lang}"
        if hasattr(obj, trans_field) and getattr(obj, trans_field):
            value = getattr(obj, trans_field)
        
        result[field] = value
    
    return result

def get_month_days(year, month):
    # Get the number of days in a month
    days_in_month = calendar.monthrange(year, month)[1]  # This returns (weekday, number_of_days)

    # Create a list of all the days in the month (1 to days_in_month)
    days = [i for i in range(1, days_in_month + 1)]

    return days

def populate_friendships():
    """
    Utility function to populate the Friendship table from existing relationships in the database.
    This should be run once to migrate existing relationships to the new model.
    """
    count = 0
    
    # 1. Get companions directly added by users
    print("Processing direct companions...")
    direct_companions = Guest.objects.filter(made_by__isnull=False)
    for companion in direct_companions:
        if companion.made_by:
            try:
                # Use the companion's actual group name if available
                group_name = 'Personal Companions'
                if companion.group:
                    group_name = companion.group.name
                
                Friendship.make_friendship(
                    user=companion.made_by,
                    friend=companion,
                    group_name=group_name
                )
                count += 1
            except Exception as e:
                print(f"Error adding direct companion friendship: {e}")
    
    # 2. Process friend groups
    print("Processing friend groups...")
    try:
        friend_groups = FriendGroup.objects.all()
        for group in friend_groups:
            members = list(group.members.all())
            owner = group.owner
            
            # Make all members friends with the owner
            for member in members:
                if member != owner:
                    try:
                        Friendship.make_friendship(
                            user=owner,
                            friend=member,
                            group_name=group.name
                        )
                        count += 1
                    except Exception as e:
                        print(f"Error adding friend group owner->member friendship: {e}")
            
            # Make all members friends with each other
            for i in range(len(members)):
                for j in range(i+1, len(members)):
                    try:
                        Friendship.make_friendship(
                            user=members[i],
                            friend=members[j],
                            group_name=group.name
                        )
                        count += 1
                    except Exception as e:
                        print(f"Error adding friend group membership friendship: {e}")
    except Exception as e:
        print(f"Error processing friend groups: {e}")
    
    # 3. Process accepted companion requests
    print("Processing accepted companion requests...")
    try:
        accepted_requests = CompanionRequest.objects.filter(status='accepted')
        for request in accepted_requests:
            try:
                group_name = 'Connected Guests'
                if request.group:
                    group_name = request.group.name
                
                Friendship.make_friendship(
                    user=request.sender,
                    friend=request.recipient,
                    group_name=group_name
                )
                count += 1
            except Exception as e:
                print(f"Error adding companion request friendship: {e}")
    except Exception as e:
        print(f"Error processing companion requests: {e}")
    
    print(f"Created {count} friendship relationships.")
    return count