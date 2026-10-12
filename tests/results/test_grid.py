import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.results.models import ClassResult
from tests.accounts.factories import StaffFactory
from tests.results.factories import make_sheet_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield make_sheet_world()


def login(client, w):
    client.force_login(w.teacher)


def test_grid_renders_inputs_and_data_attributes(client_a, w):
    login(client_a, w)
    r = client_a.get(reverse("results:grid", args=[w.offering.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    assert html.count('class="cell"') == 3 * len(w.comps)
    assert 'data-version="0"' in html and "data-student=" in html and "data-component=" in html
    assert f'data-api-url="{reverse("api:scores")}"' in html
    assert f'data-offering="{w.offering.pk}"' in html and f'data-term="{w.term.pk}"' in html
    assert "csrfmiddlewaretoken" in html
    assert "results/scoregrid.js" in html


def test_grid_read_only_when_class_locked(client_a, w):
    ClassResult.objects.update_or_create(
        arm=w.arm, term=w.term, defaults={"status": ClassResult.Status.PUBLISHED}
    )
    login(client_a, w)
    html = client_a.get(reverse("results:grid", args=[w.offering.pk])).content.decode()
    assert 'class="cell"' not in html and "scoregrid.js" not in html
    assert "locked" in html


def test_other_staff_and_unknown_offering(client_a, w):
    other = StaffFactory()
    set_roles(other, ["teacher"], by=None)
    client_a.force_login(other)
    for name in ("grid", "grid_csv", "grid_upload"):
        assert client_a.get(reverse(f"results:{name}", args=[w.offering.pk])).status_code in (
            403,
            405,
        )
    assert client_a.post(reverse("results:grid_upload", args=[w.offering.pk])).status_code == 403
    assert client_a.get(reverse("results:grid", args=[999999])).status_code == 404


def test_my_sheets_lists_offerings(client_a, w):
    login(client_a, w)
    r = client_a.get(reverse("results:my_sheets"))
    assert r.status_code == 200
    assert reverse("results:grid", args=[w.offering.pk]) in r.content.decode()


def test_no_current_term(client_a, w):
    from apps.academics.models import Term

    Term.objects.update(is_current=False)
    login(client_a, w)
    assert (
        "Set a current term first"
        in client_a.get(reverse("results:grid", args=[w.offering.pk])).content.decode()
    )


def test_home_link_for_teacher(client_a, w):
    login(client_a, w)
    assert "My score sheets" in client_a.get("/").content.decode()
