"""
DPI fragmentation profiles.

Fragmenting the TLS ClientHello is what lets a connection slip past
deep-packet inspection that matches on the handshake. The right numbers are
network-dependent and change over time — anyone claiming one fixed set works
everywhere is guessing — so these are graded by how hard they fragment, and
the panel tells the admin to test rather than promising a match for a
particular carrier.

Values are the standard `packets,length,interval` triple that v2rayNG,
sing-box and Xray clients understand.
"""

PROFILES = {
    "off": {
        "label_fa": "خاموش",
        "label_en": "Off",
        "packets": "", "length": "", "interval": "",
        "note_fa": "بدون فرگمنت. اگر شبکه‌ات محدودیتی ندارد سریع‌ترین حالت است.",
        "note_en": "No fragmentation. Fastest where nothing is inspecting.",
    },
    "light": {
        "label_fa": "سبک",
        "label_en": "Light",
        "packets": "tlshello", "length": "10-20", "interval": "10-20",
        "note_fa": "کمترین تأخیر. اول این را امتحان کنید.",
        "note_en": "Lowest overhead. Try this first.",
    },
    "balanced": {
        "label_fa": "متعادل",
        "label_en": "Balanced",
        "packets": "tlshello", "length": "40-60", "interval": "10-20",
        "note_fa": "پیش‌فرض. برای بیشتر شبکه‌ها نقطه شروع خوبی است.",
        "note_en": "The default, and a sensible starting point on most networks.",
    },
    "aggressive": {
        "label_fa": "تهاجمی",
        "label_en": "Aggressive",
        "packets": "tlshello", "length": "100-200", "interval": "10-30",
        "note_fa": "وقتی حالت‌های سبک‌تر جواب نداد. کندتر است.",
        "note_en": "When lighter settings fail. Slower.",
    },
    "packet": {
        "label_fa": "بر اساس بسته",
        "label_en": "By packet count",
        "packets": "1-3", "length": "40-60", "interval": "5-10",
        "note_fa": "به‌جای بریدن ClientHello، چند بسته اول را می‌شکند. روی بعضی شبکه‌ها بهتر جواب می‌دهد.",
        "note_en": "Splits the first few packets instead of the ClientHello. Better on some networks.",
    },
    "custom": {
        "label_fa": "دستی",
        "label_en": "Custom",
        "packets": None, "length": None, "interval": None,
        "note_fa": "مقادیر وارد شده در تنظیمات پیشرفته استفاده می‌شود.",
        "note_en": "Uses the values entered under advanced settings.",
    },
}

DEFAULT_PROFILE = "balanced"


def resolve(settings: dict) -> dict:
    """The fragment triple to put in a link, or None when it is disabled.

    "custom" defers to whatever the admin typed; every other profile ignores
    those fields so switching profiles is predictable.
    """
    settings = settings or {}
    if not settings.get("fragment_enabled", True):
        return {}

    name = settings.get("fragment_profile") or DEFAULT_PROFILE
    profile = PROFILES.get(name) or PROFILES[DEFAULT_PROFILE]

    if name == "off":
        return {}
    if name == "custom":
        packets = (settings.get("fragment_packets") or "tlshello").strip()
        length = (settings.get("fragment_length") or "10-30").strip()
        interval = (settings.get("fragment_interval") or "10-20").strip()
    else:
        packets, length, interval = profile["packets"], profile["length"], profile["interval"]

    if not (packets and length and interval):
        return {}
    return {"packets": packets, "length": length, "interval": interval}


def as_param(settings: dict) -> str:
    """The `fragment=` query value, or "" when fragmentation is off."""
    resolved = resolve(settings)
    if not resolved:
        return ""
    return f"{resolved['packets']},{resolved['length']},{resolved['interval']}"


def catalogue() -> list:
    """Profiles for the settings UI, in the order they should be offered."""
    order = ["off", "light", "balanced", "aggressive", "packet", "custom"]
    return [{"id": key, **{k: v for k, v in PROFILES[key].items()}} for key in order]
