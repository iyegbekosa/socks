import base64
import logging
from unittest import mock

import pytest

from apps.core.pdf import render_pdf

pytestmark = pytest.mark.pdf

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99="
    b"\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _render(*args, **kwargs) -> bytes:
    """Render, failing (never skipping) with a clear message when Pango is missing."""
    try:
        return render_pdf(*args, **kwargs)
    except OSError:
        pytest.fail("Pango not available — run scripts/dev_pango.sh and use scripts/with-pango")


def _weasyprint():
    try:
        import weasyprint
    except OSError:
        pytest.fail("Pango not available — run scripts/dev_pango.sh and use scripts/with-pango")
    return weasyprint


def test_renders_a_pdf():
    assert _render("<h1>Hi</h1>").startswith(b"%PDF")


def test_blocks_urls_that_are_not_allowed(caplog):
    _weasyprint()
    with (
        mock.patch("weasyprint.urls.URLFetcher.fetch") as fetch,
        caplog.at_level(logging.WARNING, logger="weasyprint"),
    ):
        pdf = _render('<h1>Hi</h1><img src="https://evil.example/x.png">')
    fetch.assert_not_called()
    assert pdf.startswith(b"%PDF")
    assert "blocked URL" in caplog.text


def test_allowed_url_is_fetched():
    _weasyprint()
    url = "https://res.cloudinary.com/demo/logo.png"
    response = _weasyprint().urls.URLFetcherResponse(url, PNG, {"Content-Type": "image/png"}, 200)
    with mock.patch("weasyprint.urls.URLFetcher.fetch", return_value=response) as fetch:
        pdf = _render(f'<img src="{url}">', allowed_urls=frozenset({url}))
    assert fetch.call_args.args[0] == url
    assert pdf.startswith(b"%PDF")


def test_data_uris_are_allowed():
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert _render(f'<img src="{uri}">').startswith(b"%PDF")
