import struct

import fixtures as F
import pytest

from wotlkconv.adt.convert import AdtParts, convert_adt
from wotlkconv.adt.mh2o import (
    LVF_DEPTH,
    LVF_HEIGHT_DEPTH,
    LVF_HEIGHT_UV,
    LiquidTypes,
    convert_mh2o,
    source_format,
    wrath_format,
)
from wotlkconv.chunks import ChunkReader, ChunkWriter
from wotlkconv.options import Options
from wotlkconv.report import FileResult, Status

#: Ocean (2), a Kul Tiras ocean (947), a magma whose material is format 3
#: (1225) and plain water (5), with the basic kind and material format retail
#: gives each.
TYPES = LiquidTypes(basic={2: 1, 947: 1, 1225: 2, 5: 0},
                    vertex_format={2: 0, 947: 0, 1225: 3, 5: 0})


def heights(n, base=1.0):
    return struct.pack(f"<{n}f", *(base + i for i in range(n)))


def uvs(n):
    return struct.pack(f"<{2 * n}H", *range(2 * n))


def depths(n):
    return bytes(range(10, 10 + n))


def retail_liquid():
    return F.build_mh2o([
        # Ocean at sea level, named by LiquidObject 42: depth only.
        {"chunk": 0, "type": 2, "field": 42, "w": 2, "h": 2, "vertices": depths(9),
             "attributes": b"\xff" * 16},
        # Magma on a format-3 material, object 500, with an exists bitmap.
        {"chunk": 1, "type": 1225, "field": 500, "lo": 5.0, "hi": 9.0, "x": 1, "y": 2, "w": 1, "h": 1,
             "bitmap": b"\x01", "vertices": heights(4) + uvs(4) + depths(4)},
        # An ocean above sea level, object 42 again but a newer ocean type.
        {"chunk": 2, "type": 947, "field": 42, "lo": 3.5, "hi": 3.5, "w": 1, "h": 1,
             "vertices": heights(4, 3.5) + depths(4)},
        # Old-style water that already states its format.
        {"chunk": 3, "type": 5, "field": 0, "lo": 1.0, "hi": 4.0, "w": 1, "h": 1,
             "vertices": heights(4) + depths(4)},
        # No vertex data at all.
        {"chunk": 4, "type": 2, "field": 42, "w": 8, "h": 8},
    ])


