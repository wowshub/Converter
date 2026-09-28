import struct

import fixtures as F
import pytest

from wotlkconv.adt.convert import MCNK_HEADER_SIZE, AdtParts, convert_adt
from wotlkconv.adt.layers import MAX_LAYERS, limit_layers, read_alpha, remap_predominant
from wotlkconv.chunks import ChunkReader, ChunkWriter
from wotlkconv.listfile import Listfile
from wotlkconv.options import Options
from wotlkconv.report import Status


def layer(texture, flags=0x100, offset=0, effect=0):
    return struct.pack("<IIIi", texture, flags, offset, effect)


def alpha(value):
    return bytes([value]) * 4096


def mcly_layers(mcly):
    return [struct.unpack_from("<IIIi", mcly, i) for i in range(0, len(mcly), 16)]


# ---------------------------------------------------------------------------
# Alpha maps
# ---------------------------------------------------------------------------
def test_compressed_alpha_decodes_to_4096_values():
    # 4096 = 127 * 32 + 32: fill runs of 127, then one copy run of 32.
    encoded = (bytes([0xFF, 9]) * 32) + bytes([32]) + bytes(range(32))
    raw, values = read_alpha(b"xx" + encoded, 2, 0x300, big=True)
    assert raw == encoded
    assert values == bytes([9]) * 4064 + bytes(range(32))


def test_an_alpha_map_past_the_end_is_unreadable():
    assert read_alpha(bytes(100), 0, 0x100, big=True) is None
    assert read_alpha(bytes([0xFF]), 0, 0x300, big=True) is None


def test_a_4_bit_map_expands_to_8_bits():
    _raw, values = read_alpha(bytes([0x1F]) + bytes(2047), 0, 0x100, big=False)
    assert values[:2] == bytes([0xF * 17, 0x1 * 17])


# ---------------------------------------------------------------------------
# Layer limit
# ---------------------------------------------------------------------------
def test_four_textured_layers_need_no_change():
    mcly = layer(0, 0) + layer(1, offset=0) + layer(2, offset=4096) + layer(3, offset=8192)
    assert limit_layers(mcly, alpha(1) * 3, set(), big=True) is None


def test_the_layers_that_show_least_are_dropped():
    # Layer 2 is nearly transparent, and layer 1 is mostly covered by the
    # three layers above it; layers 3 to 5 are what shows.
    maps = [alpha(200), alpha(3), alpha(200), alpha(200), alpha(128)]
    mcly = layer(0, 0) + b"".join(layer(i + 1, offset=i * 4096) for i in range(5))
    limited = limit_layers(mcly, b"".join(maps), set(), big=True)
    kept = mcly_layers(limited.mcly)
    assert len(kept) == MAX_LAYERS and limited.over_limit == 2
    assert [t for t, *_ in kept] == [0, 3, 4, 5]
    # Each kept map is carried over byte for byte and re-indexed.
    for _position, (texture, _flags, offset, _eff) in enumerate(kept[1:], start=1):
        assert limited.mcal[offset:offset + 4096] == maps[texture - 1]


def test_a_layer_without_a_texture_file_is_dropped():
    mcly = layer(0, 0) + layer(1, offset=0) + layer(2, offset=4096)
    limited = limit_layers(mcly, alpha(10) + alpha(20), {1}, big=True)
    assert [(t, off) for t, _f, off, _e in mcly_layers(limited.mcly)] == [(0, 0), (2, 0)]
    assert limited.mcal == alpha(20) and limited.untextured == 1


def test_an_untextured_base_hands_over_to_the_next_layer():
    mcly = layer(0, 0) + layer(1, offset=0) + layer(2, offset=4096)
    limited = limit_layers(mcly, alpha(10) + alpha(20), {0}, big=True)
    kept = mcly_layers(limited.mcly)
    assert kept[0] == (1, 0, 0, 0)          # a base has no alpha map
    assert kept[1][0] == 2 and limited.mcal == alpha(20)


def test_an_unreadable_alpha_map_drops_its_layer_rather_than_painting_over():
    mcly = (layer(0, 0) + layer(1, offset=0) + layer(2, offset=4096)
            + layer(3, offset=99999) + layer(4, offset=8192))
    limited = limit_layers(mcly, alpha(10) + alpha(20) + alpha(30), set(), big=True)
    assert [t for t, *_ in mcly_layers(limited.mcly)] == [0, 1, 2, 4]
    assert limited.unreadable == 1 and limited.over_limit == 0


