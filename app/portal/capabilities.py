"""
What a portal *has*, as opposed to what it answers (R6).

Two questions this module answers, both from text a panel sends without being
asked nicely:

  * **which build is this?** ``version.js`` is a static file in the portal
    directory, it needs no token, no MAC and no handshake - so it can be read
    while the portal is still being resolved, and it is the single most useful
    line to have in a bug report ("Ministra 5.4.2" explains a lot of quirks).
  * **does it even have the thing I am about to spend 30 minutes fetching?**
    ``type=stb&action=get_modules`` answers with the modules the panel offers
    and the ones it switched off. A portal without ``sclub`` has no series;
    queueing the series half of a fetch job there used to produce a green
    progress bar and an empty catalogue.

Everything here is pure string work on a panel's answer, which is what makes it
worth pinning in ``dev/check-links.py``: the shapes below come from real
Ministra panels, not from a specification.

The one rule that must not be broken, and the reason ``supports()`` has three
answers instead of two: **silence is not information**. A panel that 404s
``get_modules`` does not "have no modules", it has told us nothing, and gating a
feature off that answer would hide catalogues that exist. Unknown is its own
value here, and it is the default.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass

from .identity import referer_for

log = logging.getLogger("spm.portal")

# --------------------------------------------------------------------------
# version.js
# --------------------------------------------------------------------------
# `version.js` is not standardised: some panels assign one variable, some write
# an object, some only mention the version inside the `ver` advertisement they
# also use for `get_profile`. All four shapes are tried, in order of how much
# they can be trusted to be the *portal* version.
_VERSION_PATTERNS = (
    # the classic: `var ver = '5.4.2';`  (what stbapp reads)
    re.compile(r"""\bver\s*=\s*['"]([^'"]+)['"]""", re.I),
    # Ministra 5.4+: `portal_version: "5.4.2"`, `"PORTAL version": 5.4.2`
    re.compile(r"""portal[_ ]?version['"]?\s*[:=]\s*['"]?([0-9][^'"\s;,]*)""", re.I),
    # `xpcom.version = { portal: '5.4.2', image: ... }`
    re.compile(r"""\bportal\s*['"]?\s*:\s*['"]([0-9][^'"]*)['"]""", re.I),
    # last resort: `PORTAL version: 5.3.0;` inside a ver block
    re.compile(r"""PORTAL version:\s*([0-9][^;'"\s]*)""", re.I),
)

# Image/build lines are worth reading too: "the panel is new, the image is from
# 2019" is a real diagnosis for a set of link bugs.
_IMAGE_PATTERNS = (
    re.compile(r"""ImageDescription\s*[:=]\s*['"]?([^'";\n]+)""", re.I),
    re.compile(r"""\bimage[_ ]?version['"]?\s*[:=]\s*['"]?([0-9][^'"\s;,]*)""", re.I),
)

_MINISTRA = re.compile(r"\b(ministra|iversa|vision\d+)\b", re.I)


def _first(rx: re.Pattern, text: str) -> str:
    m = rx.search(text)
    return (m.group(1) or "").strip() if m else ""


@dataclass(frozen=True)
class PortalVersion:
    """What `version.js` said. Empty fields mean it did not say them."""

    raw: str = ""
    portal: str = ""
    image: str = ""
    product: str = ""
    error: str = ""      # why we have nothing, for the resolve log

    @property
    def known(self) -> bool:
        """True only when we can name the panel. `raw` alone is not knowledge."""
        return bool(self.portal or self.image)

    @property
    def label(self) -> str:
        """One-line human version for the GUI, the log and a bug report."""
        bits = []
        if self.product:
            bits.append(self.product.title())
        if self.portal:
            bits.append(f"portal {self.portal}")
        if self.image:
            bits.append(f"image {self.image}")
        if not bits:
            return self.raw
        return " ".join(bits)

    def public(self) -> dict:
        return {"raw": self.raw, "portal": self.portal, "image": self.image,
                "product": self.product, "error": self.error, "label": self.label}


