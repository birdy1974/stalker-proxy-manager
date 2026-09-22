"""
Per-MAC channel-id translations: the explicit option that makes MACs with
differently-numbered ids usable as fallbacks of ONE custom channel.

A shared-login reseller renumbers channels per account, so "BBC One" is id 10
on the fetch MAC and id 99 on its sibling. `links.plan_for` hands every MAC of
a chain ONE cmd, so without this table the second MAC asks for the wrong id
(`nothing_to_play` / the wrong channel). The Compare popup's Channel IDs tab
(tab, not a silent fix — the user asked for an option) surfaces the conflict
and its Save button lands here: one row per live source × MAC holding THAT
mac's id and cmd, read at playback as the plain `src.mac_cmd_overrides`
attribute that plan_for consults.

Deliberate edges, each pinned by tests/test_channel_translations.py:

* imports only database + models — stream_manager (attach) and mac_health
  (the compare count) both import THIS module, so anything heavier here
  would close an import cycle;
* save() matches a compare row to sources by id first and falls back to a
  UNIQUE normalized name: dual-fetch duplicates are found by id, a stale id
  still finds the channel by name, ambiguous names are refused;
* a pair equal to the source's stored (id, cmd) is `unchanged` and leaves NO
  row — the fetch MAC's view IS the stored row, and a leftover row from an
  earlier numbering would shadow a fresh fetch;
* only MACs present in the row's `ids` are touched: absent MACs keep
  whatever they had.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select

from ..database import SessionLocal
from ..models import ChannelIdTranslation, LiveSource, MacAddress, Portal


def _norm_channel_name(name) -> str:
    """Channel identity across MACs: casefolded, whitespace-collapsed name.

    A verbatim copy of mac_health._norm_channel_name — importing THAT would
    drag mac_health (and through it stream_manager) into a cycle: stream_manager
    imports this module for attach_overrides.
    """
    return " ".join((name or "").casefold().split())


async def _count(s, portal_id: int) -> int:
    """Rows for this portal, joined through their live source."""
    n = await s.scalar(
        select(func.count(ChannelIdTranslation.id))
        .select_from(ChannelIdTranslation)
        .join(LiveSource, LiveSource.id == ChannelIdTranslation.live_source_id)
        .where(LiveSource.portal_id == portal_id))
    return int(n or 0)


async def count_translations(portal_id: int) -> int:
    """How many translations this portal has (the tab's "N saved" badge)."""
    async with SessionLocal() as s:
        return await _count(s, portal_id)


async def attach_overrides(entries, *, session=None) -> None:
    """Set `src.mac_cmd_overrides = {mac_id: cmd}` on every LiveSource in `entries`.

    `entries` are chain tuples `(src, portal, macs)` — exactly the shapes
    _live_chain, _open_preview and probe_mac build. ONE query covers all
    sources × MACs. The result is a plain attribute, never a mapped column:
    chain rows outlive the session that loaded them, and a column would be
    expired (or raise) once that session closes.

    Only LiveSource instances count: a VodSource/SerieSource carries an
    unrelated integer PK, and querying translations by it would silently
    attach ANOTHER table's rows to a vod/series preview. Non-LiveSource
    entries (test stubs, `_WithTemplate`) make the whole call a no-op with
    zero DB traffic.
    """
    pairs: list[tuple[object, list[int]]] = []
    for src, _portal, macs in entries:
        if not isinstance(src, LiveSource):
            continue
        mids = [mid for mid in (getattr(m, "id", None) for m in (macs or []))
                if mid is not None]
        pairs.append((src, mids))
    if not pairs:
        return
    src_ids = {src.id for src, _ in pairs if src.id is not None}
    all_mids = {mid for _, mids in pairs for mid in mids}

    async def run(s) -> None:
        rows = []
        if src_ids and all_mids:
            rows = (await s.execute(
                select(ChannelIdTranslation).where(
                    ChannelIdTranslation.live_source_id.in_(src_ids),
                    ChannelIdTranslation.mac_id.in_(all_mids)))).scalars().all()
        by_src: dict[int, dict[int, str]] = {}
        for r in rows:
            by_src.setdefault(r.live_source_id, {})[r.mac_id] = r.cmd
        for src, mids in pairs:
            table = by_src.get(src.id) or {}
            # plain dict per THIS entry's macs — {} when nothing was saved
            src.mac_cmd_overrides = {mid: table[mid] for mid in mids if mid in table}

    if session is not None:
        await run(session)
    else:
        async with SessionLocal() as s:
            await run(s)


async def save(portal_id: int, rows: list) -> dict:
    """Upsert translations from Compare Channel-IDs rows (live only — the
    endpoint refuses any `kind` but "live").

    Each row is `{key, name, status, ids: {mac: id}, cmds: {mac: cmd}}`.
    Returns `{ok, saved, unchanged, skipped:{ambiguous, unmatched,
    unknown_mac, empty_cmd}, count}` where `count` is the portal's total
    AFTER this write. `unknown portal` comes back as `{ok: False, error}`
    so the endpoint can map it to 404.
    """
    async with SessionLocal() as s:
        if not await s.get(Portal, portal_id):
            return {"ok": False, "error": "portal not found"}
        macs = {m.mac: m.id for m in (await s.execute(
            select(MacAddress).where(MacAddress.portal_id == portal_id))).scalars()}
        srcs = (await s.execute(
            select(LiveSource.id, LiveSource.portal_channel_id,
                   LiveSource.original_name, LiveSource.cmd)
            .where(LiveSource.portal_id == portal_id))).all()
        by_cid = {str(r.portal_channel_id): r for r in srcs}
        by_name: dict[str, list] = {}
        for r in srcs:
            by_name.setdefault(_norm_channel_name(r.original_name), []).append(r)

        saved = unchanged = 0
        skipped = {"ambiguous": 0, "unmatched": 0, "unknown_mac": 0, "empty_cmd": 0}
        writes: dict[tuple[int, int], tuple[str, str]] = {}
        drops: set[tuple[int, int]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            ids = {str(k): str(v) for k, v in (row.get("ids") or {}).items()}
            cmds = {str(k): str(v) for k, v in (row.get("cmds") or {}).items()}
            key = _norm_channel_name(row.get("key") or row.get("name") or "")
            # id-hits first (they pin the right row even for dual-fetch
            # duplicates); a UNIQUE normalized-name match only fills the gap
            # left by a stale/renumbered id.
            targets: dict[int, object] = {}
            for cid in set(ids.values()):
                hit = by_cid.get(cid)
                if hit is not None:
                    targets[hit.id] = hit
            name_hits = by_name.get(key, []) if key else []
            if len(name_hits) == 1:
                targets.setdefault(name_hits[0].id, name_hits[0])
            if not targets:
                skipped["ambiguous" if len(name_hits) > 1 else "unmatched"] += 1
                continue
            for t in targets.values():
                for mac_str, cid in ids.items():
                    mac_id = macs.get(mac_str)  # exact DB mac.mac
                    if mac_id is None:
                        skipped["unknown_mac"] += 1
                        continue
                    cmd = cmds.get(mac_str) or ""
                    if not cmd.strip():
                        skipped["empty_cmd"] += 1
                        continue
                    cid = str(cid)
                    if cid == str(t.portal_channel_id) and cmd == str(t.cmd or ""):
                        # this MAC's view IS the stored row: needs no row, and
                        # any leftover from an earlier numbering must go
                        unchanged += 1
                        drops.add((t.id, mac_id))
                        continue
                    writes[(t.id, mac_id)] = (cid, cmd)

        # Deletes flush BEFORE inserts: the old row still occupies the unique
        # (live_source_id, mac_id) until its DELETE has run.
        if drops:
            existing_drops = {(r.live_source_id, r.mac_id): r for r in (await s.execute(
                select(ChannelIdTranslation).where(
                    ChannelIdTranslation.live_source_id.in_({k[0] for k in drops}),
                    ChannelIdTranslation.mac_id.in_({k[1] for k in drops})))).scalars()}
            for keypair in drops:
                row = existing_drops.get(keypair)
                if row is not None:
                    await s.delete(row)
            await s.flush()
        if writes:
            existing = {(r.live_source_id, r.mac_id): r for r in (await s.execute(
                select(ChannelIdTranslation).where(
                    ChannelIdTranslation.live_source_id.in_({k[0] for k in writes}),
                    ChannelIdTranslation.mac_id.in_({k[1] for k in writes})))).scalars()}
            for keypair, (cid, cmd) in writes.items():
                row = existing.get(keypair)
                if row is None:
                    s.add(ChannelIdTranslation(live_source_id=keypair[0],
                                               mac_id=keypair[1],
                                               portal_channel_id=cid, cmd=cmd))
                else:
                    row.portal_channel_id, row.cmd = cid, cmd
                saved += 1
            await s.commit()
        elif drops:
            await s.commit()
        return {"ok": True, "saved": saved, "unchanged": unchanged,
                "skipped": skipped, "count": await _count(s, portal_id)}


async def clear(portal_id: int) -> dict:
    """Drop every translation of this portal (the tab's Clear button)."""
    async with SessionLocal() as s:
        if not await s.get(Portal, portal_id):
            return {"ok": False, "error": "portal not found"}
        res = await s.execute(
            delete(ChannelIdTranslation).where(
                ChannelIdTranslation.live_source_id.in_(
                    select(LiveSource.id).where(LiveSource.portal_id == portal_id))))
        await s.commit()
        return {"ok": True, "cleared": int(res.rowcount or 0)}
