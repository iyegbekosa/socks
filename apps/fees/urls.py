from django.urls import path

from apps.fees.views import invoices, setup

app_name = "fees"

urlpatterns = [
    path("", setup.structures, name="structures"),
    path("settings/", setup.settings_page, name="settings"),
    path("items/", setup.items, name="items"),
    path("items/<int:item_id>/", setup.item_edit, name="item_edit"),
    path("structures/<int:level_pk>/<int:term_pk>/", setup.structure_edit, name="structure_edit"),
    path("discounts/", setup.discounts, name="discounts"),
    path("discounts/<int:discount_id>/", setup.discount_edit, name="discount_edit"),
    path("students/<int:student_pk>/discounts/", setup.student_discounts, name="student_discounts"),
    path("invoices/", invoices.invoices, name="invoices"),
    path("invoices/<str:invoice_id>/", invoices.invoice_detail, name="invoice"),
]