def parse_version_js(text: str | None) -> PortalVersion:
    """Read a portal's `version.js`. Never raises: an unreadable one is common."""
    body = str(text or "").strip()
    if not body:
        return PortalVersion()
    # A captive portal, a WAF or a router login page answers this URL instead of
    # the panel far more often than you would think, and such a page contains
    # `ver`-looking fragments inside its scripts. Rejecting the shape beats
    # printing 200 bytes of HTML as "the portal version", which is what a
    # grep-for-a-number parser does on that page.
    head = body[:600].lower().lstrip()
    if head.startswith(("<!doctype", "<html", "<head", "<body", "<?xml")):
        return PortalVersion(error="version.js answered HTML (captive portal, WAF, or a "
                                   "redirected request)")
    portal = ""
    for rx in _VERSION_PATTERNS:
        portal = _first(rx, body)
        if portal:
            break
    image = ""
    for rx in _IMAGE_PATTERNS:
        image = _first(rx, body)
        if image:
            break
    product = _first(_MINISTRA, body).lower()
    if not portal and not image:
        return PortalVersion(error="no version in version.js", raw=body[:120])
    return PortalVersion(raw=body[:200], portal=portal[:40], image=image[:80],
                         product=product[:20])


def version_js_url(portal_url: str) -> str:
    """Where a panel keeps `version.js`: next to its `index.html`.

    Derived from the same directory logic the `Referer` uses, because it is the
    same fact - the file the box loads from the portal's own base path - and a
    second copy of that path maths is how a `/stalker_portal/c/` panel ends up
    probed at `/c/`.
    """
    ref = referer_for(portal_url)
    if not ref:
        return ""
    return ref[: -len("index.html")] + "version.js"


async def read_version_js(http, portal_url: str) -> PortalVersion:
    """One cosmetic GET. It never raises and never costs a retry.

    Cosmetic matters: this runs while the portal is still being *resolved*, so
    a slow panel must not hold up finding its endpoint - hence the short
    timeout, and hence the error being a field instead of an exception.
    """
    url = version_js_url(portal_url)
    if not url:
        return PortalVersion(error="no portal directory to look in")
    try:
        resp = await http.get(url, timeout=3.0)
    except Exception as exc:  # noqa: BLE001 - a version probe must not fail a resolve
        return PortalVersion(error=f"{type(exc).__name__}")
    if resp.status_code >= 400:
        return PortalVersion(error=f"http_{resp.status_code}")
    version = parse_version_js(resp.text)
    if version.known:
        return version
    # Keep whatever reason the parser gave: "no version in version.js" and "this
    # is HTML, probably a captive portal" are different diagnoses, and the second
    # one is the only clue that something is intercepting us. `PortalVersion` is
    # frozen - a value that can be cached and handed around without anyone
    # quietly editing it - so the annotation is a new instance, not an assignment.
    return dataclasses.replace(version, error=version.error or "no version in version.js",
                               raw=resp.text[:120])


# --------------------------------------------------------------------------
# get_modules
# --------------------------------------------------------------------------
# The panel's vocabulary is the STB menu's: `tv` (live), `vclub` (VOD),
# `sclub` (series), `epg`, `tv_archive`/`captured_tv_archive` (timeshift),
# plus infrastructure modules we only display.
FEATURE_MODULES: dict[str, tuple[str, ...]] = {
    "live": ("tv", "itv"),
    "vod": ("vclub", "vod"),
    "series": ("sclub", "series"),
    "epg": ("epg",),
    "archive": ("tv_archive", "captured_tv_archive"),
}

# Only the five features above gate anything. `portal_time`, `watchdog`, `dns`,
# `recorder` and the rest of a panel's module list are *displayed* (the GUI shows
# the whole enabled list in a tooltip) but gate nothing: refusing to serve live TV
# because `recorder` is off would be a bug, not a policy.


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _on(value) -> bool:
    return str(value if value is not None else "").strip().lower() in _TRUTHY


