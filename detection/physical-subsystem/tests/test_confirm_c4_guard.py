"""Regression tests for the confirming audit (t_c4.py): the Control4 write guard checked only action/info_type, so extra
keys and nested objects passed. It is now an exact key-set / exact value check on the serialized frame."""
import json
import pytest
from br2.adapters import control4 as C4
from br2.adapters.base import OneWayViolation


class W:
    def __init__(s): s.out = b""
    def write(s, b): s.out += b
    async def drain(s): pass


GOOD = C4.build_info_summary("tok")


@pytest.mark.parametrize("bad", [
    {**GOOD, "action2": "x", "command": "ARM_AWAY", "usercode": "1234"},      # t_c4: extra keys
    {"action": "INFO", "info_type": "SUMMARY", "arm": {"type": "ARM_AWAY"}},  # t_c4: nested extra, key set short
    {**GOOD, "arm": {"type": "ARM_AWAY"}},                                    # nested object on top of a good frame
    {**GOOD, "usercode": "1234"},
    {k: v for k, v in GOOD.items() if k != "token"},
    {k: v for k, v in GOOD.items() if k != "nonce"},
    {**GOOD, "token": {"x": 1}}, {**GOOD, "token": ["a"]}, {**GOOD, "token": 5}, {**GOOD, "token": ""},
    {**GOOD, "token": "a\nb"}, {**GOOD, "token": "x" * 5000},
    {**GOOD, "nonce": "other"}, {**GOOD, "source": "APP"}, {**GOOD, "source": ["C4"]},
    {**GOOD, "version": 1}, {**GOOD, "version": "0"}, {**GOOD, "version": False}, {**GOOD, "version": 0.0},
    {**GOOD, "version": None},
    {**GOOD, "action": "INFO ", "info_type": "SUMMARY"}, {**GOOD, "info_type": "summary"},
    {**GOOD, "action": ["INFO"]},
    {**GOOD, "token": float("nan")},
    [], "INFO", None, 5,
])
async def test_anything_but_the_exact_info_summary_frame_is_blocked(bad):
    n, w = [], W()
    g = C4.GuardedC4Writer(w, lambda: n.append(1))
    with pytest.raises(OneWayViolation):
        await g.send(bad)
    assert w.out == b"" and n == [1] and g.sent == []


async def test_a_dict_subclass_cannot_show_the_guard_one_thing_and_the_wire_another():
    class Sneaky(dict):
        calls = 0
        def items(self):                      # json.dumps may use items(); a first call is clean, later ones are not
            Sneaky.calls += 1
            return super().items() if Sneaky.calls == 1 else [("action", "ARM")]
    w = W(); g = C4.GuardedC4Writer(w)
    try:
        await g.send(Sneaky(GOOD))
    except OneWayViolation:
        pass
    wrote = w.out
    assert wrote == b"" or json.loads(wrote) == GOOD       # whatever was written is exactly the checked frame


async def test_duplicate_keys_in_a_pre_serialized_frame_are_not_a_way_around_it():
    w = W(); g = C4.GuardedC4Writer(w)
    with pytest.raises(OneWayViolation):
        g.check('{"a":1}')                                  # a string is not a frame
    await g.send(GOOD)
    assert json.loads(w.out) == GOOD and w.out.endswith(b"\n") and w.out.count(b"\n") == 1


async def test_the_written_bytes_are_exactly_the_validated_serialization():
    w = W(); g = C4.GuardedC4Writer(w)
    await g.send(C4.build_info_summary("tok-123"))
    assert json.loads(w.out) == {"action": "INFO", "info_type": "SUMMARY", "token": "tok-123", "nonce": "qolsys",
                                 "source": "C4", "version": 0}
    assert g.sent == ["INFO/SUMMARY"]
