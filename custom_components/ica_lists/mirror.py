"""Mirror a Home Assistant to-do list (e.g. Google Keep) into an ICA list.

Items added, renamed, ticked off or removed in the Home Assistant list are
done in ICA too. Items ticked off (or removed) in the ICA app are ticked off
in the Home Assistant list. Optionally, items added in the ICA app are copied
to the Home Assistant list. The rules live in sync_plan.py; this file reads
both lists, carries out the plan and remembers the pairs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import sync_plan as sp
from .const import (
    DOMAIN,
    IMPORT_ATTEMPTS,
    IMPORT_TIMEOUT,
    ISSUE_MIRROR_LIST_MISSING,
    MASS_DELETE_HOLD,
    MIRROR_DEBOUNCE,
    MIRROR_POLL,
    STORAGE_VERSION,
    mirror_store_key,
)
from .ica_api import IcaApiError, IcaError, IcaList, IcaRow

if TYPE_CHECKING:
    from .coordinator import IcaConfigEntry, IcaCoordinator
    from .debuglog import DebugLog

_LOGGER = logging.getLogger(__name__)

TODO = "todo"


class IcaMirror:
    """Keeps one Home Assistant to-do list and one ICA list in step."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IcaConfigEntry,
        coordinator: IcaCoordinator,
        debug: DebugLog,
        source: str,
        list_id: str,
        import_new: bool,
    ) -> None:
        self.hass = hass
        self.source = source
        self.list_id = list_id
        self._entry = entry
        self._coordinator = coordinator
        self._client = coordinator.client
        self._debug = debug
        self._import = import_new
        self._pair = f"{source}|{list_id}"
        self._issue_id = f"{ISSUE_MIRROR_LIST_MISSING}_{entry.entry_id}"
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, mirror_store_key(entry.entry_id)
        )
        self._links: dict[str, sp.Link] = {}
        self._pending: dict[str, float] = {}  # ICA row id -> when it was copied to HA
        self._attempts: dict[str, int] = {}  # ICA row id -> copy attempts
        self._hold_since: float | None = None
        self._lock = asyncio.Lock()
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=MIRROR_DEBOUNCE,
            immediate=False,
            function=self.async_sync,
        )
        self._unsubs: list[Callable[[], None]] = []
        self._warned: set[str] = set()
        self._started = False
        self._stopped = False
        self.last_sync: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        if data.get("pair") == self._pair:
            for uid, raw in (data.get("links") or {}).items():
                if (link := sp.Link.from_dict(raw)) is not None:
                    self._links[str(uid)] = link
            self._pending = _float_map(data.get("pending"))
            self._attempts = {k: int(v) for k, v in _float_map(data.get("attempts")).items()}
        self._unsubs += [
            async_track_state_change_event(self.hass, [self.source], self._on_source_change),
            self._coordinator.async_add_listener(self._schedule),
            async_track_time_interval(
                self.hass,
                self._on_timer,
                timedelta(seconds=MIRROR_POLL),
                name=f"{DOMAIN} mirror",
            ),
            async_at_started(self.hass, self._on_started),
        ]

    async def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._debouncer.async_cancel()
        await self._store.async_save(self._data())

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._started = True
        self._schedule()

    @callback
    def _on_source_change(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule()

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._started and not self._stopped:
            self._debouncer.async_schedule_call()

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "pairs": len(self._links),
            "waiting_imports": len(self._pending),
            "holding_removals": self._hold_since is not None,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
        }

    # ---------------------------------------------------------------- sync
    async def async_sync(self) -> None:
        if self._stopped or not self._started:
            return
        async with self._lock:
            try:
                await self._sync()
            except Exception:
                _LOGGER.exception("ICA mirror: sync failed")
                self._debug.add("mirror", "sync failed unexpectedly (see the Home Assistant log)")

    async def _sync(self) -> None:
        ica = self._ica_list()
        source = await self._source_items() if ica is not None else None
        if ica is None or source is None:
            return
        self._tidy(ica)
        plan = self._plan(source, ica)
        if plan.writes_ica:
            # About to change ICA: act on fresh data from both sides.
            await self._coordinator.async_refresh()
            ica = self._ica_list()
            source = await self._source_items() if ica is not None else None
            if ica is None or source is None:
                return
            plan = self._plan(source, ica)
        await self._execute(plan, ica)
        self.last_sync = dt_util.utcnow()
        self._store.async_delay_save(self._data, 5)

    def _ica_list(self) -> IcaList | None:
        if not self._coordinator.last_update_success or not self._coordinator.data:
            return None
        lst = self._coordinator.data.get(self.list_id)
        if lst is None:
            if "list" not in self._warned:
                self._warn("list", "the ICA list to mirror into is gone; pick another one")
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_MIRROR_LIST_MISSING,
                    translation_placeholders={"source": self.source},
                )
            return None
        if "list" in self._warned:
            self._warned.discard("list")
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return lst

    async def _source_items(self) -> list[sp.SourceItem] | None:
        state = self.hass.states.get(self.source)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._warn("source", f"{self.source} is unavailable, waiting for it")
            return None
        try:
            response = await self.hass.services.async_call(
                TODO,
                "get_items",
                {},
                target={"entity_id": self.source},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            self._warn("source", f"couldn't read {self.source}: {err}")
            return None
        self._warned.discard("source")
        entity_response = (response or {}).get(self.source)
        items = entity_response.get("items") if isinstance(entity_response, dict) else None
        result: list[sp.SourceItem] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("uid") or item.get("summary") is None:
                continue
            result.append(
                sp.SourceItem(
                    uid=str(item["uid"]),
                    summary=str(item["summary"]),
                    done=item.get("status") == "completed",
                )
            )
        return result

    def _plan(self, source: list[sp.SourceItem], ica: IcaList) -> sp.Plan:
        items = [sp.IcaItem(id=row.id, text=row.text, done=row.striked) for row in ica.rows]
        return sp.plan(
            source,
            items,
            self._links,
            import_new=self._import,
            pending_imports=set(self._pending),
        )

    def _tidy(self, ica: IcaList) -> None:
        """Forget import bookkeeping for rows that are gone or waited too long."""
        now = time.time()
        rows = {row.id for row in ica.rows}
        self._pending = {
            row_id: since
            for row_id, since in self._pending.items()
            if row_id in rows and now - since < IMPORT_TIMEOUT
        }
        self._attempts = {k: v for k, v in self._attempts.items() if k in rows}

    async def _execute(self, plan: sp.Plan, ica: IcaList) -> None:
        held = self._hold(plan.mass_delete)
        for uid in plan.forget:
            self._links.pop(uid, None)

        # 1. Changes and removals to ICA.
        ica_results: dict[str, tuple[bool, sp.IcaItem | None]] = {}
        for pair in plan.pairs:
            if pair.delete_ica and not held:
                ica_results[pair.uid] = (await self._ica_delete(pair), None)
            elif pair.pushes_ica:
                ica_results[pair.uid] = await self._ica_update(ica.row(pair.link.row), pair)

        # 2. New items: pair with an identical open ICA item, or add them.
        for src, item in plan.link_existing:
            self._links[src.uid] = sp.new_link(src, item)
            self._pending.pop(item.id, None)
        for src in plan.add_to_ica:
            await self._ica_add(src)

        # 3. Changes to the Home Assistant list, then read it back: some lists
        #    (Google Keep among them) fail quietly, so trust only what took.
        after: dict[str, sp.SourceItem] = {}
        source_pairs = [pair for pair in plan.pairs if pair.pushes_src]
        if source_pairs:
            for pair in source_pairs:
                await self._source_update(pair)
            after = {item.uid: item for item in await self._source_items() or []}

        # 4. Remember how every pair looks now.
        for pair in plan.pairs:
            outcome: dict[str, Any] = {}
            if pair.uid in ica_results:
                outcome["ica_ok"], outcome["ica_after"] = ica_results[pair.uid]
            if pair.pushes_src:
                outcome["src_after"] = after.get(pair.uid)
                outcome["src_ok"] = sp.source_push_took(pair, outcome["src_after"])
                if not outcome["src_ok"]:
                    self._warn(
                        f"src:{pair.uid}",
                        f"{self.source} didn't take the change to '{pair.src.summary if pair.src else pair.uid}'",
                    )
                else:
                    self._warned.discard(f"src:{pair.uid}")
            link = sp.link_after(pair, held=held and pair.delete_ica, **outcome)
            if link is None:
                self._links.pop(pair.uid, None)
            else:
                self._links[pair.uid] = link

        # 5. Items added in the ICA app, copied to the Home Assistant list.
        for item in plan.add_to_source:
            await self._source_add(item)

    def _hold(self, mass_delete: bool) -> bool:
        """Big removals wait a few minutes in case the list just glitched."""
        if not mass_delete:
            if self._hold_since is not None:
                self._hold_since = None
                self._warned.discard("hold")
            return False
        now = time.monotonic()
        if self._hold_since is None:
            self._hold_since = now
            self._warn(
                "hold",
                f"many items disappeared from {self.source} at once; removing them "
                "from ICA in 3 minutes if they're still gone",
            )
        return now - self._hold_since < MASS_DELETE_HOLD

    # ------------------------------------------------------------ ICA writes
    async def _ica_add(self, src: sp.SourceItem) -> None:
        try:
            row = await self._client.async_add_row(self.list_id, src.summary)
        except IcaError as err:
            self._warn("ica", f"couldn't add '{src.summary}' to ICA: {err}")
            return
        self._warned.discard("ica")
        if row is None:
            # ICA didn't say which row it made: re-read so the next sync pairs
            # it by name instead of adding it twice.
            await self._coordinator.async_refresh()
            return
        self._coordinator.apply_row(self.list_id, row)
        self._links[src.uid] = sp.new_link(src, sp.IcaItem(row.id, row.text, row.striked))
        self._debug.add("mirror", f"added '{src.summary}' to ICA")

    async def _ica_update(
        self, row: IcaRow | None, pair: sp.PairPlan
    ) -> tuple[bool, sp.IcaItem | None]:
        if row is None:
            return False, None
        try:
            updated = await self._client.async_update_row(
                row, text=pair.ica_text, striked=pair.ica_done
            )
        except IcaApiError as err:
            if err.status == 404:  # removed in ICA meanwhile; the next read shows it
                await self._coordinator.async_refresh()
            self._warn("ica", f"couldn't update '{row.text}' in ICA: {err}")
            return False, None
        except IcaError as err:
            self._warn("ica", f"couldn't update '{row.text}' in ICA: {err}")
            return False, None
        self._warned.discard("ica")
        self._coordinator.apply_row(self.list_id, updated)
        what = []
        if pair.ica_text is not None:
            what.append(f"renamed to '{updated.text}'")
        if pair.ica_done is not None:
            what.append("ticked off" if updated.striked else "unticked")
        self._debug.add("mirror", f"'{row.text}' {' and '.join(what)} in ICA")
        return True, sp.IcaItem(updated.id, updated.text, updated.striked)

    async def _ica_delete(self, pair: sp.PairPlan) -> bool:
        try:
            await self._client.async_delete_row(pair.link.row)
        except IcaError as err:
            self._warn("ica", f"couldn't remove '{pair.link.ica_text}' from ICA: {err}")
            return False
        self._warned.discard("ica")
        self._coordinator.remove_row(self.list_id, pair.link.row)
        self._debug.add("mirror", f"removed '{pair.link.ica_text}' from ICA")
        return True

    # ------------------------------------------------- Home Assistant writes
    async def _source_update(self, pair: sp.PairPlan) -> None:
        data: dict[str, Any] = {"item": pair.uid}
        if pair.src_text is not None:
            data["rename"] = pair.src_text
        if pair.src_done is not None:
            data["status"] = "completed" if pair.src_done else "needs_action"
        name = pair.src.summary if pair.src else pair.uid
        try:
            await self.hass.services.async_call(
                TODO, "update_item", data, target={"entity_id": self.source}, blocking=True
            )
        except HomeAssistantError as err:
            self._warn(f"src:{pair.uid}", f"couldn't update '{name}' in {self.source}: {err}")
            return
        what = []
        if pair.src_text is not None:
            what.append(f"renamed to '{pair.src_text}'")
        if pair.src_done is not None:
            what.append("ticked off" if pair.src_done else "unticked")
        self._debug.add("mirror", f"'{name}' {' and '.join(what)} in {self.source}")

    async def _source_add(self, item: sp.IcaItem) -> None:
        tries = self._attempts.get(item.id, 0)
        if tries >= IMPORT_ATTEMPTS:
            return
        self._attempts[item.id] = tries + 1
        self._pending[item.id] = time.time()
        try:
            await self.hass.services.async_call(
                TODO, "add_item", {"item": item.text}, target={"entity_id": self.source}, blocking=True
            )
        except HomeAssistantError as err:
            self._warn("import", f"couldn't copy '{item.text}' to {self.source}: {err}")
            return
        self._debug.add("mirror", f"copied '{item.text}' from ICA to {self.source}")

    # --------------------------------------------------------------- helpers
    def _warn(self, key: str, message: str) -> None:
        """Log a problem once until it clears."""
        if key in self._warned:
            return
        self._warned.add(key)
        _LOGGER.warning("ICA mirror: %s", message)
        self._debug.add("mirror", message)

    def _data(self) -> dict[str, Any]:
        return {
            "pair": self._pair,
            "links": {uid: link.as_dict() for uid, link in self._links.items()},
            "pending": dict(self._pending),
            "attempts": dict(self._attempts),
        }


def _float_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result
