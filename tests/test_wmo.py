import struct

import fixtures as F
import pytest

from wotlkconv.chunks import ChunkReader
from wotlkconv.errors import UnsupportedFormatError
from wotlkconv.limits import MOMT_SIZE
from wotlkconv.options import Options
from wotlkconv.report import Status
from wotlkconv.wmo.convert import convert_wmo_root, inspect_wmo_root
from wotlkconv.wmo.group import convert_group, inspect_group, parse_group
from wotlkconv.wmo.root import StringTable, parse_root, split_string_table


@pytest.fixture
def wmo_root():
    return F.build_modern_wmo_root()


@pytest.fixture
def wmo_group():
    return F.build_modern_wmo_group()


@pytest.fixture
def wmo_source(source, asset_dir, wmo_group):
    (asset_dir / "830001.wmo").write_bytes(wmo_group)
    (asset_dir / "830002.wmo").write_bytes(wmo_group)
    return source


# ---------------------------------------------------------------------------
# String tables
# ---------------------------------------------------------------------------
def test_string_table_dedupes_and_aligns():
    table = StringTable()
    a = table.add("world\\a.blp")
    b = table.add("world\\bb.blp")
    assert table.add("WORLD\\A.BLP") == a       # lookup is case-insensitive
    assert b % 4 == 0                            # entries stay 4-byte aligned
    assert set(split_string_table(table.getvalue()).values()) == {
        "world\\a.blp", "world\\bb.blp"}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
def test_modern_root_is_recognised_as_incompatible(wmo_root):
    info = inspect_wmo_root(wmo_root, "t.wmo")
    assert info["wotlk_compatible"] is False
    assert set(info["modern_chunks"]) >= {"GFID", "MODI", "MOSI", "MOUV"}
    assert info["num_lod"] == 2


def test_legion_flags_and_numlod_word_is_split(wmo_root, listfile):
    out, res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert back.header_flags == 0x2 and back.num_lod == 0
    assert any(n.code == "wmo.lod" for n in res.notes)


