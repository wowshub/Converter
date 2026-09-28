import struct

import fixtures as F
import pytest

from wotlkconv.adt.convert import (
    MCIN_ENTRY_SIZE,
    MCIN_SIZE_PAYLOAD_ONLY,
    MCIN_SIZE_WITH_HEADER,
    MCNK_HEADER_SIZE,
    AdtParts,
    _fold_holes,
    convert_adt,
    inspect_adt,
    mcin_size_convention,
)
from wotlkconv.chunks import ChunkReader, ChunkWriter
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.limits import ADT_VERSION
from wotlkconv.listfile import Listfile
from wotlkconv.options import Options
from wotlkconv.report import Status

MHDR_FIELD_NAMES = ("MCIN", "MTEX", "MMDX", "MMID", "MWMO", "MWID", "MDDF",
                    "MODF", "MFBO", "MH2O", "MTXF")


@pytest.fixture
def split_tile():
    return F.build_split_adt(chunks=4)


def read_tile(data: bytes):
    named, mcnks = {}, []
    for c in ChunkReader(data, reverse=True):
        mcnks.append(c) if c.name == "MCNK" else named.setdefault(c.name, c)
    return named, mcnks


# ---------------------------------------------------------------------------
# Holes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("high,low", [
    (bytes([0b11, 0b11, 0, 0, 0, 0, 0, 0]), 0b1),          # one corner quadrant
    (bytes([0, 0, 0, 0, 0, 0, 0b11000000, 0b11000000]), 1 << 15),
    (bytes([0xFF] * 8), 0xFFFF),
    (bytes(8), 0),
])
def test_high_res_holes_fold_to_the_low_res_mask(high, low):
    assert _fold_holes(high) == low


def test_a_single_high_res_hole_still_punches_its_quadrant():
    assert _fold_holes(bytes([0b1] + [0] * 7)) == 0b1


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
def test_split_pieces_are_recognised(split_tile):
    root, tex, obj = split_tile
    assert inspect_adt(root, "r.adt")["split"] is True
    assert set(inspect_adt(tex, "t.adt")["modern_chunks"]) >= {"MDID", "MTXP"}


