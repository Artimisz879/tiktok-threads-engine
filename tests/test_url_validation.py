"""URL validation + SSRF protection tests."""
import pytest

from app.security.url_validation import (
    URLValidationError,
    is_allowed_url,
    validate_public_host,
)


class TestUrlAllowlist:
    def test_tiktok_short_link_allowed(self):
        assert is_allowed_url("https://vt.tiktok.com/xyz123/")

    def test_tiktok_shop_allowed(self):
        assert is_allowed_url("https://shop.tiktok.com/product/123")

    def test_tiktokv_allowed(self):
        assert is_allowed_url("https://www.tiktokv.com/v/123456")

    def test_shopee_my_allowed(self):
        assert is_allowed_url("https://shopee.com.my/product/123")

    def test_lazada_my_allowed(self):
        assert is_allowed_url("https://www.lazada.com.my/products/123")

    def test_temu_allowed(self):
        assert is_allowed_url("https://www.temu.com/goods.html?g=123")

    def test_google_blocked(self):
        assert not is_allowed_url("https://google.com/search?q=fan")

    def test_evil_tiktokcom_evil_blocked(self):
        # Lookalike domain must be blocked.
        assert not is_allowed_url("https://tiktok.com.evil.com/steal")

    def test_subdomain_of_evil_blocked(self):
        assert not is_allowed_url("https://evil.tiktok.com.attacker.com/x")

    def test_non_http_blocked(self):
        assert not is_allowed_url("ftp://vt.tiktok.com/x")
        assert not is_allowed_url("javascript:alert(1)")

    def test_empty_blocked(self):
        assert not is_allowed_url("")
        assert not is_allowed_url("not a url")

    def test_http_allowed(self):
        # http (not just https) is allowed for short-link resolvers.
        assert is_allowed_url("http://vt.tiktok.com/xyz")


class TestSSRF:
    def test_localhost_blocked(self):
        with pytest.raises(URLValidationError):
            validate_public_host("localhost")

    def test_private_ip_blocked(self):
        with pytest.raises(URLValidationError):
            validate_public_host("192.168.1.10")

    def test_loopback_blocked(self):
        with pytest.raises(URLValidationError):
            validate_public_host("127.0.0.1")

    def test_link_local_blocked(self):
        with pytest.raises(URLValidationError):
            validate_public_host("169.254.169.254")  # cloud metadata!

    def test_public_ip_ok(self):
        validate_public_host("1.1.1.1")

    def test_public_domain_ok(self):
        validate_public_host("vt.tiktok.com")

    def test_internal_suffix_blocked(self):
        with pytest.raises(URLValidationError):
            validate_public_host("router.local")