def read_liquid(payload):
    """{chunk: [(type, field, lo, hi, x, y, w, h, bitmap, vertex bytes)]}"""
    out = {}
    for chunk in range(256):
        table, layers, _attrs = struct.unpack_from("<III", payload, chunk * 12)
        for k in range(layers):
            t, f, lo, hi, x, y, w, h, bm, vd = struct.unpack_from(
                "<HHffBBBBII", payload, table + k * 24)
            n = (w + 1) * (h + 1)
            size = {0: 5, 1: 8, 2: 1}[f] * n if vd else 0
            out.setdefault(chunk, []).append(
                (t, f, lo, hi, x, y, w, h,
                 payload[bm:bm + (w * h + 7) // 8] if bm else b"",
                 payload[vd:vd + size]))
    return out


@pytest.fixture
def result():
    return FileResult(source="t.adt", kind="adt")


def test_the_retail_format_rule(result):
    assert source_format(5, 1, TYPES) == LVF_HEIGHT_UV        # a stated format
    assert source_format(2, 42, TYPES) == LVF_DEPTH           # ocean, always
    assert source_format(947, 42, TYPES) == LVF_HEIGHT_DEPTH  # its material
    assert source_format(1225, 500, TYPES) == 3
    assert source_format(1225, 500, None) is None


@pytest.mark.parametrize("basic,source,lo,hi,expected", [
    (2, 3, 5.0, 9.0, LVF_HEIGHT_UV),     # magma carries UVs
    (3, 0, 0.0, 0.0, LVF_HEIGHT_UV),     # slime too
    (1, 2, 0.0, 0.0, LVF_DEPTH),         # ocean at sea level
    (1, 2, 3.5, 3.5, LVF_HEIGHT_DEPTH),  # ocean anywhere else needs heights
    (0, 1, 0.0, 0.0, LVF_HEIGHT_DEPTH),  # water never carries UVs
    (None, 3, 0.0, 1.0, LVF_HEIGHT_UV),  # no LiquidType: the data decides
])
def test_the_wrath_format_follows_the_kind_of_liquid(basic, source, lo, hi, expected):
    assert wrath_format(basic, source, lo, hi) == expected


def test_retail_liquid_is_reencoded_in_wrath_formats(result):
    out = read_liquid(convert_mh2o(retail_liquid(), TYPES, result))

    ocean = out[0][0]
    assert ocean[:2] == (2, LVF_DEPTH) and ocean[9] == depths(9)

    magma = out[1][0]
    assert magma[:8] == (1225, LVF_HEIGHT_UV, 5.0, 9.0, 1, 2, 1, 1)
    assert magma[8] == b"\x01"
    assert magma[9] == heights(4) + uvs(4)          # depth has no slot

    raised = out[2][0]
    assert raised[1] == LVF_HEIGHT_DEPTH
    assert raised[9] == heights(4, 3.5) + depths(4)

    assert out[3][0][1] == LVF_HEIGHT_DEPTH and out[3][0][9] == heights(4) + depths(4)
    assert out[4][0][1] == LVF_DEPTH and out[4][0][9] == b""
    assert any(n.code == "adt.liquid.reshaped" for n in result.notes)


def test_attributes_survive(result):
    payload = convert_mh2o(retail_liquid(), TYPES, result)
    _table, _layers, attrs = struct.unpack_from("<III", payload, 0)
    assert payload[attrs:attrs + 16] == b"\xff" * 16


def test_without_liquid_types_the_block_sizes_decide(result):
    out = read_liquid(convert_mh2o(retail_liquid(), None, result))
    assert out[1][0][1] == LVF_HEIGHT_UV and out[1][0][9] == heights(4) + uvs(4)
    assert out[2][0][1] == LVF_HEIGHT_DEPTH
    assert out[2][0][9] == heights(4, 3.5) + depths(4)
    assert not any(n.code == "adt.liquid.flat" for n in result.notes)


def test_a_missing_height_is_filled_from_the_minimum(result):
    payload = F.build_mh2o([{"chunk": 0, "type": 947, "field": 2, "lo": 3.5, "hi": 3.5,
                                 "w": 1, "h": 1, "vertices": depths(4)},
                            {"chunk": 1, "type": 2, "field": 42, "w": 1, "h": 1}])
    out = read_liquid(convert_mh2o(payload, TYPES, result))
    assert out[0][0][1] == LVF_HEIGHT_DEPTH
    assert out[0][0][9] == struct.pack("<4f", 3.5, 3.5, 3.5, 3.5) + depths(4)


def test_a_wrath_liquid_chunk_is_left_byte_for_byte(result):
    payload = F.build_mh2o([{"chunk": 7, "type": 2, "field": 2, "w": 1, "h": 1,
                                 "vertices": depths(4)},
                            {"chunk": 8, "type": 3, "field": 1, "lo": 1.0, "hi": 2.0,
                                 "w": 1, "h": 1, "vertices": heights(4) + uvs(4)}])
    assert convert_mh2o(payload, TYPES, result) is payload
    assert not result.notes


def _tile_with_liquid(liquid: bytes) -> bytes:
    root, _tex, _obj = F.build_split_adt(chunks=4)
    cw = ChunkWriter(reverse=True)
    for c in ChunkReader(root, reverse=True):
        cw.add(c.name, liquid if c.name == "MH2O" else c.data)
    return cw.getvalue()


def test_a_tile_carries_its_converted_liquid(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    out, _res = convert_adt(AdtParts(_tile_with_liquid(retail_liquid()), tex, obj),
                            "t.adt", Options(), listfile, liquid_types=TYPES)
    liquid = next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MH2O")
    assert all(inst[1] <= LVF_DEPTH for insts in read_liquid(liquid).values()
               for inst in insts)

    again, res = convert_adt(AdtParts(out), "t.adt", Options(), listfile,
                             liquid_types=TYPES)
    assert res.status is Status.PASSTHROUGH and again == out
