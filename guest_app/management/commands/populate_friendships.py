from django.core.management.base import BaseCommand
from guest_app.utils import populate_friendships

class Command(BaseCommand):
    help = 'Populates the Friendship table from existing relationships'
    
    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting friendship population...'))
        count = populate_friendships()
        self.stdout.write(self.style.SUCCESS(f'Successfully created {count} friendship relationships')) 