def test_merge_produces_a_monolithic_tile(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, mcnks = read_tile(out)
    assert res.status is Status.LOSSY
    assert len(mcnks) == 4
    assert {"MVER", "MHDR", "MCIN", "MTEX", "MMDX", "MMID", "MWMO", "MWID",
            "MDDF", "MODF"} <= set(named)
    assert not ({"MTXP", "MDID", "MHID"} & set(named))
    assert struct.unpack_from("<I", named["MVER"].data, 0)[0] == ADT_VERSION


def test_terrain_texture_ids_become_mtex(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert paths == [b"tileset\\generic\\grass.blp", b"tileset\\generic\\rock.blp"]
    assert any(n.code == "adt.texture.resolved" for n in res.notes)


def test_mhdr_offsets_point_at_real_chunk_headers(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)
    mhdr = named["MHDR"]
    for name, offset in zip(MHDR_FIELD_NAMES,
                            struct.unpack_from("<11I", mhdr.data, 4)):
        if offset == 0:
            continue
        assert out[mhdr.offset + offset: mhdr.offset + offset + 4][::-1] \
            == name.encode()


def test_mcin_indexes_every_map_chunk(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, mcnks = read_tile(out)
    mcin = named["MCIN"].data
    for i, chunk in enumerate(mcnks):
        offset, size, flags, async_id = struct.unpack_from(
            "<4I", mcin, i * MCIN_ENTRY_SIZE)
        assert offset == chunk.offset - 8      # points at the MCNK magic
        assert size == chunk.size + 8          # covers magic + size + payload
        assert flags == 0 and async_id == 0


def test_map_chunk_pieces_are_reassembled(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    chunk = mcnks[0]
    subs = {c.name: c for c in ChunkReader(chunk.data, reverse=True,
                                           start=MCNK_HEADER_SIZE)}
    assert set(subs) == {"MCVT", "MCCV", "MCNR", "MCLY", "MCRF", "MCSH",
                         "MCAL", "MCSE"}
    assert "MCLV" not in subs and "MCMT" not in subs
    # MCRD (3 doodad refs) then MCRW (2 object refs) become one MCRF.
    assert struct.unpack_from("<5I", subs["MCRF"].data, 0) == (0, 1, 2, 0, 1)


def test_map_chunk_header_offsets_and_counts(split_tile, listfile):
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    chunk = mcnks[0]
    hdr = chunk.data[:MCNK_HEADER_SIZE]
    subs = {c.name: c for c in ChunkReader(chunk.data, reverse=True,
                                           start=MCNK_HEADER_SIZE)}

    for field, name in ((0x14, "MCVT"), (0x18, "MCNR"), (0x1C, "MCLY"),
                        (0x20, "MCRF"), (0x24, "MCAL"), (0x2C, "MCSH"),
                        (0x58, "MCSE"), (0x74, "MCCV")):
        offset = struct.unpack_from("<I", hdr, field)[0]
        assert chunk.data[offset - 8: offset - 4][::-1] == name.encode()

    assert struct.unpack_from("<I", hdr, 0x0C)[0] == 2   # nLayers
    assert struct.unpack_from("<I", hdr, 0x10)[0] == 3   # nDoodadRefs
    assert struct.unpack_from("<I", hdr, 0x38)[0] == 2   # nMapObjRefs
    assert struct.unpack_from("<I", hdr, 0x5C)[0] == 2   # nSndEmitters
    # Sizes include the sub-chunk header; an absent MCLQ still declares 8.
    assert struct.unpack_from("<I", hdr, 0x28)[0] == len(subs["MCAL"].data) + 8
    assert struct.unpack_from("<I", hdr, 0x30)[0] == len(subs["MCSH"].data) + 8
    assert struct.unpack_from("<I", hdr, 0x64)[0] == 8
    assert struct.unpack_from("<II", hdr, 0x78) == (0, 0)


def test_high_res_holes_are_converted_in_place(split_tile, listfile):
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    hdr = mcnks[0].data[:MCNK_HEADER_SIZE]
    assert struct.unpack_from("<I", hdr, 0)[0] & 0x10000 == 0
    assert struct.unpack_from("<H", hdr, 0x3C)[0] == 0b1
    # 0x40 is the low-quality texture map in both layouts, not the mask.
    assert hdr[0x40:0x50] == bytes(range(0xA0, 0xB0))
    assert any(n.code == "adt.holes" for n in res.notes)


def test_chunk_flags_follow_the_sub_chunks_written(listfile):
    """Retail leaves "has shadows" and "has vertex colours" set on chunks it
    no longer ships MCSH or MCCV for."""
    root, tex, obj = F.build_split_adt(chunks=4)
    cw = ChunkWriter(reverse=True)
    for c in ChunkReader(tex, reverse=True):
        if c.name != "MCNK":
            cw.add(c.name, c.data)
            continue
        inner = ChunkWriter(reverse=True)
        for sub in ChunkReader(c.data, reverse=True):
            if sub.name != "MCSH":
                inner.add(sub.name, sub.data)
        cw.add("MCNK", inner.getvalue())
    out, _res = convert_adt(AdtParts(root, cw.getvalue(), obj), "t.adt",
                            Options(), listfile)
    _named, mcnks = read_tile(out)
    for mcnk in mcnks:
        flags = struct.unpack_from("<I", mcnk.data, 0)[0]
        subs = {c.name for c in ChunkReader(mcnk.data, reverse=True,
                                            start=MCNK_HEADER_SIZE)}
        assert "MCSH" not in subs and not flags & 0x1
        assert "MCCV" in subs and flags & 0x40


def test_a_monolithic_tile_is_passed_through(split_tile, listfile):
    root, tex, obj = split_tile
    merged, _ = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    again, res = convert_adt(AdtParts(merged), "t.adt", Options(), listfile)
    assert res.status is Status.PASSTHROUGH
    named, mcnks = read_tile(again)
    assert "MCIN" in named and len(mcnks) == 4


def test_merging_without_the_texture_file_still_works(split_tile, listfile):
    root, _tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, b"", obj), "t.adt", Options(), listfile)
    _named, mcnks = read_tile(out)
    subs = {c.name for c in ChunkReader(mcnks[0].data, reverse=True,
                                        start=MCNK_HEADER_SIZE)}
    assert "MCLY" not in subs     # no texture layers were supplied
    assert "MCRF" in subs and res.ok


def test_a_missing_root_is_an_error(listfile):
    with pytest.raises(MalformedFileError):
        convert_adt(AdtParts(b"", b"x", b"y"), "t.adt", Options(), listfile)


def test_a_non_adt_is_rejected(listfile):
    from wotlkconv.chunks import ChunkWriter
    cw = ChunkWriter(reverse=True)
    cw.add("MVER", struct.pack("<I", 18))
    with pytest.raises(UnsupportedFormatError):
        convert_adt(AdtParts(cw.getvalue()), "t.adt", Options(), listfile)


def test_path_prefix_reaches_the_terrain_texture_list(split_tile, listfile):
    """--path-prefix has to rewrite MTEX too, or the tile references art that
    is not where the rest of the converted set went."""
    root, tex, obj = split_tile
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                            Options(path_prefix="custom\\mypatch"), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert all(p.startswith(b"custom\\mypatch\\") for p in paths), paths


def test_path_prefix_applies_to_an_existing_mtex(split_tile, listfile):
    """A tile that already had MTEX (rather than MDID) gets prefixed too."""
    root, tex, obj = split_tile
    merged, _ = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    out, _res = convert_adt(AdtParts(merged), "t.adt",
                            Options(path_prefix="patch"), listfile)
    named, _ = read_tile(out)
    paths = [p for p in named["MTEX"].data.split(b"\0") if p]
    assert paths and all(p.startswith(b"patch\\") for p in paths), paths


def test_unresolved_terrain_textures_get_placeholders(split_tile):
    from wotlkconv.listfile import Listfile
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(),
                           Listfile())
    named, _ = read_tile(out)
    assert b"unknown\\700001.blp" in named["MTEX"].data
    assert any(n.code == "adt.texture.placeholder" for n in res.notes)


def test_unresolved_terrain_textures_can_fail_the_tile(split_tile):
    from wotlkconv.listfile import Listfile
    from wotlkconv.options import UnresolvedPolicy
    root, tex, obj = split_tile
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                           Options(unresolved=UnresolvedPolicy.FAIL), Listfile())
    assert res.status is Status.FAILED and out == b""
    assert any(n.code == "adt.texture.unresolved" for n in res.notes)


