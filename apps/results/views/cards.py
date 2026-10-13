from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.results.cards import card_withheld, downloadable_card


def signed_redirect(file_name: str) -> HttpResponseRedirect:
    """302 to a short-lived signed URL for a private file; never cached."""
    response = HttpResponseRedirect(default_storage.url(file_name))
    response["Cache-Control"] = "no-store"
    return response


@require_GET
@login_required
def report_card(request, student_pk: str, term_pk: str) -> HttpResponse:
    """Download a report card: students their own once published, staff through
    ``students_visible_to``. Anything else is a 404. A student's card for a term whose fees
    are owed is a 403 when results blocking is on (UC-17)."""
    card = downloadable_card(request.user, student_pk, term_pk)
    if card is None:
        raise Http404
    if card_withheld(request.user, card):
        from apps.fees.blocking import WITHHELD_MESSAGE

        response = render(
            request, "results/withheld.html", {"message": WITHHELD_MESSAGE}, status=403
        )
        response["Cache-Control"] = "no-store"
        return response
    return signed_redirect(card.file_name)
