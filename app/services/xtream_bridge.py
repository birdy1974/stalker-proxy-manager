"""
The MAC → Xtream bridge, as an operator decision (R7).

`app/portal/xtream.py` parses; this module decides. Three steps, deliberately
separated because they have very different blast radii:

1. **probe** - ask the panel for one `create_link` answer, look for account
   credentials in it, and if it has them, read `player_api.php` for the account
   state. Read-only, best-effort, and never fatal: a portal without an Xtream side
   is the normal case, so the *absence* of a bridge is stored as an observation
   ("we looked, there is nothing") and not as a fault.
2. **adopt** - the one step that changes how a user watches TV. It fetches the
   Xtream stream list, matches it to the channels we already have, writes a
   per-channel URL, and sets `Portal.xtream_adopted`. It is reachable only from an
   explicit action, and it refuses an account the panel itself reports as
   expired/banned - harvesting credentials from a MAC that is about to be cut off
   is how a proxy ends up replaying a dead account forever.
3. **detach** - flip the flag. The URLs stay: rotating them costs the panel
   requests, and an operator who wants to compare both paths should be able to
   switch back without a re-fetch.

Why the matching is conservative is documented on `plan_adoption()`; why nothing
here is automatic is documented on `Portal.xtream_adopted`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select

from ..models import LiveSource, MacAddress, Portal, VodSource
from ..portal.account import mac_is_usable
from ..portal.pool import POOL, PortalSession
from ..portal.xtream import (XtreamCreds, mask_password, parse_player_api,
                             parse_streams, plan_adoption)
from ..services.http_client import outbound_client
from .db_logging import db_log

log = logging.getLogger("spm.xtream")

#: what a stream list costs, per kind. Series are deliberately absent: an
#: episode-level adoption needs `get_series_info` per title (hundreds of
#: requests), which is a different trade than "one request, whole catalogue".
KIND_MODELS = {"live": LiveSource, "vod": VodSource}
KIND_ACTIONS = {"live": "get_live_streams", "vod": "get_vod_streams"}
#: how long between "the panel refused our harvested credentials" and asking again
REPROBE_AFTER_S = 15 * 60


# ---------------------------------------------------------------------------
# the stored observation
# ---------------------------------------------------------------------------
def dumps_observation(creds: XtreamCreds | None, account=None, *, why: str = "",
                      adopted: bool = False, extra: dict | None = None) -> str:
    """What we keep on the Portal row: the credentials *in full*, plus what they say.

    The password is stored unmasked because it has to be usable: this database
    already holds MAC addresses, portal proxies with embedded credentials and user
    passwords, and a backup that restored a portal whose Xtream password was
    `****` would be worse than no backup at all - it would be a portal that plays
    nothing and says nothing. Anything this module *returns* masks it instead.
    """
    payload: dict = {"found": bool(creds), "why": why}
    if creds is not None:
        payload["creds"] = {"base": creds.base, "username": creds.username,
                            "password": creds.password, "kind": creds.kind,
                            "auth": creds.auth,
                            "from_server_info": creds.from_server_info}
    if account is not None:
        # either an XtreamAccount or the dict an earlier observation kept: the
        # second form is why adopting twice does not re-ask player_api.php
        payload["account"] = account.public() if hasattr(account, "public") else dict(account)
    if adopted:
        payload["adopted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if extra:
        payload["adopt"] = extra
    return json.dumps(payload)


def loads_observation(text: str | None) -> dict:
    """The stored JSON, or {} for anything unparseable (a hand-edited backup)."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def creds_from_observation(obs: dict) -> XtreamCreds | None:
    c = obs.get("creds")
    if not isinstance(c, dict) or not c.get("base") or not c.get("username"):
        return None
    pw = str(c.get("password") or "")
    if pw in ("", "****"):
        return None                      # a masked backup is not a credential
    return XtreamCreds(base=str(c["base"]), username=str(c["username"]), password=pw,
                       kind=str(c.get("kind") or ""),
                       from_server_info=bool(c.get("from_server_info")),
                       auth=int(c.get("auth") or 1))


def public_observation(obs: dict) -> dict:
    """The GUI's view of the observation: no password, URLs masked."""
    creds = creds_from_observation(obs)
    out = {"found": bool(obs.get("found")), "why": str(obs.get("why") or "")}
    if creds is not None:
        out["creds"] = creds.public()
    if isinstance(obs.get("account"), dict):
        out["account"] = obs["account"]
    if obs.get("adopted_at"):
        out["adopted_at"] = obs["adopted_at"]
    return out