# ---------------------------------------------------------------------------
# What an MCIN entry's size covers
# ---------------------------------------------------------------------------
def _converted_tile(**opts):
    root, tex, obj = F.build_split_adt(chunks=4)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(**opts),
                           Listfile())
    return out, res


def test_a_tile_we_write_reads_back_as_its_own_reference():
    """The detector and the writer have to agree, or one of them is wrong."""
    out, _res = _converted_tile()
    convention, why = mcin_size_convention(out, "t.adt")
    assert convention == MCIN_SIZE_WITH_HEADER
    assert "header as well as the payload" in why


def test_mcin_sizes_span_the_whole_chunk_by_default():
    """Header-inclusive, so a reader trusting MCIN sees the last sub-chunk."""
    out, _res = _converted_tile()
    mcin = _chunk_payload(out, "MCIN")
    offset, size = struct.unpack_from("<II", mcin, 0)
    declared = struct.unpack_from("<I", out, offset + 4)[0]
    assert size == declared + 8


def test_a_reference_tile_can_override_the_default(tmp_path):
    """A real 3.3.5a tile settles it; here one that sizes the payload alone."""
    reference, _res = _converted_tile()
    reference = _restate_mcin_sizes(reference, with_header=False)
    path = tmp_path / "reference.adt"
    path.write_bytes(reference)
    assert mcin_size_convention(reference)[0] == MCIN_SIZE_PAYLOAD_ONLY

    out, res = _converted_tile(adt_reference=str(path))
    assert mcin_size_convention(out)[0] == MCIN_SIZE_PAYLOAD_ONLY
    assert any(n.code == "adt.mcin.learned" for n in res.notes)


def test_a_reference_that_cannot_be_read_warns_and_keeps_the_default(tmp_path):
    out, res = _converted_tile(adt_reference=str(tmp_path / "nope.adt"))
    assert mcin_size_convention(out)[0] == MCIN_SIZE_WITH_HEADER
    note = next(n for n in res.notes if n.code == "adt.mcin.reference_unusable")
    assert "could not read" in note.message


def test_a_modern_tile_is_rejected_as_a_reference():
    """Cataclysm dropped MCIN, so a split tile cannot answer the question."""
    root, _tex, _obj = F.build_split_adt(chunks=4)
    convention, why = mcin_size_convention(root, "modern.adt")
    assert convention == "" and "no MCIN" in why


def test_a_reference_that_disagrees_with_itself_is_refused():
    reference, _res = _converted_tile()
    # Flip one entry only, leaving the rest header-inclusive.
    mixed = _restate_mcin_sizes(reference, with_header=False, only=1)
    convention, why = mcin_size_convention(mixed, "mixed.adt")
    assert convention == "" and "inconsistent with itself" in why


