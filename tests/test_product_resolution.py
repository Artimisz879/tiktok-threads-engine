"""TikTok redirect resolution + public metadata parsing (network mocked)."""
import httpx
import pytest
import respx

from app.product.providers import PublicMetadataResolver
from app.security.url_validation import resolve_redirects

SAMPLE_HTML = """
<html><head>
<title>Portable Turbo Fan USB Rechargeable - TikTok Shop</title>
<meta property="og:title" content="Portable Turbo Fan USB Rechargeable" />
<meta property="og:description" content="3 speeds, 4000mAh, clip-on. RM19.90" />
<meta property="og:image" content="https://p16-oec.s3cn.com/fan.jpg" />
<script type="application/ld+json">
{"@type": "Product", "name": "Portable Turbo Fan", "description": "USB fan", "offers": {"price": "19.90", "priceCurrency": "MYR"}}
</script>
</head><body>buy now</body></html>
"""


@pytest.mark.asyncio
async def test_redirect_resolution_chain():
    """vt.tiktok.com -> www.tiktok.com/shop/... resolved, chain recorded."""
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://vt.tiktok.com/abc/").mock(
                return_value=httpx.Response(302, headers={"location": "https://www.tiktok.com/shop/product/999"})
            )
            mock.get("https://www.tiktok.com/shop/product/999").mock(
                return_value=httpx.Response(200, text=SAMPLE_HTML)
            )
            final, chain = await resolve_redirects(client, "https://vt.tiktok.com/abc/")
    assert final == "https://www.tiktok.com/shop/product/999"
    assert chain[0] == "https://vt.tiktok.com/abc/"
    assert chain[-1] == final


@pytest.mark.asyncio
async def test_redirect_leaving_allowlist_stops():
    """A redirect to a non-allowlisted host must stop the chain (no SSRF)."""
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://vt.tiktok.com/evil/").mock(
                return_value=httpx.Response(302, headers={"location": "https://attacker.com/steal"})
            )
            final, chain = await resolve_redirects(client, "https://vt.tiktok.com/evil/")
    assert final == "https://vt.tiktok.com/evil/"
    assert len(chain) == 1


@pytest.mark.asyncio
async def test_metadata_extraction():
    resolver = PublicMetadataResolver()
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://www.tiktok.com/shop/product/999").mock(
                return_value=httpx.Response(200, text=SAMPLE_HTML)
            )
            mock.get("https://www.tiktok.com/oembed").mock(
                return_value=httpx.Response(400, text="{}")
            )
            result = await resolver.resolve("https://www.tiktok.com/shop/product/999", client)
    assert result is not None
    assert "Portable Turbo Fan" in result.title
    assert result.price == 19.90
    assert result.currency == "MYR"
    assert any("fan.jpg" in img for img in result.images)
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_metadata_empty_page_low_confidence():
    resolver = PublicMetadataResolver()
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://www.tiktok.com/shop/product/000").mock(
                return_value=httpx.Response(200, text="<html><body></body></html>")
            )
            mock.get("https://www.tiktok.com/oembed").mock(
                return_value=httpx.Response(400, text="{}")
            )
            result = await resolver.resolve("https://www.tiktok.com/shop/product/000", client)
    assert result is not None
    assert result.title == ""
    assert result.confidence < 0.3  # honest: we got nothing


def test_price_extraction_variants():
    r = PublicMetadataResolver._extract_price
    assert r("RM19.90") == 19.90
    assert r("rm 25") == 25.0
    assert r("25.50 RM") == 25.5
    assert r("MYR 99.00") == 99.0
    assert r("no price here") is None
    assert r("RM 99999999") is None  # absurd price rejected