# ---------------------------------------------------------------------------
# the one network call of this module
# ---------------------------------------------------------------------------
async def api_request(creds: XtreamCreds, action: str = "", *, proxy: str | None = None,
                      tls_insecure: bool = False, timeout: float = 10.0):
    """`player_api.php` through the app's single trust policy.

    The proxy and the TLS flag come from the *portal row*, which matters more than
    it looks: the Xtream host is usually the same machine as the panel, so a portal
    reachable only through a proxy (or only with a self-signed chain) is unreachable
    here too. A bridge that used a bare `httpx.AsyncClient()` would work in
    development and hang in the field - which is why `dev/check-links.py` greps for
    exactly that.
    """
    params = {}
    if action:
        params["action"] = action
    url = creds.api_url(**params)
    async with outbound_client(proxy=proxy, insecure=tls_insecure,
                               timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"player_api.php answered HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        # an HTML refusal from a CDN in front of the panel; `parse_player_api`
        # turns the shape into a reason, so hand it the text rather than guess
        return resp.text[:4000]


async def _usable_mac(db, portal_id: int) -> MacAddress | None:
    """The MAC to ask the panel with: first usable one, by the panel's own word."""
    rows = (await db.execute(select(MacAddress).where(MacAddress.portal_id == portal_id)
                             .order_by(MacAddress.order))).scalars().all()
    return next((m for m in rows if mac_is_usable(m.status)), rows[0] if rows else None)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------
async def probe(db, portal: Portal, *, force: bool = False) -> dict:
    """Look for the Xtream side of this portal and store what we find.

    Best effort by contract: every failure path ends in `{"found": False, "why":
    …}`, because the two callers of this function are "press Resolve" and "press
    Check Portal", and neither is about Xtream. `why` is for a human, and it is
    never a secret: no URL with credentials in it.
    """
    if not portal.enabled:
        return {"found": False, "why": "portal is disabled", "probed": False}
    obs = loads_observation(portal.xtream)
    if obs.get("found") and not force:
        # the stored `why` is kept and annotated, not replaced: "credentials in the
        # live link" is the answer to "how did you find this", and the annotation is
        # the answer to "why did pressing it again change nothing"
        pub = public_observation(obs)
        pub["why"] = f"{pub.get('why') or 'already harvested'} - press Detect again to re-read"
        return {"found": True, "cached": True, **pub}
    mac = await _usable_mac(db, portal.id)
    if mac is None:
        return {"found": False, "why": "this portal has no MAC to ask the panel with"}

    client = await POOL.get(PortalSession.from_rows(portal, mac,
                                                    portal_url=portal.resolved_url
                                                    or portal.base_url))
    try:
        creds, why = await client.xtream_harvest()
    except Exception as exc:  # noqa: BLE001 - a probe may not break a portal check
        log.debug("xtream probe crashed: %s: %s", type(exc).__name__, exc)
        creds, why = None, f"probe failed ({type(exc).__name__})"
    finally:
        await client.close()

    account = None
    if creds is not None:
        try:
            raw = await api_request(creds, "", proxy=portal.proxy_url,
                                    tls_insecure=portal.tls_insecure)
            account = parse_player_api(raw)
        except Exception as exc:  # noqa: BLE001
            account = parse_player_api(None)
            account = replace(account, error=f"player_api.php: {type(exc).__name__}"[:120])
        if account is not None and account.base and account.base != creds.base:
            # the panel named a media host different from the one in the link. It
            # wins: some panels stream from :8080 and serve the portal on :80, and
            # a bridge that used the portal origin would write URLs that 404.
            creds = replace(creds, base=account.base, from_server_info=True)

    portal.xtream = dumps_observation(creds, account, why=why,
                                      adopted=bool(portal.xtream_adopted))
    portal.xtream_at = datetime.now(timezone.utc)
    await db.commit()
    if creds is None:
        await db_log("INFO", "portals", f"[{portal.name}] no xtream bridge: {why}")
        return {"found": False, "why": why, **public_observation(loads_observation(portal.xtream))}
    await db_log("INFO", "portals",
                 f"[{portal.name}] xtream bridge found for {creds.username}@{creds.base} "
                 f"(status {account.status if account else 'unknown'}"
                 + (f", expires {account.exp_date}" if account and account.exp_date else "")
                 + ") - press Adopt to let its links bypass the MAC path")
    return {"found": True, "why": why, **public_observation(loads_observation(portal.xtream))}


# ---------------------------------------------------------------------------
# adopt / detach
# ---------------------------------------------------------------------------
async def adopt(db, portal: Portal, *, kinds: tuple[str, ...] = ("live", "vod"),
                force: bool = False) -> dict:
    """Rewrite the matched channels' playback onto the harvested Xtream account."""
    obs = loads_observation(portal.xtream)
    creds = creds_from_observation(obs)
    if creds is None:
        return {"ok": False,
                "error": "no harvested credentials on this portal - press Detect "
                         "(or Check Portal) first"}
    account = obs.get("account") if isinstance(obs.get("account"), dict) else {}
    bad = str(account.get("status") or "")
    refused = bad == "error"
    if (bad in ("expired", "banned") or refused) and not force:
        # The panel's own word about the account outranks our convenience: an
        # expired Xtream login plays nothing, and "adopt anyway" is the escape
        # hatch for a panel that lies about its own status. `error` is in the same
        # class because it means the credentials were *refused* (or `player_api.php`
        # was unreachable) - and an unknown state is not: a DNS-locked panel says
        # nothing and plays fine, so silence must not be a reason to block adoption.
        why = "the panel would not confirm these credentials" if refused \
            else f"the Xtream account says it is {bad}"
        return {"ok": False,
                "error": f"{why}; adopt with ?force=1 to use it anyway"}
    written: dict[str, dict] = {}
    total = 0
    for kind in kinds:
        model = KIND_MODELS.get(kind)
        if model is None or not creds:
            continue
        rows = (await db.execute(select(model).where(model.portal_id == portal.id)
                                 .order_by(model.id))).scalars().all()
        if not rows:
            written[kind] = {"matched": 0, "unmatched": [], "ambiguous": [],
                             "why": "this portal has no fetched channels of this kind"}
            continue
        sources = [{"id": r.id, "number": getattr(r, "number", "") or "",
                    "name": r.original_name} for r in rows]
        try:
            raw = await api_request(creds, KIND_ACTIONS[kind], proxy=portal.proxy_url,
                                    tls_insecure=portal.tls_insecure)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{KIND_ACTIONS[kind]} failed: "
                                          f"{type(exc).__name__}", "adopted": total}
        plan = plan_adoption(sources, parse_streams(raw), creds, kind=kind)
        for row in rows:
            # every row is rewritten, including to None: a channel that no longer
            # matches must not keep playing yesterday's stream id because adopting
            # again was a no-op for it
            row.xtream_url = plan.urls.get(row.id) or None
        written[kind] = plan.public()
        total += plan.matched

    portal.xtream_adopted = True
    portal.xtream = dumps_observation(creds, account, why=str(obs.get("why") or ""),
                                      adopted=True, extra=written)
    await db.commit()
    await db_log("INFO", "portals",
                 f"[{portal.name}] adopted the xtream bridge: {total} channel(s) now play "
                 f"through {mask_password(creds.base)}/<account> instead of a MAC session")
    return {"ok": True, "adopted": total, "kinds": written,
            "portal": {"xtream_adopted": True, "username": creds.username}}