def _chunk_payload(data: bytes, name: str) -> bytes:
    for chunk in ChunkReader(data, reverse=True):
        if chunk.name == name:
            return chunk.data
    raise AssertionError(f"no {name} chunk")


def _restate_mcin_sizes(data: bytes, *, with_header: bool,
                        only: int | None = None) -> bytes:
    """Rewrite MCIN sizes under the other convention, in place."""
    out = bytearray(data)
    for chunk in ChunkReader(data, reverse=True):
        if chunk.name != "MCIN":
            continue
        for i in range(len(chunk.data) // MCIN_ENTRY_SIZE):
            if only is not None and i != only:
                continue
            at = chunk.offset + i * MCIN_ENTRY_SIZE
            offset, _size = struct.unpack_from("<II", out, at)
            if not offset:
                continue
            declared = struct.unpack_from("<I", out, offset + 4)[0]
            struct.pack_into("<I", out, at + 4,
                             declared + 8 if with_header else declared)
    return bytes(out)


# ---------------------------------------------------------------------------
# Placements named by FileDataID
# ---------------------------------------------------------------------------
def _names(named, blob: str, ids: str) -> list[bytes]:
    data = named[blob].data
    offsets = struct.unpack_from(f"<{len(named[ids].data) // 4}I",
                                 named[ids].data, 0)
    return [data[o:data.index(b"\0", o)] for o in offsets]


def _placements(named, name: str, size: int, flags_at: int):
    data = named[name].data
    return [(struct.unpack_from("<I", data, i)[0],
             struct.unpack_from("<H", data, i + flags_at)[0])
            for i in range(0, len(data), size)]


def _refs(mcnk):
    counts = struct.unpack_from("<I", mcnk.data, 0x10)[0], \
        struct.unpack_from("<I", mcnk.data, 0x38)[0]
    subs = {c.name: c.data for c in ChunkReader(mcnk.data, reverse=True,
                                                start=MCNK_HEADER_SIZE)}
    refs = list(struct.unpack_from(f"<{len(subs['MCRF']) // 4}I", subs["MCRF"], 0))
    return refs[:counts[0]], refs[counts[0]:]


def test_placements_named_by_file_data_id_get_their_name_tables_back(listfile):
    """Retail writes MDDF/MODF entries keyed by FileDataID and no name tables;
    3.3.5a (and Noggit, which crashed on it) can only follow a name index."""
    root, tex, obj = F.build_split_adt(doodad_ids=(810001, 810002, 810001),
                                       wmo_ids=(840001,), object_refs=1)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)

    assert _names(named, "MMDX", "MMID") == [b"world\\doodads\\tree.m2",
                                             b"world\\doodads\\rock.m2"]
    assert _placements(named, "MDDF", 36, 34) == [(0, 0x1), (1, 0x1), (0, 0x1)]
    assert _names(named, "MWMO", "MWID") == [b"world\\wmo\\dungeon\\keep.wmo"]
    assert _placements(named, "MODF", 64, 56) == [(0, 0x1)]
    assert any(n.code == "adt.doodad.resolved" for n in res.notes)

    again, res = convert_adt(AdtParts(out), "t.adt", Options(), listfile)
    assert res.status is Status.PASSTHROUGH and again == out


def test_every_placement_table_is_written_even_when_empty(listfile):
    """The reader seeks to MDDF and MODF through MHDR without checking for 0."""
    root, tex, obj = F.build_split_adt(doodad_ids=(), wmo_ids=(),
                                       doodad_refs=0, object_refs=0)
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    mhdr = struct.unpack_from("<12I", out, 20)
    for i, name in enumerate(MHDR_FIELD_NAMES[:8]):
        at = 20 + mhdr[1 + i]
        assert mhdr[1 + i], name
        assert out[at:at + 4] == name[::-1].encode(), name


def test_unresolved_placements_get_placeholders():
    root, tex, obj = F.build_split_adt(doodad_ids=(810001,), wmo_ids=(840001,),
                                       doodad_refs=1, object_refs=1)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), Listfile())
    named, _ = read_tile(out)
    assert _names(named, "MMDX", "MMID") == [b"unknown\\810001.m2"]
    assert _names(named, "MWMO", "MWID") == [b"unknown\\840001.wmo"]
    assert any(n.code == "adt.doodad.placeholder" for n in res.notes)