def _names(value) -> list[str]:
    """Module names out of whatever shape the panel used."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, dict) and "status" in val:
                # A panel that marks a module off *inside* `all_modules` has not
                # lost it, it has switched it off. `!name` says so in a form the
                # caller folds into `disabled_modules`, so the module is neither
                # gated on nor silently missing from what we show the GUI.
                out.append(str(key) if _on(val.get("status")) else f"!{key}")
            # {tv: {title: ...}} (no status: present means offered) and {'tv': 1}
            elif isinstance(val, (dict, list)) or _on(val):
                out.append(str(key))
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return out
        if text[0] in "[{":
            try:
                return _names(json.loads(text))
            except ValueError:
                pass
        return [p.strip() for p in re.split(r"[,\s]+", text) if p.strip()]
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("module") or item.get("id") or ""
                # a listed-but-disabled entry: {"name":"vod","status":"0"}
                status = str(item.get("status", item.get("enabled", 1))).strip().lower()
                if name:
                    out.append(f"!{name}" if status in ("0", "false", "no", "off",
                                                        "disabled") else str(name))
            elif item is not None:
                out.append(str(item))
    return out


def parse_modules(payload) -> tuple[list[str], list[str]]:
    """(offered, disabled) module names from a `get_modules` answer.

    Accepts the whole response or its `js` member, a dict or a list of dicts,
    and a `!name` marker for entries that carry their own status - which is how
    some panels report a disabled module instead of using `disabled_modules`.
    """
    data = payload
    if isinstance(data, dict) and "js" in data:
        data = data["js"]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        return [], []
    offered = _names(data.get("all_modules") or data.get("modules")
                     or data.get("enabled_modules"))
    disabled = [n for n in _names(data.get("disabled_modules"))]
    # entries that marked themselves off belong in `disabled`, wherever they came
    merged_offered, seen = [], set()
    for name in offered:
        if name.startswith("!"):
            if name[1:] not in seen:
                seen.add(name[1:])
                disabled.append(name[1:])
            continue
        if name not in seen:
            seen.add(name)
            merged_offered.append(name)
    return merged_offered, sorted(set(disabled) | {n[1:] for n in offered if n.startswith("!")})


def enabled_modules(payload) -> list[str] | None:
    """The modules this panel offers *and* has switched on. None = it did not say.

    An empty `all_modules` is treated as "did not say" on purpose: a panel with
    no modules at all cannot serve anything, so a reply that claims it is
    almost certainly a half-implemented endpoint, and gating on it would hide a
    working portal.
    """
    offered, disabled = parse_modules(payload)
    if not offered:
        return None
    off = {d.lower() for d in disabled}
    return [m for m in offered if m.lower() not in off]


def supports(modules: list[str] | None, feature: str) -> bool | None:
    """Does the panel have `feature`? None means it never told us."""
    if modules is None:
        return None
    have = {str(m).lower() for m in modules}
    wanted = FEATURE_MODULES.get(feature)
    if not wanted:
        return None
    return any(w in have for w in wanted)


def gate_feature(modules: list[str] | None, feature: str) -> tuple[bool, str]:
    """(run it?, why-not) - the form a caller can log.

    Unknown panels always run: the cost of a needless fetch is a few minutes,
    the cost of skipping a real catalogue is "this proxy lost my channels".
    """
    ok = supports(modules, feature)
    if ok is False:
        names = "/".join(FEATURE_MODULES.get(feature) or ())
        return False, (f"the panel says it has no {names} module "
                       f"(get_modules offered: {', '.join(modules) or 'nothing'})")
    return True, ""


def modules_label(modules: list[str] | None) -> str:
    return ", ".join(modules) if modules else ""


def dumps_modules(modules: list[str] | None) -> str | None:
    """How the answer is kept on the Portal row. None stays NULL, deliberately:
    "the panel has no modules" and "the panel never answered" must not share a
    value, or a portal that was briefly unreachable loses its catalogues in the
    GUI and in every fetch job until someone re-resolves it."""
    return json.dumps(sorted(modules)) if modules else None


def loads_modules(text: str | None) -> list[str] | None:
    """The inverse, tolerant by necessity: a hand-edited backup can hold junk."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list) or not data:
        return None
    return [str(m) for m in data]
