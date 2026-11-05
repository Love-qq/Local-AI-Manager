"""TLS helpers for outbound HTTPS requests.

The context trusts both the operating system certificate store and the
Mozilla CA bundle shipped by certifi. Certificate verification stays enabled.
"""
from __future__ import annotations

import ssl
import urllib.request


def create_ssl_context() -> ssl.SSLContext:
    """Return a verified TLS context with system and certifi roots."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        # The OS/Python trust store remains available when certifi is absent.
        pass
    except (OSError, ssl.SSLError):
        # Do not disable verification if the optional bundle is unavailable.
        # Keep the system trust store and let invalid chains fail closed.
        pass
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def https_handler() -> urllib.request.HTTPSHandler:
    """Build an urllib HTTPS handler using the verified combined trust store."""
    return urllib.request.HTTPSHandler(context=create_ssl_context())
