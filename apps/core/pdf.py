"""HTML to PDF with WeasyPrint, never fetching a URL we did not explicitly allow."""

from __future__ import annotations

from typing import Any


def _fetcher(allowed_urls: frozenset[str]) -> Any:
    """A WeasyPrint URL fetcher that fetches only ``data:`` URIs and ``allowed_urls``.

    Any other URL is refused with ``ValueError("blocked URL")``. No redirects: an allowed URL
    must not bounce the fetcher to one that is not.

    WeasyPrint is imported here so importing Django never needs Pango.
    """
    from weasyprint.urls import URLFetcher

    class AllowListFetcher(URLFetcher):
        def fetch(self, url: str, headers: Any = None) -> Any:
            if not (url.startswith("data:") or url in allowed_urls):
                raise ValueError("blocked URL")
            return super().fetch(url, headers)

    return AllowListFetcher(allowed_protocols={"data", "https"}, allow_redirects=False)


def render_pdf(html: str, *, allowed_urls: frozenset[str] = frozenset()) -> bytes:
    """Render ``html`` to PDF bytes.

    Only ``data:`` URIs and the exact URLs in ``allowed_urls`` are fetched. Any other URL
    is refused with ``ValueError("blocked URL")``; WeasyPrint logs that and renders
    without the resource.
    """
    import weasyprint

    return weasyprint.HTML(string=html, url_fetcher=_fetcher(allowed_urls)).write_pdf()


def fetch_url(url: str, *, allowed_urls: frozenset[str]) -> bytes:
    """Fetch ``url`` with the same allow-list fetcher the renderer uses.

    Raises ``ValueError("blocked URL")`` unless ``url`` is in ``allowed_urls`` (or a
    ``data:`` URI); network errors propagate.
    """
    response = _fetcher(allowed_urls).fetch(url)
    try:
        data: bytes = response.read()
    finally:
        response.close()
    return data
