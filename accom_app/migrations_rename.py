from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ('accom_app', '0014_hotelrooms_person_limit'),  # Update this to your latest migration
    ]

    operations = [
        migrations.RenameModel(
            old_name='HotelRooms',
            new_name='Room',
        ),
    ] 