def test_texture_file_ids_become_a_motx_table(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    motx = split_string_table(back.payload("MOTX"))
    assert set(motx.values()) == {"world\\wmo\\tex1.blp", "world\\wmo\\tex2.blp"}
    momt = back.payload("MOMT")
    for i in range(2):
        assert struct.unpack_from("<I", momt, i * MOMT_SIZE + 12)[0] in motx
    assert back.n_textures == 2


def test_material_fields_are_read_at_the_smomaterial_offsets(wmo_root, listfile):
    """texture_3 is at 0x24; 0x20 is the ground type and stays a number.  An
    absent texture points at an empty name, as in 3.3.5a's own files, because
    offset 0 is the first texture's name."""
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    blob = back.payload("MOTX")

    def name_at(offset):
        return blob[offset:blob.index(b"\0", offset)].decode("latin-1")

    momt = back.payload("MOMT")
    for i in range(2):
        texture_2, _color, ground, texture_3 = struct.unpack_from(
            "<4I", momt, i * MOMT_SIZE + 24)
        assert ground == 10
        assert name_at(texture_2) == ""
        assert name_at(texture_3) == ("world\\wmo\\tex1.blp" if i else "")
    assert "unknown\\10.blp" not in split_string_table(blob).values()


def test_a_shader_23_material_keeps_its_diffuse_texture(wmo_root, listfile):
    """Shader 23 leaves texture_1 empty and keeps the diffuse in texture_2,
    and further texture ids in color_3/flags_3/runtime data.  Falling back to
    diffuse, 3.3.5a reads texture_1 and takes those words for colour and
    flags."""
    raw = bytearray(wmo_root)
    at = raw.index(b"TMOM") + 8 + MOMT_SIZE          # the second material
    struct.pack_into("<3I", raw, at, 0, 23, 0)
    struct.pack_into("<I", raw, at + 12, 0)
    struct.pack_into("<I", raw, at + 24, 800002)
    struct.pack_into("<4I", raw, at + 40, 800001, 800002, 7, 9)
    out, res, _ = convert_wmo_root(bytes(raw), "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    blob, momt = back.payload("MOTX"), back.payload("MOMT")
    texture_1, = struct.unpack_from("<I", momt, MOMT_SIZE + 12)
    assert blob[texture_1:blob.index(b"\0", texture_1)] == b"world\\wmo\\tex2.blp"
    assert momt[MOMT_SIZE + 40:2 * MOMT_SIZE] == bytes(24)
    assert any(n.code == "wmo.material.texture_promoted" for n in res.notes)


def test_doodad_file_ids_become_a_modn_table(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert "MODI" not in back.chunks
    modn = split_string_table(back.payload("MODN"))
    assert set(modn.values()) == {"world\\doodads\\tree.m2", "world\\doodads\\rock.m2"}
    modd = back.payload("MODD")
    for i in range(2):
        word = struct.unpack_from("<I", modd, i * 40)[0]
        assert (word & 0xFFFFFF) in modn      # nameIndex is now a byte offset
        assert (word >> 24) == 0x01           # the flags byte is preserved
    assert back.n_doodad_names == 2


def test_skybox_file_id_becomes_mosb(wmo_root, listfile):
    out, res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert back.payload("MOSB").startswith(b"environments\\stars\\sky.m2\0")
    assert any(n.code == "wmo.skybox.resolved" for n in res.notes)


def test_material_shader_and_blend_are_clamped(wmo_root, listfile):
    out, res = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)[:2]
    momt = parse_root(out, "o.wmo").payload("MOMT")
    for i in range(2):
        flags, shader, blend = struct.unpack_from("<3I", momt, i * MOMT_SIZE)
        assert shader <= 6 and blend <= 6
        assert flags == 0x0004                # the 0x8000 bit is post-Wrath
    codes = {n.code for n in res.notes}
    assert {"wmo.material.shader", "wmo.material.blend"} <= codes


def test_modern_chunks_are_dropped(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(), listfile)
    names = set(parse_root(out, "o.wmo").chunks)
    assert not (names & {"GFID", "MODI", "MOSI", "MOUV", "MAVG"})
    assert {"MVER", "MOHD", "MOTX", "MOMT", "MOGI", "MODS", "MODN", "MODD",
            "MFOG"} <= names


def test_path_prefix_applies_to_both_tables(wmo_root, listfile):
    out, _res, _ = convert_wmo_root(wmo_root, "t.wmo", Options(path_prefix="patch"),
                                    listfile)
    back = parse_root(out, "o.wmo")
    assert all(v.startswith("patch\\")
               for v in split_string_table(back.payload("MOTX")).values())
    assert all(v.startswith("patch\\")
               for v in split_string_table(back.payload("MODN")).values())


def test_groups_are_converted_and_renamed(wmo_root, listfile, wmo_source):
    _out, res, companions = convert_wmo_root(wmo_root, "House.wmo", Options(),
                                             listfile, wmo_source)
    assert [c.filename for c in companions] == ["House_000.wmo", "House_001.wmo"]
    assert all(c.data for c in companions)


def test_output_stem_renames_the_groups(wmo_root, listfile, wmo_source):
    _out, _res, companions = convert_wmo_root(wmo_root, "12345.wmo", Options(),
                                              listfile, wmo_source,
                                              output_stem="Stormwind")
    assert [c.filename for c in companions] == ["Stormwind_000.wmo",
                                                "Stormwind_001.wmo"]


def test_missing_groups_are_reported(wmo_root, listfile, source):
    _out, res, companions = convert_wmo_root(wmo_root, "t.wmo", Options(),
                                             listfile, source)
    assert companions == []
    assert any(n.code == "wmo.group.missing" for n in res.notes)


def test_a_non_wmo_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_root(b"NOPE" + b"\0" * 100, "t.wmo")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
def group_subchunks(data: bytes) -> list[str]:
    return [c.name for c in parse_group(data, "g").subchunks]


def test_wide_indices_and_polys_are_narrowed(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    names = group_subchunks(out)
    assert "MOVX" not in names and "MPY2" not in names
    assert "MOVI" in names and "MOPY" in names
    codes = {n.code for n in res.notes}
    assert {"wmo.group.movx", "wmo.group.mpy2"} <= codes


def test_material_ids_above_255_become_collision_only(opts):
    raw = F.build_modern_wmo_group(big_material=True)
    out, res = convert_group(raw, "g.wmo", opts)
    mopy = next(c for c in parse_group(out, "g").subchunks if c.name == "MOPY")
    assert mopy.data[1] == 0xFF
    assert any(n.code == "wmo.group.material_id" for n in res.notes)


def test_extra_uv_and_colour_layers_are_dropped(opts):
    raw = F.build_modern_wmo_group(uv_layers=4, colour_layers=3)
    out, res = convert_group(raw, "g.wmo", opts)
    names = group_subchunks(out)
    assert names.count("MOTV") == 2 and names.count("MOCV") == 2
    codes = {n.code for n in res.notes}
    assert {"wmo.group.uv_layers", "wmo.group.color_layers"} <= codes


def test_second_uv_and_colour_layers_come_last(opts):
    """3.3.5a's own groups, and Noggit, read the chunks in a fixed order; a
    second MOTV straight after the first is read as MOBA, and Noggit fails the
    group or crashes."""
    raw = F.build_modern_wmo_group(uv_layers=2, colour_layers=2)
    out, _res = convert_group(raw, "g.wmo", opts)
    names = [n for n in group_subchunks(out) if n in ("MOTV", "MOBA", "MOCV", "MLIQ", "MOBN", "MOBR")]
    assert names[:2] == ["MOTV", "MOBA"]
    assert names[-2:] == ["MOTV", "MOCV"]
    assert names.index("MOCV") < names.index("MOTV", 1)


def _collision_group(with_moba: bool, uv_layers: int, vertices: int = 6) -> bytes:
    """A group the way retail writes collision-only geometry."""
    from wotlkconv.chunks import ChunkWriter
    raw = F.build_modern_wmo_group(uv_layers=uv_layers, colour_layers=0,
                                   wide_indices=False, wide_polys=False,
                                   vertices=vertices)
    top = list(ChunkReader(raw, reverse=True))
    mogp = next(c.data for c in top if c.name == "MOGP")
    inner = ChunkWriter(reverse=True)
    for c in ChunkReader(mogp, reverse=True, start=68):
        if c.name == "MOBA" and not with_moba:
            continue
        inner.add(c.name, c.data)
    outer = ChunkWriter(reverse=True)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", mogp[:68] + inner.getvalue())
    return outer.getvalue()


def test_a_group_always_has_the_chunks_readers_expect(opts):
    """3.3.5a's groups always carry MOPY, MOVI, MOVT, MONR, MOTV and MOBA, in
    that order; Noggit reads them unconditionally, so a missing MOTV or MOBA
    makes it take the next chunk for it."""
    out, _res = convert_group(_collision_group(with_moba=False, uv_layers=0), "g.wmo", opts)
    names = group_subchunks(out)
    assert names[:6] == ["MOPY", "MOVI", "MOVT", "MONR", "MOTV", "MOBA"]
    subs = {c.name: c.data for c in ChunkReader(
        next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MOGP"),
        reverse=True, start=68)}
    assert subs["MOTV"] == bytes(8 * 6)          # zeroed, one pair per vertex
    assert subs["MOBA"] == b""


def _group_flags(out: bytes) -> int:
    mogp = next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MOGP")
    return struct.unpack_from("<I", mogp, 8)[0]


def _set_group_flags(raw: bytes, flags: int) -> bytes:
    out = bytearray(raw)
    mogp = next(c for c in ChunkReader(raw, reverse=True) if c.name == "MOGP")
    struct.pack_into("<I", out, mogp.offset + 8, flags)
    return bytes(out)


def test_optional_chunk_flags_follow_what_is_written(opts):
    """Retail keeps "has lights" (0x200) on 5,850 groups whose lights moved out
    of MOLR; 3.3.5a reads a flagged chunk without checking its name."""
    raw = F.build_modern_wmo_group()
    raw = _set_group_flags(raw, 0x8 | 0x4 | 0x200 | 0x800 | 0x1000 | 0x400)
    out, _res = convert_group(raw, "g.wmo", opts)
    flags = _group_flags(out)
    names = group_subchunks(out)
    for tag, bit in (("MOLR", 0x200), ("MODR", 0x800), ("MLIQ", 0x1000),
                     ("MOBN", 0x1), ("MPBV", 0x400)):
        assert bool(flags & bit) == (tag in names), tag


def test_a_group_without_a_collision_tree_gets_one(opts):
    out, res = convert_group(F.build_modern_wmo_group(triangles=4), "g.wmo", opts)
    subs = {c.name: c.data for c in ChunkReader(
        next(c.data for c in ChunkReader(out, reverse=True) if c.name == "MOGP"),
        reverse=True, start=68)}
    assert _group_flags(out) & 0x1
    faces = struct.unpack_from(f"<{len(subs['MOBR']) // 2}H", subs["MOBR"], 0)
    assert sorted(set(faces)) == [0, 1, 2, 3]
    assert any(n.code == "wmo.group.bsp_built" for n in res.notes)


def test_material_count_follows_momt_not_texture_names(listfile):
    """MOHD's first word is the MOMT entry count; materials sharing a texture
    made the name count smaller, and readers sized the material array by it."""
    raw = F.build_modern_wmo_root(materials=4, texture_ids=(800001,))
    out, _res, _ = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    back = parse_root(out, "o.wmo")
    assert back.n_textures == len(back.payload("MOMT")) // MOMT_SIZE == 4


def _edit_root(raw: bytes, **replace) -> bytes:
    """Rewrite a root's chunks: name -> new payload, or a callable on the old
    one; names not present are appended."""
    from wotlkconv.chunks import ChunkWriter
    cw = ChunkWriter(reverse=True)
    seen = set()
    for chunk in ChunkReader(raw, reverse=True):
        data = chunk.data
        if chunk.name in replace:
            new = replace[chunk.name]
            data = new(data) if callable(new) else new
            seen.add(chunk.name)
        cw.add(chunk.name, data)
    for name, new in replace.items():
        if name not in seen:
            cw.add(name, new)
    return cw.getvalue()


def _ambient_volume(colour: int, doodad_set: int = 0) -> bytes:
    entry = bytearray(48)
    struct.pack_into("<IIIIH", entry, 0x14, colour, 0, 0, 0, doodad_set)
    return bytes(entry)


def _set_mohd(offset: int, value: int):
    def edit(mohd: bytes) -> bytes:
        out = bytearray(mohd)
        struct.pack_into("<I", out, offset, value)
        return bytes(out)
    return edit


def test_doodad_count_follows_modd(listfile):
    raw = _edit_root(F.build_modern_wmo_root(doodads=2), MOHD=_set_mohd(0x14, 5))
    out, _res, _ = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    assert struct.unpack_from("<I", parse_root(out, "o.wmo").payload("MOHD"), 0x14)[0] == 2


@pytest.mark.parametrize("header,mavg,mavd,written", [
    # Retail keeps the colour in the default set's global volume, header black.
    (0, _ambient_volume(0xFF101820, 1) + _ambient_volume(0xFF304050, 0), b"", 0xFF304050),
    # With no global colour, a black header takes the first volume's.
    (0, _ambient_volume(0), _ambient_volume(0xFF223344), 0xFF223344),
    # A header colour is not replaced by a local volume or a black entry.
    (0xFF808080, _ambient_volume(0), _ambient_volume(0xFF223344), 0xFF808080),
])
def test_ambient_colour_comes_from_the_ambient_volumes(listfile, header, mavg, mavd, written):
    raw = _edit_root(F.build_modern_wmo_root(), MOHD=_set_mohd(0x1C, header),
                     MAVG=mavg, MAVD=mavd)
    out, res, _ = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    assert struct.unpack_from("<I", parse_root(out, "o.wmo").payload("MOHD"), 0x1C)[0] == written
    assert any(n.code == "wmo.ambient" for n in res.notes) == (header == 0)


def test_layer_flags_are_recomputed_to_match(opts):
    raw = F.build_modern_wmo_group(uv_layers=1, colour_layers=0)
    out, _res = convert_group(raw, "g.wmo", opts)
    flags = parse_group(out, "g").flags
    assert flags & 0x02000000 == 0   # only one MOTV: the two-UV bit must clear
    assert flags & 0x01000000 == 0
    assert flags & 0x00000004 == 0   # no MOCV: the vertex-colour bit must clear
    assert flags & 0x08000000 == 0   # post-Wrath bit cleared


def test_split_group_indices_are_cleared(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    group = parse_group(out, "g")
    assert group.flags2 == 0
    assert struct.unpack_from("<I", group.header, 0x40)[0] == 0
    assert any(n.code == "wmo.group.split_index" for n in res.notes)


def test_batch_bounds_are_recomputed_for_shadowlands_layout(wmo_group, opts):
    out, res = convert_group(wmo_group, "g.wmo", opts)
    moba = next(c for c in parse_group(out, "g").subchunks if c.name == "MOBA")
    assert struct.unpack_from("<6h", moba.data, 0) != (0, 0, 0, 0, 0, 0)
    assert any(n.code == "wmo.group.moba_bounds" for n in res.notes)


def test_modern_group_chunks_are_dropped(wmo_group, opts):
    out, _res = convert_group(wmo_group, "g.wmo", opts)
    names = group_subchunks(out)
    assert "MOBS" not in names and "MOLS" not in names


def test_a_wrath_group_is_passed_through(opts):
    raw = F.build_modern_wmo_group(wide_indices=False, wide_polys=False,
                                   uv_layers=1, colour_layers=1)
    # Strip the modern-only chunks so nothing needs converting.
    inner = [c for c in parse_group(raw, "g").subchunks
             if c.name not in ("MOBS", "MOLS")]
    from wotlkconv.chunks import ChunkWriter
    body = ChunkWriter(reverse=True)
    for c in inner:
        body.add(c.name, c.data)
    header = bytearray(parse_group(raw, "g").header)
    struct.pack_into("<I", header, 8, 0x0C)
    struct.pack_into("<II", header, 0x3C, 0, 0)
    outer = ChunkWriter(reverse=True)
    outer.add("MVER", struct.pack("<I", 17))
    outer.add("MOGP", bytes(header) + body.getvalue())
    _out, res = convert_group(outer.getvalue(), "g.wmo", opts)
    assert res.status is Status.PASSTHROUGH


def test_an_index_past_the_vertex_array_is_a_hard_failure(opts):
    raw = bytearray(F.build_modern_wmo_group(wide_indices=True))
    # Sub-chunk offsets are relative to the MOGP payload, so rebase onto the file.
    mogp = next(c for c in ChunkReader(bytes(raw), reverse=True) if c.name == "MOGP")
    movx = next(c for c in parse_group(bytes(raw), "g").subchunks
                if c.name == "MOVX")
    struct.pack_into("<I", raw, mogp.offset + movx.offset, 70000)
    out, res = convert_group(bytes(raw), "g.wmo", opts)
    assert res.status is Status.FAILED and out == b""
    assert any(n.code == "wmo.group.bad_index" for n in res.notes)


def test_inspect_group_lists_modern_subchunks(wmo_group):
    info = inspect_group(wmo_group, "g.wmo")
    assert set(info["modern_subchunks"]) >= {"MOVX", "MPY2", "MOBS", "MOLS"}
