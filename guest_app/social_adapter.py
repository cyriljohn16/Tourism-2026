import re

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import Guest


def _is_owner_or_restricted_account(user):
    role_value = str(getattr(user, "role", "") or "").strip().lower()
    disallowed_role_values = {
        "admin",
        "employee",
        "accommodation_owner",
        "accommodation owner",
        "owner",
        "establishment",
    }
    if role_value in disallowed_role_values:
        return True
    try:
        blocked_group_names = {
            "accommodation_owner",
            "accommodation_owner_pending",
            "accommodation_owner_declined",
        }
        user_groups = {str(name).strip().lower() for name in user.groups.values_list("name", flat=True)}
        return bool(user_groups.intersection(blocked_group_names))
    except Exception:
        return False


def _generate_unique_username(email_value):
    base = (email_value or "").split("@")[0].strip().lower()
    base = re.sub(r"[^a-z0-9_\.]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("._")
    if not base:
        base = "guest"

    candidate = base
    counter = 1
    while Guest.objects.filter(username__iexact=candidate).exists():
        counter += 1
        candidate = f"{base}{counter}"
    return candidate


class GuestSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Guest-only social login adapter.
    Keeps owner/admin role separation intact and fills required Guest fields.
    """

    def pre_social_login(self, request, sociallogin):
        email = str((sociallogin.user.email or "")).strip().lower()
        if not email:
            return

        existing_user = Guest.objects.filter(email__iexact=email).first()
        if existing_user and _is_owner_or_restricted_account(existing_user):
            messages.error(
                request,
                "This account is registered for owner/admin access. Please log in through the Admin Panel.",
            )
            raise ImmediateHttpResponse(HttpResponseRedirect(reverse("admin_app:login")))

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        email = str(getattr(user, "email", "") or "").strip().lower()
        extra = getattr(sociallogin.account, "extra_data", {}) or {}

        first_name = str(getattr(user, "first_name", "") or "").strip() or str(extra.get("given_name") or "").strip()
        last_name = str(getattr(user, "last_name", "") or "").strip() or str(extra.get("family_name") or "").strip()
        user.first_name = first_name or "Guest"
        user.last_name = last_name or "User"

        if not getattr(user, "username", None):
            user.username = _generate_unique_username(email)

        # Required Guest-model fields with safe defaults for social sign-in.
        if not getattr(user, "country_of_origin", None):
            user.country_of_origin = "Philippines"
        if not getattr(user, "city", None):
            user.city = "Bayawan City"
        if not getattr(user, "phone_number", None):
            user.phone_number = "N/A"
        if not getattr(user, "sex", None):
            user.sex = "M"

        # Social accounts should remain guest/tourist by default.
        user.is_staff = False
        user.is_superuser = False
        return user

