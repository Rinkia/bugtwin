"""Gate-style policy loader. The golden fix flips the fail-open default below."""


class GatePolicy:
    def __init__(self, default, allow):
        self.default = default
        self.allow = allow


def from_dict(obj):
    return GatePolicy(
        default=obj.get("default", "allow"),
        allow=frozenset(obj.get("allow", []) or []),
    )
