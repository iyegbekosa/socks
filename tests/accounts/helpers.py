import time

from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_tenants.utils import schema_context


def login_verified(client, user):
    """Log ``user`` in with an OTP-verified session, as after a completed MFA step."""
    schema = client.tenant.schema_name
    with schema_context(schema):
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
        client.force_login(user)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session["last_seen"] = int(time.time())
        session.save()
    return device
