"""DD/MM/YYYY everywhere (Nigerian convention); Django's stock en_GB already does this,
but pinning it here keeps templates independent of Django upgrades."""

DATE_FORMAT = "d/m/Y"
DATETIME_FORMAT = "d/m/Y H:i"
SHORT_DATE_FORMAT = "d/m/Y"
SHORT_DATETIME_FORMAT = "d/m/Y H:i"
TIME_FORMAT = "H:i"
