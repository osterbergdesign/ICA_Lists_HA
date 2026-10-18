"""Rules for keeping a Home Assistant to-do list and an ICA list in step.

Pure functions with no Home Assistant imports, so the rules can be tested alone.

Each pair of items (one in Home Assistant, one in ICA) has a Link that
remembers what each side looked like the last time they agreed. Comparing each
side with its own snapshot shows which side changed, so a change is pushed
once, and one side's own formatting (ICA capitalising a word, say) is never
bounced back to the other.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

MASS_DELETE_MIN = 5  # this many removals in one go ...
MASS_DELETE_SHARE = 0.5  # ... that are at least half of all pairs wait for a second look
ICA_FAIL_LIMIT = 5  # give up pushing one change to ICA after this many failures
SRC_FAIL_LIMIT = 3  # same for pushes to the Home Assistant list


@dataclass(frozen=True)
class SourceItem:
    """An item on the Home Assistant to-do list."""

    uid: str
    summary: str
    done: bool


@dataclass(frozen=True)
class IcaItem:
    """An item on the ICA list."""

    id: str
    text: str
    done: bool


@dataclass
class Link:
    """A pair of items and how both looked when they last agreed."""

    row: str
    src_text: str
    ica_text: str
    src_done: bool
    ica_done: bool
    src_fail: int = 0
    ica_fail: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Link | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                row=str(data["row"]),
                src_text=str(data["src_text"]),
                ica_text=str(data["ica_text"]),
                src_done=bool(data["src_done"]),
                ica_done=bool(data["ica_done"]),
                src_fail=int(data.get("src_fail", 0)),
                ica_fail=int(data.get("ica_fail", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def norm(text: str) -> str:
    """Compare texts without caring about case or extra spaces."""
    return " ".join(str(text).split()).casefold()


@dataclass
class PairPlan:
    """What to do with one linked pair."""

    uid: str
    link: Link
    src: SourceItem | None
    ica: IcaItem | None
    ica_text: str | None = None  # push this text to ICA
    ica_done: bool | None = None  # push this ticked state to ICA
    src_text: str | None = None  # push this text to Home Assistant
    src_done: bool | None = None  # push this ticked state to Home Assistant
    delete_ica: bool = False  # removed in Home Assistant: remove the ICA row
    gone_in_ica: bool = False  # removed in ICA: tick off in Home Assistant, then forget

    @property
    def pushes_ica(self) -> bool:
        return self.ica_text is not None or self.ica_done is not None

    @property
    def pushes_src(self) -> bool:
        return self.src_text is not None or self.src_done is not None


@dataclass
class Plan:
    """Everything one sync should do."""

    pairs: list[PairPlan] = field(default_factory=list)
    link_existing: list[tuple[SourceItem, IcaItem]] = field(default_factory=list)
    add_to_ica: list[SourceItem] = field(default_factory=list)
    add_to_source: list[IcaItem] = field(default_factory=list)
    forget: list[str] = field(default_factory=list)
    mass_delete: bool = False

    @property
    def writes_ica(self) -> bool:
        return bool(self.add_to_ica) or any(
            p.pushes_ica or p.delete_ica for p in self.pairs
        )

    @property
    def has_work(self) -> bool:
        return (
            self.writes_ica
            or bool(self.add_to_source or self.link_existing or self.forget)
            or any(p.pushes_src or p.gone_in_ica for p in self.pairs)
        )


def plan(
    source: list[SourceItem],
    ica: list[IcaItem],
    links: dict[str, Link],
    *,
    import_new: bool = False,
    pending_imports: frozenset[str] | set[str] = frozenset(),
) -> Plan:
    """Work out what to change so both lists agree."""
    result = Plan()
    src_by_uid = {item.uid: item for item in source}
    ica_by_id = {item.id: item for item in ica}
    linked_rows = {link.row for link in links.values()}

    for uid, link in links.items():
        src = src_by_uid.get(uid)
        row = ica_by_id.get(link.row)
        if src is None and row is None:
            result.forget.append(uid)
            continue
        pair = PairPlan(uid, link, src, row)
        if src is None:
            pair.delete_ica = True
        elif row is None:
            pair.gone_in_ica = True
            if not src.done:
                pair.src_done = True
        else:
            _compare(pair, src, row, link)
        result.pairs.append(pair)

    removals = sum(1 for pair in result.pairs if pair.delete_ica)
    if removals >= MASS_DELETE_MIN and removals >= MASS_DELETE_SHARE * len(links):
        result.mass_delete = True

    # New items in Home Assistant: pair with an identical open ICA item if
    # there is one (no duplicates), otherwise add them to ICA.
    free = [row for row in ica if row.id not in linked_rows and not row.done]
    for src in source:
        if src.uid in links or src.done or not src.summary.strip():
            continue
        match = next((row for row in free if norm(row.text) == norm(src.summary)), None)
        if match is not None:
            free.remove(match)
            result.link_existing.append((src, match))
        else:
            result.add_to_ica.append(src)

    if import_new:
        result.add_to_source = [
            row for row in free if row.id not in pending_imports and row.text.strip()
        ]
    return result


def _compare(pair: PairPlan, src: SourceItem, row: IcaItem, link: Link) -> None:
    # Text: the side that changed since they last agreed wins; HA wins a tie.
    # An empty text is never pushed (neither side accepts one).
    if src.summary != link.src_text:
        if norm(src.summary) != norm(row.text) and src.summary.strip():
            pair.ica_text = src.summary
    elif (
        row.text != link.ica_text
        and norm(row.text) != norm(src.summary)
        and row.text.strip()
    ):
        pair.src_text = row.text
    # Ticked off or not: same rule.
    if src.done != link.src_done:
        if src.done != row.done:
            pair.ica_done = src.done
    elif row.done != link.ica_done and row.done != src.done:
        pair.src_done = row.done


def source_push_took(pair: PairPlan, after: SourceItem | None) -> bool:
    """Did the Home Assistant list really take the change? (Some lists fail quietly.)"""
    if after is None:
        return False
    if pair.src_text is not None and norm(after.summary) != norm(pair.src_text):
        return False
    return pair.src_done is None or after.done == pair.src_done


def link_after(
    pair: PairPlan,
    *,
    ica_ok: bool | None = None,
    ica_after: IcaItem | None = None,
    src_ok: bool | None = None,
    src_after: SourceItem | None = None,
    held: bool = False,
) -> Link | None:
    """The link to keep once the pushes in `pair` were tried; None forgets it.

    `ica_ok` / `src_ok` are None when nothing was pushed to that side. A
    failed push keeps the old snapshot for that field, so the change is seen
    and retried next time, up to a limit.
    """
    link = pair.link
    if pair.delete_ica:
        if held:
            return link
        if ica_ok:
            return None
        fails = link.ica_fail + 1
        return None if fails >= ICA_FAIL_LIMIT else replace(link, ica_fail=fails)
    if pair.gone_in_ica:
        if pair.src_done is None or src_ok:
            return None
        fails = link.src_fail + 1
        return None if fails >= SRC_FAIL_LIMIT else replace(link, src_fail=fails)

    src, row = pair.src, pair.ica
    assert src is not None and row is not None
    new = Link(
        row=link.row,
        src_text=src.summary,
        ica_text=row.text,
        src_done=src.done,
        ica_done=row.done,
        src_fail=link.src_fail,
        ica_fail=link.ica_fail,
    )
    if pair.pushes_ica:
        if ica_ok and ica_after is not None:
            new.ica_text, new.ica_done, new.ica_fail = ica_after.text, ica_after.done, 0
        else:
            new.ica_fail = link.ica_fail + 1
            if new.ica_fail < ICA_FAIL_LIMIT:
                if pair.ica_text is not None:
                    new.src_text = link.src_text
                if pair.ica_done is not None:
                    new.src_done = link.src_done
            else:
                new.ica_fail = 0  # give up on this change; take things as they are
    if pair.pushes_src:
        if src_ok and src_after is not None:
            if pair.src_text is not None:
                new.src_text = src_after.summary
            if pair.src_done is not None:
                new.src_done = src_after.done
            new.src_fail = 0
        else:
            new.src_fail = link.src_fail + 1
            if new.src_fail < SRC_FAIL_LIMIT:
                if pair.src_text is not None:
                    new.ica_text = link.ica_text
                if pair.src_done is not None:
                    new.ica_done = link.ica_done
            else:
                new.src_fail = 0
    return new


def new_link(src: SourceItem, row: IcaItem) -> Link:
    """A fresh pair: both sides agree as they are now."""
    return Link(
        row=row.id,
        src_text=src.summary,
        ica_text=row.text,
        src_done=src.done,
        ica_done=row.done,
    )
