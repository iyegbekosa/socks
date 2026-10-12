"""HTML to PDF with WeasyPrint, never fetching a URL we did not explicitly allow."""

from __future__ import annotations

from typing import Any


def render_pdf(html: str, *, allowed_urls: frozenset[str] = frozenset()) -> bytes:
    """Render ``html`` to PDF bytes.

    Only ``data:`` URIs and the exact URLs in ``allowed_urls`` are fetched. Any other URL
    is refused with ``ValueError("blocked URL")``; WeasyPrint logs that and renders
    without the resource.

    WeasyPrint is imported here so importing Django never needs Pango.
    """
    import weasyprint
    from weasyprint.urls import URLFetcher

    class AllowListFetcher(URLFetcher):
        def fetch(self, url: str, headers: Any = None) -> Any:
            if not (url.startswith("data:") or url in allowed_urls):
                raise ValueError("blocked URL")
            return super().fetch(url, headers)

    # No redirects: an allowed URL must not bounce the renderer to one that is not.
    fetcher = AllowListFetcher(allowed_protocols={"data", "https"}, allow_redirects=False)
    return weasyprint.HTML(string=html, url_fetcher=fetcher).write_pdf()
