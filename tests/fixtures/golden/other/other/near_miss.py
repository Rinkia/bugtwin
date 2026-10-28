"""Near misses: none of these may be reported."""

from mesh.config import ALLOW  # cross-module constant: out of v0.1 scope


def already_fixed(d):
    return d.get("default", "deny")


def different_call(d):
    return d.setdefault("default", "allow")


def different_function(x):
    return get_mode("allow")


def wrong_slot(x):
    return x.get("allow")


def other_fallback(d):
    return d.get("k") or "deny"


def imported_constant(d):
    return d.get("k", ALLOW)


def just_a_string():
    print("allow")


def get_mode(v):
    return v
