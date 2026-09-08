from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse


def is_safe_web_url(url: str) -> bool:
    """Validate that a URL is safe for browser navigation.

    Enforces:
    1. Only http and https schemes.
    2. No embedded user/password credentials.
    3. Hostname cannot be localhost, loopback, private (RFC1918), link-local, or local domain.
    4. Rejects raw IP addresses (IPv4/IPv6, decimal, or hex formats) where a domain is expected.
    5. Hostname must be a syntactically valid domain with at least two labels and valid characters.
    """
    if not isinstance(url, str) or not url.strip():
        return False

    trimmed = url.strip()
    try:
        parsed = urlparse(trimmed)
    except Exception:
        return False

    # 1. Reject non-http/https schemes (e.g., javascript:, file:, data:, ftp:)
    if not parsed.scheme or parsed.scheme.lower() not in {"http", "https"}:
        return False

    # 2. Reject credentials embedded in URL (e.g., https://user:pass@example.com)
    if parsed.username is not None or parsed.password is not None:
        return False

    # Ensure netloc and hostname exist
    if not parsed.netloc:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    hostname = hostname.lower().strip(".")

    # 3. Reject localhost and local network suffixes
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    if hostname.endswith(".local") or hostname.endswith(".internal") or hostname.endswith(".lan"):
        return False

    # 4. Reject raw IP addresses (both standard and alternate representations)
    # Check standard IPv4 / IPv6 parsing
    try:
        ip = ipaddress.ip_address(hostname.strip("[]"))
        # Any raw IP address is rejected where a domain is expected
        return False
    except ValueError:
        pass

    # Check for integer/hex representation (e.g., http://2130706433 or http://0x7f.1)
    if hostname.isdigit() or hostname.startswith("0x"):
        return False

    # Check for IPv4-like numeric octets even if malformed or partially octal/hex
    if re.fullmatch(r"[0-9a-fA-FxX.:]+", hostname):
        return False

    # 5. Hostname must be a valid multi-label domain name
    labels = hostname.split(".")
    if len(labels) < 2:
        return False

    for label in labels:
        if not label:
            return False
        # Each label must be alphanumeric with hyphens, not starting or ending with hyphen
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            return False

    return True
