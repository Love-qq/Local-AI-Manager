import ssl
import urllib.request

from lamcore.tls import create_ssl_context, https_handler
from lamcore.huggingface import build_opener


def test_ssl_context_keeps_certificate_and_hostname_verification_enabled():
    context = create_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_https_handler_uses_verified_context():
    handler = https_handler()
    assert isinstance(handler, urllib.request.HTTPSHandler)
    assert handler._context.verify_mode == ssl.CERT_REQUIRED
    assert handler._context.check_hostname is True


def test_huggingface_opener_contains_verified_https_handler():
    opener = build_opener()
    handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert handlers
    assert all(h._context.verify_mode == ssl.CERT_REQUIRED for h in handlers)
    assert all(h._context.check_hostname for h in handlers)
