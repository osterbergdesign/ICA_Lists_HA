"""Tests for the sync rules (pure Python)."""

from __future__ import annotations

from run import COMPONENT, load_module

sp = load_module("sync_plan", COMPONENT / "sync_plan.py")
S, I, L = sp.SourceItem, sp.IcaItem, sp.Link


def link(row, text, done=False, ica_text=None, ica_done=None, **kw):
    return L(row=row, src_text=text, ica_text=ica_text if ica_text is not None else text,
             src_done=done, ica_done=done if ica_done is None else ica_done, **kw)


def test_first_sync_links_matches_and_adds_the_rest():
    source = [S("a", "Mjölk", False), S("b", "Ägg", False), S("c", "Kaffe", True), S("d", "  ", False)]
    ica = [I("R1", "mjölk ", False), I("R2", "Bröd", True)]
    p = sp.plan(source, ica, {})
    assert [(s.uid, r.id) for s, r in p.link_existing] == [("a", "R1")]
    assert [s.uid for s in p.add_to_ica] == ["b"]  # done and blank items are left alone
    assert p.writes_ica and not p.add_to_source


def test_duplicates_get_their_own_rows():
    source = [S("a", "Mjölk", False), S("b", "Mjölk", False)]
    p = sp.plan(source, [I("R1", "Mjölk", False)], {})
    assert len(p.link_existing) == 1 and len(p.add_to_ica) == 1


def test_struck_ica_rows_are_not_reused():
    p = sp.plan([S("a", "Mjölk", False)], [I("R1", "Mjölk", True)], {})
    assert not p.link_existing and len(p.add_to_ica) == 1


def test_rename_in_ha_goes_to_ica_and_rename_in_ica_comes_back():
    links = {"a": link("R1", "Mjölk")}
    p = sp.plan([S("a", "Mjölk 3%", False)], [I("R1", "Mjölk", False)], links)
    assert p.pairs[0].ica_text == "Mjölk 3%" and p.pairs[0].src_text is None
    p = sp.plan([S("a", "Mjölk", False)], [I("R1", "Havremjölk", False)], links)
    assert p.pairs[0].src_text == "Havremjölk" and p.pairs[0].ica_text is None


def test_ha_wins_when_both_renamed():
    links = {"a": link("R1", "Mjölk")}
    p = sp.plan([S("a", "Mjölk 3%", False)], [I("R1", "Havremjölk", False)], links)
    assert p.pairs[0].ica_text == "Mjölk 3%" and p.pairs[0].src_text is None


def test_ica_formatting_does_not_bounce():
    # HA renamed to lower case; ICA stores it capitalised.
    links = {"a": link("R1", "Mjölk")}
    src = S("a", "mjölk 3%", False)
    p = sp.plan([src], [I("R1", "Mjölk", False)], links)
    pair = p.pairs[0]
    assert pair.ica_text == "mjölk 3%"
    new = sp.link_after(pair, ica_ok=True, ica_after=I("R1", "Mjölk 3%", False))
    p2 = sp.plan([src], [I("R1", "Mjölk 3%", False)], {"a": new})
    assert not p2.has_work


def test_ticks_flow_both_ways():
    links = {"a": link("R1", "Mjölk")}
    p = sp.plan([S("a", "Mjölk", False)], [I("R1", "Mjölk", True)], links)
    assert p.pairs[0].src_done is True and not p.writes_ica
    p = sp.plan([S("a", "Mjölk", True)], [I("R1", "Mjölk", False)], links)
    assert p.pairs[0].ica_done is True and p.writes_ica


def test_nothing_to_do_when_in_step():
    links = {"a": link("R1", "Mjölk", done=True)}
    p = sp.plan([S("a", "Mjölk", True)], [I("R1", "Mjölk", True)], links)
    assert not p.has_work


def test_removed_in_ha_removes_in_ica():
    links = {"a": link("R1", "Mjölk")}
    p = sp.plan([], [I("R1", "Mjölk", False)], links)
    assert p.pairs[0].delete_ica and not p.mass_delete
    assert sp.link_after(p.pairs[0], ica_ok=True) is None