async def detach(db, portal: Portal, *, clear: bool = False) -> dict:
    """Stop using the harvested links. `clear` also drops them; by default they stay."""
    for model in KIND_MODELS.values():
        rows = (await db.execute(select(model).where(model.portal_id == portal.id)
                                 .where(model.xtream_url.isnot(None)))).scalars().all()
        if clear:
            for row in rows:
                row.xtream_url = None
    portal.xtream_adopted = False
    await db.commit()
    await db_log("INFO", "portals",
                 f"[{portal.name}] xtream adoption {'cleared' if clear else 'paused'} - "
                 f"every play goes through the MAC path again")
    return {"ok": True, "xtream_adopted": False, "cleared": clear}


def summary(portal: Portal) -> dict:
    """What the GUI needs to decide which buttons to show (no network, no DB).

    Deliberately *not* async: it reads fields already on the row, and an `async def`
    that awaits nothing is a coroutine a caller forgets to await - which in a
    template reads as `<coroutine object>` instead of raising anywhere.
    """
    obs = loads_observation(portal.xtream)
    creds = creds_from_observation(obs)
    return {"probed": portal.xtream_at is not None or bool(obs),
            "at": portal.xtream_at.isoformat() if portal.xtream_at else "",
            "found": bool(creds) and bool(obs.get("found")),
            "adopted": bool(portal.xtream_adopted),
            "username": creds.username if creds else "",
            **({"account": obs["account"]} if isinstance(obs.get("account"), dict) else {})}
