"""Transaction-safe playlist order allocation and stable legacy tie repair.

Locks live in the database, not an asyncio.Lock: multiple app workers must use
one allocator. All writers acquire the lock before reading playlist positions
and keep it until commit/rollback. Order includes disabled rows, so re-enabling
an item cannot collide with a position assigned while it was switched off.
"""
from sqlalchemy import distinct, false, func, select, update

from ..models import LivePlaylist, LocalPlaylist, SeriePlaylist, VodPlaylist

ORDER_MODELS = {'live': LivePlaylist, 'vod': VodPlaylist,
                'series': SeriePlaylist, 'local': LocalPlaylist}
_LOCK_IDS = {model: i for i, model in enumerate(ORDER_MODELS.values(), 1)}


async def lock_playlist_order(db, model) -> None:
    """Serialize writers until their transaction ends (also for empty tables)."""
    transaction = db.sync_session.get_transaction()
    held = db.info.setdefault('playlist_order_locks', {})
    if transaction is not None and held.get(model) is transaction:
        return
    # Do not flush pending source toggles before taking the shared lock; another
    # writer may be holding it while waiting to update those same sources.
    with db.no_autoflush:
        if db.get_bind().dialect.name == 'postgresql':
            # Fixed namespace/keys, stable across processes and Python runs.
            await db.execute(select(func.pg_advisory_xact_lock(0x53504D, _LOCK_IDS[model])))
        else:
            # SQLite has one writer. Even a zero-row UPDATE acquires its write
            # reservation, without changing rows or requiring a lock table.
            await db.execute(update(model).where(false()).values(order=model.order)
                             .execution_options(synchronize_session=False))
    held[model] = db.sync_session.get_transaction()


async def repair_playlist_order(db, model) -> int:
    """Repair ties/nonpositive positions, preserving the (order, id) sequence.

    Valid positive orders (including gaps/custom values) are left alone. Returns
    the number of changed rows. Does not touch Live channel numbers or locks.
    """
    await lock_playlist_order(db, model)
    count, unique, minimum = (await db.execute(select(
        func.count(model.id), func.count(distinct(model.order)), func.min(model.order)))).one()
    if not count or (unique == count and minimum is not None and minimum > 0):
        return 0
    rows = (await db.scalars(select(model).order_by(model.order, model.id))).all()
    changed = 0
    for order, row in enumerate(rows, 1):
        if row.order != order:
            row.order = order
            changed += 1
    await db.flush()
    return changed


async def next_playlist_order(db, model) -> int:
    """Append after all existing rows, after repairing any legacy ties."""
    await repair_playlist_order(db, model)
    return (await db.scalar(select(func.max(model.order))) or 0) + 1