def test_mass_removal_is_held():
    links = {str(n): link(f"R{n}", f"Vara {n}") for n in range(6)}
    ica = [I(f"R{n}", f"Vara {n}", False) for n in range(6)]
    p = sp.plan([], ica, links)
    assert p.mass_delete
    pair = p.pairs[0]
    assert sp.link_after(pair, held=True) is pair.link
    # A few removals out of many are fine.
    p = sp.plan([S(str(n), f"Vara {n}", False) for n in range(2, 6)], ica, links)
    assert not p.mass_delete


def test_removed_in_ica_ticks_off_in_ha():
    links = {"a": link("R1", "Mjölk"), "b": link("R2", "Ägg", done=True)}
    p = sp.plan([S("a", "Mjölk", False), S("b", "Ägg", True)], [], links)
    by_uid = {pair.uid: pair for pair in p.pairs}
    assert by_uid["a"].gone_in_ica and by_uid["a"].src_done is True
    assert by_uid["b"].gone_in_ica and by_uid["b"].src_done is None
    assert sp.link_after(by_uid["b"]) is None
    assert sp.link_after(by_uid["a"], src_ok=True, src_after=S("a", "Mjölk", True)) is None
    kept = sp.link_after(by_uid["a"], src_ok=False)
    assert kept is not None and kept.src_fail == 1


def test_both_gone_is_forgotten():
    p = sp.plan([], [], {"a": link("R1", "Mjölk")})
    assert p.forget == ["a"] and p.has_work


def test_import_only_when_asked_and_not_twice():
    ica = [I("R1", "Ost", False), I("R2", "Bröd", True), I("R3", "Smör", False)]
    links = {"x": link("R3", "Smör")}
    assert not sp.plan([S("x", "Smör", False)], ica, links).add_to_source
    p = sp.plan([S("x", "Smör", False)], ica, links, import_new=True)
    assert [r.id for r in p.add_to_source] == ["R1"]
    p = sp.plan([S("x", "Smör", False)], ica, links, import_new=True, pending_imports={"R1"})
    assert not p.add_to_source
    # When the imported item shows up in HA it gets linked, not added again.
    p = sp.plan([S("x", "Smör", False), S("y", "Ost", False)], ica, links, import_new=True, pending_imports={"R1"})
    assert [(s.uid, r.id) for s, r in p.link_existing] == [("y", "R1")] and not p.add_to_ica


def test_failed_ica_push_is_retried_then_given_up():
    links = {"a": link("R1", "Mjölk")}
    src, row = S("a", "Mjölk", True), I("R1", "Mjölk", False)
    current = links["a"]
    for attempt in range(1, sp.ICA_FAIL_LIMIT):
        pair = sp.plan([src], [row], {"a": current}).pairs[0]
        assert pair.ica_done is True, attempt
        current = sp.link_after(pair, ica_ok=False)
        assert current.ica_fail == attempt
    pair = sp.plan([src], [row], {"a": current}).pairs[0]
    current = sp.link_after(pair, ica_ok=False)
    assert current.ica_fail == 0
    assert not sp.plan([src], [row], {"a": current}).has_work  # gave up, no loop


def test_quiet_failure_in_ha_does_not_undo_ica():
    # ICA ticked; the HA list ignored our update. We must not untick ICA.
    links = {"a": link("R1", "Mjölk")}
    src, row = S("a", "Mjölk", False), I("R1", "Mjölk", True)
    pair = sp.plan([src], [row], links).pairs[0]
    assert pair.src_done is True
    assert not sp.source_push_took(pair, S("a", "Mjölk", False))
    current = sp.link_after(pair, src_ok=False, src_after=S("a", "Mjölk", False))
    retry = sp.plan([src], [row], {"a": current}).pairs[0]
    assert retry.src_done is True and retry.ica_done is None  # retried, ICA untouched
    for _ in range(sp.SRC_FAIL_LIMIT):
        pair = sp.plan([src], [row], {"a": current}).pairs[0]
        current = sp.link_after(pair, src_ok=False, src_after=src)
    final = sp.plan([src], [row], {"a": current})
    assert not final.writes_ica and not final.has_work


def test_link_roundtrip():
    original = link("R1", "Mjölk", src_fail=2)
    assert sp.Link.from_dict(original.as_dict()) == original
    assert sp.Link.from_dict({"row": "x"}) is None
