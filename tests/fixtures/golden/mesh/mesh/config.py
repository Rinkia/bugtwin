"""Drifted twins: same fail-open default, different spelling (Tier 2)."""

ALLOW = "allow"


def mode(settings):
    return settings.get("mode") or "allow"


def on_error(d):
    return d.get("on_error", "allow")


def fallback(p):
    return p.get("fallback", ALLOW)
