import logging

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.ratelimit import RateLimit
from apps.core.request_id import client_ip
from apps.tenancy.models import Plan, School
from apps.tenancy.provisioning import provision_school
from apps.tenancy.signup import SignupForm, schema_name_for

logger = logging.getLogger(__name__)

STATUS_SALT = "signup-status"
STATUS_MAX_AGE = 86400
_signup_limit = RateLimit("signup", limit=5, window=3600)
_global_limit = RateLimit("signup_global", limit=50, window=86400)
GLOBAL_KEY = "all"
BUSY_MESSAGE = "We're getting a lot of signups — please try again later or contact us."


def landing(request):
    return render(request, "tenancy/landing.html")


def _too_many(request, message: str, limiter: RateLimit, key: str) -> HttpResponse:
    response = render(
        request,
        "tenancy/signup.html",
        {"form": SignupForm(), "blocked": message},
        status=429,
    )
    response["Retry-After"] = str(limiter.retry_after(key))
    return response


def signup(request):
    if request.method != "POST":
        return render(request, "tenancy/signup.html", {"form": SignupForm()})

    # Reserve first: every POST counts, valid or not (cheap defence against schema spam).
    if _global_limit.hit(GLOBAL_KEY) > _global_limit.limit:
        return _too_many(request, BUSY_MESSAGE, _global_limit, GLOBAL_KEY)
    ip = client_ip(request)
    if ip is not None and _signup_limit.hit(ip) > _signup_limit.limit:
        return _too_many(
            request,
            "Too many sign-up attempts from your network. Please try again later.",
            _signup_limit,
            ip,
        )

    form = SignupForm(request.POST)
    if not form.is_valid():
        return render(request, "tenancy/signup.html", {"form": form}, status=200)
    data = form.cleaned_data
    try:
        school = _create_school(data)
    except IntegrityError:  # a concurrent signup took the same subdomain
        form.add_error("subdomain", "This web address is already taken.")
        return render(request, "tenancy/signup.html", {"form": form})
    try:
        provision_school.delay(school.pk)
    except Exception:
        # Eager mode surfaces task errors here; a queue outage must not leave it "pending".
        logger.exception("Provisioning task raised", extra={"school_id": school.pk})
        School.objects.filter(pk=school.pk, provisioning_status="pending").update(
            provisioning_status="failed"
        )
    token = signing.dumps(school.pk, salt=STATUS_SALT)
    return redirect(reverse("signup_status", args=[token]))


def _create_school(data) -> School:
    with transaction.atomic():
        school = School(
            schema_name=schema_name_for(data["subdomain"]),
            name=data["school_name"].strip(),
            plan=Plan.objects.get(code=data["plan"]),
            levels_offered=data["levels_offered"],
            contact_name=data["contact_name"].strip(),
            contact_phone=data["contact_phone"],
            contact_email=data["contact_email"],
            provisioning_status="pending",
        )
        school.auto_create_schema = False  # the provisioning task creates the schema
        school.save()
    return school


def signup_status(request, token: str):
    try:
        school_id = signing.loads(token, salt=STATUS_SALT, max_age=STATUS_MAX_AGE)
    except signing.BadSignature:
        raise Http404 from None
    with schema_context(get_public_schema_name()):
        school = School.objects.filter(pk=school_id).first()
    if school is None:
        raise Http404
    subdomain = school.schema_name.replace("_", "-")
    context = {
        "school": school,
        "status": school.provisioning_status,
        "portal_url": settings.PLATFORM_URL_TEMPLATE.format(subdomain=subdomain),
    }
    response: HttpResponse = render(request, "tenancy/signup_status.html", context)
    response["Cache-Control"] = "no-store"
    return response
