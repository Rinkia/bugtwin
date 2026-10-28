"""Copy-pasted loader: same shape as the fixed line (Tier 1 twin)."""


def tool_default(cfg):
    return cfg.get("default", "allow")


def tool_names(cfg):
    return list(cfg.get("tools", []))
