from django.urls import path

from apps.fees.views import invoices, manual, payout, receipts, setup

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
    path("students/<str:student_pk>/payments/new/", manual.manual_new, name="manual_new"),
    path("payments/pending/", manual.manual_pending, name="manual_pending"),
    path("payments/manual/<str:pk>/decide/", manual.manual_decide, name="manual_decide"),
    path("payments/manual/<str:pk>/teller/", manual.teller_photo, name="teller_photo"),
    path("receipts/<int:receipt_pk>/", receipts.receipt_download, name="receipt_download"),
    path(
        "receipts/<int:receipt_pk>/regenerate/",
        receipts.receipt_regenerate,
        name="receipt_regenerate",
    ),
    path("payout/", payout.payout, name="payout"),
    path("pay/test/<str:reference>/", payout.fake_checkout, name="fake_checkout"),
]
