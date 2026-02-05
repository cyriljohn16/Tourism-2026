from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password

# Get the custom user model (Guest)
User = get_user_model()

class GuestAuthenticationBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None):
        """
        Authenticate a user based on username or email and password.
        """
        try:
            # First, try to get the user by username
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                # If not found, try to get the user by email
                try:
                    user = User.objects.get(email=username)
                except User.DoesNotExist:
                    print(f"User with username/email {username} does not exist.")
                    return None

            # Verify the password against the stored hashed password
            if user.check_password(password):
                print(f"User {username} authenticated successfully.")
                return user  # Return the authenticated user object
            else:
                print(f"Invalid password for user {username}.")
        except Exception as e:
            print(f"Authentication error: {str(e)}")
        return None  # Return None if authentication fails

    def get_user(self, user_id):
        """
        Retrieve a user by their ID.
        """
        print(f"Fetching user with ID: {user_id}")  # Debugging
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            print(f"User with ID {user_id} does not exist.")  # Debugging
            return None  # Return None if the user does not exist