def _texture_map(cells):
    out = bytearray(16)
    for cell, value in enumerate(cells):
        out[cell // 4] |= value << (cell % 4 * 2)
    return bytes(out)


def _cells(texture_map):
    return [(texture_map[c // 4] >> (c % 4 * 2)) & 3 for c in range(64)]


def test_the_low_quality_texture_map_follows_the_kept_layers():
    # Six layers become [0, 3, 4, 5]: cells naming layer 3 now name slot 1.
    # A cell naming dropped layer 1 or 2 takes the kept layer showing most:
    # layer 5, whose alpha of 128 shows half of it over everything below.
    maps = [alpha(200), alpha(3), alpha(200), alpha(200), alpha(128)]
    mcly = layer(0, 0) + b"".join(layer(i + 1, offset=i * 4096) for i in range(5))
    limited = limit_layers(mcly, b"".join(maps), set(), big=True)
    assert limited.kept == [0, 3, 4, 5]
    source = _texture_map([0, 1, 2, 3] * 16)
    assert _cells(remap_predominant(source, limited)) == [0, 3, 3, 1] * 16


def _tile_with_layers(count, texture_ids):
    """A split tile whose first chunk has ``count`` layers."""
    root, tex, obj = F.build_split_adt(chunks=1, texture_ids=texture_ids)
    cw = ChunkWriter(reverse=True)
    for c in ChunkReader(tex, reverse=True):
        if c.name != "MCNK":
            cw.add(c.name, c.data)
            continue
        inner = ChunkWriter(reverse=True)
        inner.add("MCLY", layer(0, 0) + b"".join(
            layer(i % len(texture_ids), offset=(i - 1) * 4096) for i in range(1, count)))
        inner.add("MCAL", b"".join(alpha(40 * i) for i in range(1, count)))
        cw.add("MCNK", inner.getvalue())
    return root, cw.getvalue(), obj


def test_a_tile_never_has_more_than_four_layers(listfile):
    root, tex, obj = _tile_with_layers(7, (700001, 700002))
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    mcnk = next(c for c in ChunkReader(out, reverse=True) if c.name == "MCNK")
    assert struct.unpack_from("<I", mcnk.data, 0x0C)[0] == MAX_LAYERS
    subs = {c.name: c.data for c in ChunkReader(mcnk.data, reverse=True, start=MCNK_HEADER_SIZE)}
    assert len(subs["MCLY"]) == 4 * 16 and len(subs["MCAL"]) == 3 * 4096
    assert any(n.code == "adt.layers.limit" for n in res.notes)


# ---------------------------------------------------------------------------
# Textures, texture flags and placement flags
# ---------------------------------------------------------------------------
@pytest.fixture
def specular_listfile():
    lf = Listfile("<test>")
    lf.update(["700001;tileset/generic/grass_s.blp", "700011;tileset/generic/grass.blp",
               "700002;tileset/generic/rock_s.blp"])
    return lf


def test_the_plain_diffuse_texture_replaces_the_specular_variant(specular_listfile):
    root, tex, obj = F.build_split_adt(chunks=1)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), specular_listfile)
    mtex = next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MTEX")
    # rock has no plain file, so it keeps the name retail uses
    assert mtex.split(b"\0")[:-1] == [b"tileset\\generic\\grass.blp",
                                     b"tileset\\generic\\rock_s.blp"]
    assert any(n.code == "adt.texture.plain" for n in res.notes)


def test_a_plain_texture_missing_from_the_build_is_not_used(specular_listfile):
    root, tex, obj = F.build_split_adt(chunks=1)
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(),
                            specular_listfile, file_exists=lambda fid: fid != 700011)
    mtex = next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MTEX")
    assert mtex.split(b"\0")[0] == b"tileset\\generic\\grass_s.blp"


def _with_mtxp(tex, entries):
    cw = ChunkWriter(reverse=True)
    for c in ChunkReader(tex, reverse=True):
        cw.add(c.name, b"".join(struct.pack("<Iff4x", *e) for e in entries)
               if c.name == "MTXP" else c.data)
    return cw.getvalue()


def test_texture_parameters_become_the_flags_wrath_reads(listfile):
    root, tex, obj = F.build_split_adt(chunks=1)
    tex = _with_mtxp(tex, [(0x0, 1.0, 0.0), (0x31, 1.0, 0.0)])
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    mtxf = next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MTXF")
    assert struct.unpack("<2I", mtxf) == (0, 1)    # the scale nibble has no slot


def test_no_texture_flags_means_no_mtxf(listfile):
    root, tex, obj = F.build_split_adt(chunks=1)
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    assert "MTXF" not in {c.name for c in ChunkReader(out, reverse=True)}


def test_placement_flags_are_cut_to_wrath_and_the_wmo_scale_cleared(listfile):
    root, tex, obj = F.build_split_adt(chunks=1, doodad_ids=(810001,), wmo_ids=(840001,),
                                       doodad_refs=1, object_refs=1)
    raw = bytearray(obj)
    # biodome plus later "liquid known" and projected-texture bits on the doodad,
    # destroyable plus "has scale" and a 1.5x scale on the WMO
    at = raw.index(b"FDDM") + 8
    struct.pack_into("<H", raw, at + 34, 0x1 | 0x40 | 0x20 | 0x100)
    at = raw.index(b"FDOM") + 8
    struct.pack_into("<HHHH", raw, at + 56, 0x1 | 0x8 | 0x4, 0, 0, 1536)
    out, res = convert_adt(AdtParts(root, tex, bytes(raw)), "t.adt", Options(), listfile)
    named = {c.name: c.data for c in ChunkReader(out, reverse=True) if c.name != "MCNK"}
    assert struct.unpack_from("<H", named["MDDF"], 34)[0] == 0x1
    assert struct.unpack_from("<HHHH", named["MODF"], 56) == (0x1, 0, 0, 0)
    assert any(n.code == "adt.wmo.scale" for n in res.notes)


def test_a_wrath_tile_stays_a_passthrough(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    merged, _ = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    again, res = convert_adt(AdtParts(merged), "t.adt", Options(), listfile)
    assert res.status is Status.PASSTHROUGH and again == merged
