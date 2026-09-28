"""Liquid volumes: recognised, read, and left out of a 3.3.5a patch.

3.3.5a takes its liquid from terrain and world-object chunks; none of its
archives holds a liquid volume and its executable has no name to ask for one.
"""

import struct

import fixtures as F
import pytest

from wotlkconv.detect import LIQUID, SKIP, classify, detect
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.liquid import inspect_liquid, parse_header


def test_a_liquid_volume_is_recognised_without_a_name():
    assert detect(F.build_liquid(), "") == LIQUID


def test_the_other_byte_order_is_recognised_too():
    assert detect(F.build_liquid(magic=b"LIQ*"), "") == LIQUID


@pytest.mark.parametrize("path", ["unknown/5336174.wlw", "world/maps/az/az.wlw",
                                  "a.wlm", "a.wlq"])
def test_a_liquid_volume_is_skipped_saying_the_client_never_loads_it(path):
    action, reason = classify(LIQUID, path)
    assert action == SKIP and "never loads" in reason


def test_a_liquid_name_on_other_bytes_gets_the_same_reason():
    action, reason = classify("unknown", "world/maps/az/az.wlw")
    assert action == SKIP and "never loads" in reason


def test_the_header_is_read_as_retail_lays_it_out():
    header = parse_header(F.build_liquid(liquid_type=81, blocks=3), "a.wlw")
    assert (header.version, header.liquid_type, header.blocks) == (2, 81, 3)
    assert header.secondary_blocks == 0 and header.trailing == 1


def test_an_empty_volume_is_valid_not_malformed():
    # 21 retail files are exactly this: the old reader took liquid type 5 for
    # a block count and called them "5 blocks that cannot fit in 21 bytes".
    empty = b"*QIL" + struct.pack("<HHHHI", 2, 1, 5, 0, 0) + struct.pack("<I", 0) + b"\x01"
    assert len(empty) == 21
    header = parse_header(empty, "unknown/5336173.wlw")
    assert (header.blocks, header.liquid_type, header.trailing) == (0, 5, 1)


def test_secondary_blocks_are_accounted_for():
    header = parse_header(F.build_liquid(blocks=1, secondary=2), "a.wlw")
    assert header.secondary_blocks == 2 and header.trailing == 1


def test_blocks_that_do_not_fit_are_refused():
    truncated = F.build_liquid(blocks=3)[:16 + 360]
    with pytest.raises(MalformedFileError, match="do not fit"):
        parse_header(truncated, "a.wlw")


def test_a_truncated_header_is_malformed():
    with pytest.raises(MalformedFileError, match="too short"):
        parse_header(b"*QIL\x02\x00", "a.wlw")


def test_a_file_that_is_not_a_liquid_volume_says_so():
    with pytest.raises(UnsupportedFormatError, match="not a liquid volume"):
        parse_header(b"NOPE" + b"\0" * 32, "a.wlw")


def test_inspect_describes_the_volume():
    info = inspect_liquid(F.build_liquid(liquid_type=350, blocks=2), "a.wlw")
    assert info["liquid_type"] == 350 and info["blocks"] == 2
    assert info["loaded_by_wotlk"] is False
    assert inspect_liquid(b"junk", "a.wlw")["readable"] is False
