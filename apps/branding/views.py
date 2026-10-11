from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.accounts.roles import PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.audit.services import record
from apps.branding.forms import BrandingForm
from apps.branding.models import get_branding


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR)
def branding_settings(request):
    instance = get_branding()
    if request.method == "POST":
        form = BrandingForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            changed = list(form.changed_data)
            if form.save_with_quota() is not None:
                if changed:
                    record("branding.updated", request=request, after={"changed": changed})
                messages.success(request, "Branding saved.")
                return redirect("branding:settings")
    else:
        form = BrandingForm(instance=instance)
    return render(request, "branding/settings.html", {"form": form})