def test_stripped_placements_take_their_map_chunk_references_with_them():
    from wotlkconv.options import UnresolvedPolicy
    lf = Listfile("<test>")
    lf.update(["810002;world/doodads/rock.m2", "840001;world/wmo/dungeon/keep.wmo"])
    root, tex, obj = F.build_split_adt(doodad_ids=(810001, 810002, 810001),
                                       wmo_ids=(840001,), doodad_refs=3,
                                       object_refs=1)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                           Options(unresolved=UnresolvedPolicy.STRIP), lf)
    named, mcnks = read_tile(out)
    assert _placements(named, "MDDF", 36, 34) == [(0, 0x1)]
    assert _refs(mcnks[0]) == ([0], [0])
    assert sum(n.code == "adt.doodad.stripped" for n in res.notes) == 2


def test_unresolved_placements_can_fail_the_tile(listfile):
    from wotlkconv.options import UnresolvedPolicy
    root, tex, obj = F.build_split_adt(doodad_ids=(999999,), doodad_refs=1,
                                       object_refs=0)
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                           Options(unresolved=UnresolvedPolicy.FAIL), listfile)
    assert res.status is Status.FAILED and out == b""
    assert any(n.code == "adt.doodad.unresolved" for n in res.notes)


def test_path_prefix_reaches_the_placement_names(listfile):
    root, tex, obj = F.build_split_adt(doodad_ids=(810001,), wmo_ids=(840001,),
                                       doodad_refs=1, object_refs=1)
    out, _res = convert_adt(AdtParts(root, tex, obj), "t.adt",
                            Options(path_prefix="patch"), listfile)
    named, _ = read_tile(out)
    assert _names(named, "MMDX", "MMID") == [b"patch\\world\\doodads\\tree.m2"]
    assert _names(named, "MWMO", "MWID") == [b"patch\\world\\wmo\\dungeon\\keep.wmo"]


def _with_doodad_set_lists(obj: bytes, entries, ranges, sets) -> bytes:
    """Rewrite an _obj0's MODF flags/doodadSet words and add MWDR and MWDS."""
    out = ChunkWriter(reverse=True)
    for chunk in ChunkReader(obj, reverse=True):
        data = chunk.data
        if chunk.name == "MODF":
            data = bytearray(data)
            for i, (flags, doodad_set) in enumerate(entries):
                struct.pack_into("<HH", data, i * 64 + 56, flags, doodad_set)
            data = bytes(data)
        out.add(chunk.name, data)
        if chunk.name == "MODF":
            out.add("MWDR", b"".join(struct.pack("<II", b, e) for b, e in ranges))
            out.add("MWDS", struct.pack(f"<{len(sets)}H", *sets))
    return out.getvalue()


def test_a_placement_showing_several_doodad_sets_keeps_its_largest(listfile):
    """Shadowlands placements list their sets in MWDS and put an MWDR index in
    doodadSet (flag 0x80); read as a set, that index is an arbitrary one."""
    root, tex, obj = F.build_split_adt(wmo_ids=(840001, 840001, 840001), object_refs=1)
    obj = _with_doodad_set_lists(
        obj, [(0x8 | 0x80, 1), (0x8 | 0x80, 0), (0x8, 5)],
        ranges=[(0, 0), (1, 3)], sets=[0, 1, 2, 3])
    sizes = {"world\\wmo\\dungeon\\keep.wmo": [40, 9, 120, 3]}
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile,
                           wmo_doodad_sets=sizes.get)
    named, _ = read_tile(out)
    modf = named["MODF"].data
    assert [struct.unpack_from("<HH", modf, i * 64 + 56) for i in range(3)] == [
        (0, 2),   # sets 1, 2 and 3: set 2 holds the most doodads
        (0, 0),   # only the default set
        (0, 0),   # set 5 of a world object with four
    ]
    codes = {n.code: n.level for n in res.notes}
    assert codes["adt.wmo.doodad_sets"] == "lossy"
    assert codes["adt.wmo.doodad_set_range"] == "lossy"
    assert "MWDR" not in set(named)
    assert "MWDS" not in str([n.message for n in res.notes if n.code == "adt.chunks.dropped"])


def test_without_the_world_object_the_first_listed_set_is_kept(listfile):
    root, tex, obj = F.build_split_adt(wmo_ids=(840001,), object_refs=1)
    obj = _with_doodad_set_lists(obj, [(0x8 | 0x80, 0)], ranges=[(0, 2)], sets=[0, 4, 1])
    out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(), listfile)
    named, _ = read_tile(out)
    assert struct.unpack_from("<HH", named["MODF"].data, 56) == (0, 4)
