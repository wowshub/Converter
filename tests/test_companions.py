"""SKIN, ANIM and SKEL -- the files that travel with a model."""

import struct

import fixtures as F
import pytest

from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.limits import SKIN_HEADER_SIZE_WOTLK
from wotlkconv.m2.anim import anim_spans, convert_anim, inspect_anim, measure_offset_base
from wotlkconv.m2.convert import convert_m2
from wotlkconv.m2.downgrade import merge_skeleton
from wotlkconv.m2.model import parse_m2
from wotlkconv.m2.skel import load_skeleton_chain, parse_skel
from wotlkconv.m2.skin import convert_skin, inspect_skin, parse_skin
from wotlkconv.options import Options
from wotlkconv.report import FileResult, Status


# ---------------------------------------------------------------------------
# SKIN
# ---------------------------------------------------------------------------
def test_legion_and_wrath_headers_are_told_apart():
    assert parse_skin(F.build_skin(legion=True), "l").had_legion_header is True
    assert parse_skin(F.build_skin(legion=False), "w").had_legion_header is False


def test_shadow_batches_are_dropped(opts):
    out, res = convert_skin(F.build_skin(legion=True, shadow_batches=3), "t.skin", opts)
    assert res.status is Status.LOSSY
    assert any(n.code == "skin.shadow_batches" for n in res.notes)
    assert len(out) >= SKIN_HEADER_SIZE_WOTLK
    assert parse_skin(out, "o").had_legion_header is False


def test_geometry_survives_the_downgrade(opts):
    raw = F.build_skin(vertices=9, triangles=4, legion=True)
    out, _res = convert_skin(raw, "t.skin", opts)
    before, after = parse_skin(raw, "i"), parse_skin(out, "o")
    assert after.vertices == before.vertices
    assert after.indices == before.indices
    assert after.bones == before.bones
    assert after.submeshes == before.submeshes


def test_batch_texture_count_is_clamped(opts):
    out, res = convert_skin(F.build_skin(texture_count=4, shader_id=0), "t.skin", opts)
    batch = parse_skin(out, "o").batches[0]
    # textureCount sits at 0x0E; 0x10 is textureComboIndex and must be untouched.
    assert struct.unpack_from("<H", batch, 0x0E)[0] == 2
    assert struct.unpack_from("<H", batch, 0x10)[0] == 0
    assert any(n.code == "skin.batch.textures" for n in res.notes)


def test_combiner_shader_without_combiner_combos_is_reset(opts):
    out, res = convert_skin(F.build_skin(shader_id=0x8001), "t.skin", opts,
                            uses_combiner_combos=False)
    assert struct.unpack_from("<H", parse_skin(out, "o").batches[0], 0x02)[0] == 0
    assert any(n.code == "skin.batch.shader" for n in res.notes)


def test_combiner_shader_is_kept_when_the_model_carries_the_table(opts):
    out, res = convert_skin(F.build_skin(shader_id=0x8001), "t.skin", opts,
                            uses_combiner_combos=True)
    assert struct.unpack_from("<H", parse_skin(out, "o").batches[0], 0x02)[0] == 0x8001
    assert not any(n.code == "skin.batch.shader" for n in res.notes)


def test_a_wrath_skin_is_passed_through(opts):
    raw = F.build_skin(legion=False, texture_count=2, shader_id=0)
    _out, res = convert_skin(raw, "t.skin", opts)
    assert res.status is Status.PASSTHROUGH


@pytest.mark.parametrize("declared,bones,written", [
    (0, 4, 21),     # retail writes 0 in every skin
    (0, 30, 53),
    (0, 60, 64),
    (0, 84, 256),   # bloodelffemale00.skin's largest submesh
    (21, 30, 53),   # too small for its own submeshes
    (64, 30, 64),   # a larger palette is left alone, as 41 genuine skins have
])
def test_a_skin_declares_a_bone_palette_that_holds_its_submeshes(opts, declared, bones, written):
    raw = bytearray(F.build_skin())
    struct.pack_into("<I", raw, 0x2C, declared)
    submeshes_at = struct.unpack_from("<I", raw, 4 + 3 * 8 + 4)[0]
    struct.pack_into("<H", raw, submeshes_at + 12, bones)
    out, res = convert_skin(bytes(raw), "t.skin", opts)
    assert struct.unpack_from("<I", out, 0x2C)[0] == written
    assert any(n.code == "skin.bone_count_max" for n in res.notes) == (declared != written)


def test_a_non_skin_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_skin(b"NOPE" + b"\0" * 100, "t.skin")


def test_inspect_skin_counts_everything():
    info = inspect_skin(F.build_skin(vertices=8, triangles=3, legion=True), "t")
    assert info["vertices"] == 8 and info["triangles"] == 3
    assert info["header"] == "legion" and info["shadow_batches"] == 2


# ---------------------------------------------------------------------------
# ANIM
# ---------------------------------------------------------------------------
def test_chunked_anim_is_unwrapped(opts):
    payload = b"\xAA\xBB" * 20
    out, res = convert_anim(F.build_anim(payload), "t.anim", opts)
    assert out == payload
    # Without the model, nothing says where the skeleton chunks belong.
    assert res.status is Status.LOSSY
    assert {n.detail.get("chunk") for n in res.notes
            if n.code == "anim.skeleton.unplaced"} == {"AFSA", "AFSB"}


def test_flat_anim_is_passed_through(opts):
    payload = b"\x01\x02\x03\x04" * 10
    out, res = convert_anim(F.build_anim(payload, chunked=False), "t.anim", opts)
    assert out == payload
    assert res.status is Status.PASSTHROUGH


def test_inspect_anim_detects_the_wrapper():
    assert inspect_anim(F.build_anim(), "t")["chunked"] is True
    assert inspect_anim(F.build_anim(chunked=False), "t")["chunked"] is False


def test_a_too_short_anim_is_an_error(opts):
    with pytest.raises(MalformedFileError):
        convert_anim(b"\x01\x02", "t.anim", opts)


# ---------------------------------------------------------------------------
# SKEL
# ---------------------------------------------------------------------------
def test_skeleton_sections_are_parsed():
    skel = parse_skel(F.build_skel(bones=4, sequences=3, attachments=2), "t.skel")
    assert skel.name == "TestSkeleton"
    assert len(skel.bones) == 4
    assert len(skel.sequences) == 3
    assert len(skel.attachments) == 2
    assert skel.global_loops == [1000]
    assert len(skel.bones[0]["translation"].values) == 3


def test_a_non_skeleton_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_skel(b"NOPE" + b"\0" * 100, "t.skel")


def build_skeleton_model():
    """A Legion character model: the rig lives entirely in the .skel."""
    m = F.build_modern_model(sequences=0, bones=0)
    m.bones = m.sequences = m.attachments = []
    m.key_bone_lookup = m.sequence_lookups = m.attachment_lookup = []
    return F.serialise_modern_m2(m, skeleton_id=940000, anim_ids=())


def test_external_skeleton_is_merged(listfile, source, asset_dir):
    (asset_dir / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=2))
    raw = build_skeleton_model()
    assert parse_m2(raw, "c.m2").uses_external_skeleton
    out, res, _ = convert_m2(raw, "char.m2", Options(), listfile, source)
    back = parse_m2(out, "o.m2")
    assert len(back.bones) == 4 and len(back.sequences) == 2
    assert len(back.attachments) == 1
    assert back.sequences[0]["blend_time"] == 200
    assert any(n.code == "m2.skeleton.merged" for n in res.notes)


def test_missing_skeleton_fails_loudly(listfile, source):
    _out, res, _ = convert_m2(build_skeleton_model(), "char.m2", Options(),
                              listfile, source)
    assert res.status is Status.FAILED
    message = next(n.message for n in res.notes if n.level == "error")
    assert "--allow-missing-skeleton" in message


def test_missing_skeleton_can_be_overridden(listfile, source):
    out, res, _ = convert_m2(build_skeleton_model(), "char.m2",
                             Options(allow_missing_skeleton=True), listfile, source)
    assert res.status is Status.LOSSY
    assert parse_m2(out, "o.m2").bones == []


def test_parent_skeleton_chain_is_followed(source, asset_dir):
    (asset_dir / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=3))
    (asset_dir / "941000.skel").write_bytes(
        F.build_skel(bones=0, sequences=0, attachments=0, parent_id=940000))
    merged = load_skeleton_chain(source.loader_for(".skel"), 941000, "child")
    assert len(merged.bones) == 4 and len(merged.sequences) == 3


def test_companions_are_found_and_renamed(listfile, source, asset_dir):
    for fid in (910000, 910001, 910002, 910003):
        (asset_dir / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    for fid in (920000, 920001):
        (asset_dir / f"{fid}.anim").write_bytes(F.build_anim())
    raw = F.serialise_modern_m2(F.build_modern_model())
    _out, _res, companions = convert_m2(raw, "123456.m2", Options(), listfile,
                                        source, output_stem="testbeast")
    names = sorted(c.filename for c in companions)
    assert names == ["testbeast00.skin", "testbeast0000-00.anim",
                     "testbeast0001-00.anim", "testbeast01.skin",
                     "testbeast02.skin", "testbeast03.skin"]
    assert all(c.data for c in companions)


def test_missing_skins_are_reported(listfile, source):
    raw = F.serialise_modern_m2(F.build_modern_model())
    _out, res, companions = convert_m2(raw, "t.m2", Options(), listfile, source)
    assert companions == []
    assert any(n.code == "m2.skin.missing" for n in res.notes)


# ---------------------------------------------------------------------------
# Texture coordinate lookups
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shader_id,count,units", [
    (0x8000, 2, (0, 0xFFFF)),       # Combiners_Opaque_Mod2xNA_Alpha: T1_Env
    (0x8000 | 25, 1, (0xFFFF,)),    # EdgeFade_Env
    (0x8000 | 18, 3, (0, 0)),       # Diffuse_T1 drawing two textures
    (0x8000 | 15, 2, (0, 1)),       # Diffuse_T1_T2
    (0x8000 | 999, 2, (0, 1)),      # not in the table
    (0, 2, (0, 1)),
    (0, 0, (0,)),
])
def test_a_batch_reads_its_units_off_the_retail_vertex_shader(shader_id, count, units):
    from wotlkconv.m2.texcoords import batch_units
    assert batch_units(shader_id, count) == units


def _batch(shader_id, count, coord):
    return struct.pack("<BbHHHHHHHHHHH", 0, 0, shader_id, 0, 0, 0, 0, 0,
                       count, 0, coord, 0, 0)


def _coords(skin):
    return [struct.unpack_from("<H", b, 18)[0] for b in skin.batches]


def test_batches_past_an_empty_lookup_table_get_runs_of_their_own():
    """Retail writes the table empty; Noggit logged every batch and 3.3.5a
    reads past the end of it."""
    from wotlkconv.m2.model import M2Model
    from wotlkconv.m2.skin import Skin
    from wotlkconv.m2.texcoords import assign_texture_coord_combos
    model = M2Model()
    skin = Skin(batches=[_batch(0x8000, 2, 0), _batch(0, 1, 0xFFFF),
                         _batch(0x8000 | 32, 1, 0), _batch(0x8000 | 25, 1, 0)])
    res = FileResult(source="t.m2", kind="m2")
    assign_texture_coord_combos(model, {0: skin, 1: skin}, res)
    assert model.texture_coord_combos == [0, 0xFFFF]
    assert _coords(skin) == [0, 0, 0, 1]
    assert any(n.code == "m2.texture_coord_combos.rebuilt" for n in res.notes)


def test_batches_already_in_range_are_left_alone():
    from wotlkconv.m2.model import M2Model
    from wotlkconv.m2.skin import Skin
    from wotlkconv.m2.texcoords import assign_texture_coord_combos
    model = M2Model()
    model.texture_coord_combos = [0xFFFF, 1]
    skin = Skin(batches=[_batch(0, 1, 1), _batch(0x8000, 2, 0)])
    before = list(skin.batches)
    res = FileResult(source="t.m2", kind="m2")
    assign_texture_coord_combos(model, {0: skin}, res)
    assert model.texture_coord_combos == [0xFFFF, 1] and skin.batches == before
    assert not res.notes


def test_a_converted_model_covers_every_batch_lookup(listfile, source, asset_dir):
    for fid in (910000, 910001, 910002, 910003):
        (asset_dir / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    model = F.build_modern_model()
    model.texture_coord_combos = []
    out, _res, companions = convert_m2(F.serialise_modern_m2(model), "t.m2",
                                       Options(), listfile, source)
    table = parse_m2(out, "o.m2").texture_coord_combos
    skins = [parse_skin(c.data, c.filename) for c in companions
             if c.filename.endswith(".skin")]
    assert table == [0, 0xFFFF] and skins
    for skin in skins:
        for raw in skin.batches:
            count, coord = struct.unpack_from("<H", raw, 14)[0], \
                struct.unpack_from("<H", raw, 18)[0]
            assert coord + count - 1 < len(table)


# ---------------------------------------------------------------------------
# Where the model counts its .anim offsets from
# ---------------------------------------------------------------------------
def _model_with_external_keys(offset: int, keys: int = 2):
    """A model whose sequence 1 keeps `keys` keyframes at `offset`."""
    model = F.build_modern_model(sequences=2, external_sequence=1)
    model.bones[0]["translation"] = F.external_track(
        "vec3", sequences=2, index=1, offset=offset, keys=keys)
    return model


def test_offsets_counted_from_the_payload_are_recognised():
    """A span starting at 0 cannot be counted from the file: 0 is the magic."""
    # 2 timestamps (8 bytes) then 2 vec3 values (24) = 32 bytes of payload.
    model = _model_with_external_keys(0)
    out, res = convert_anim(F.build_anim(b"\x01" * 32), "t.anim", Options(),
                            model, anim_id=1, sub_id=0)
    assert len(out) == 32                      # the payload alone
    assert any(n.code == "anim.offsets.payload" for n in res.notes)


def test_offsets_counted_from_the_file_keep_the_header_as_padding():
    """The keys sit 8 bytes in, so the payload must stay 8 bytes in."""
    model = _model_with_external_keys(8)
    out, res = convert_anim(F.build_anim(b"\x01" * 32), "t.anim", Options(),
                            model, anim_id=1, sub_id=0)
    assert len(out) == 40                      # 8 bytes of header + 32
    assert out[8:] == b"\x01" * 32
    assert any(n.code == "anim.offsets.file" for n in res.notes)


def test_a_span_running_past_the_payload_end_settles_it():
    """32 bytes of keys starting 8 in need 40: only the file reading holds."""
    spans = anim_spans(_model_with_external_keys(8), 1, 0)
    payload_base, file_base = measure_offset_base(spans, payload_len=32,
                                                  body_start=8)
    assert not payload_base.usable                  # 8 bytes short
    assert file_base.usable and file_base.exact


def test_the_reading_that_fills_the_file_exactly_wins():
    """Both readings fit a roomier file; one ends where the file does."""
    spans = anim_spans(_model_with_external_keys(8), 1, 0)
    payload_base, file_base = measure_offset_base(spans, payload_len=40,
                                                  body_start=8)
    assert payload_base.usable and file_base.usable
    assert payload_base.exact and not file_base.exact


def test_where_nothing_distinguishes_them_the_payload_is_kept():
    """Both fit, neither fills: prefer the reading that rewrites nothing."""
    spans = anim_spans(_model_with_external_keys(8), 1, 0)
    payload_base, file_base = measure_offset_base(spans, payload_len=64,
                                                  body_start=8)
    assert payload_base.usable and file_base.usable
    assert not payload_base.exact and not file_base.exact
    out, res = convert_anim(F.build_anim(b"\x01" * 64), "t.anim", Options(),
                            _model_with_external_keys(8), anim_id=1, sub_id=0)
    assert len(out) == 64
    assert any(n.code == "anim.offsets.payload" for n in res.notes)


def test_a_model_that_names_nothing_leaves_the_offsets_alone():
    model = F.build_modern_model(sequences=2)     # nothing external
    out, res = convert_anim(F.build_anim(b"\x01" * 32), "t.anim", Options(),
                            model, anim_id=1, sub_id=0)
    assert len(out) == 32
    assert any(n.code == "anim.offsets.unmeasured" for n in res.notes)


def test_spans_that_fit_neither_reading_are_reported():
    model = _model_with_external_keys(9000)
    out, res = convert_anim(F.build_anim(b"\x01" * 32), "t.anim", Options(),
                            model, anim_id=1, sub_id=0)
    assert len(out) == 32                      # unchanged, and said so
    assert any(n.code == "anim.offsets.unfit" for n in res.notes)


def test_only_the_animation_this_file_covers_is_measured():
    model = _model_with_external_keys(8)
    assert anim_spans(model, 1, 0)             # sequence 1 is this file
    assert anim_spans(model, 0, 0) == []       # sequence 0 is embedded
    assert anim_spans(model, 7, 0) == []       # no such animation


# ---------------------------------------------------------------------------
# Skeleton keyframes: AFSB and AFSA
# ---------------------------------------------------------------------------
# A model rigged by a .skel splits each .anim three ways, and every chunk
# counts its offsets from its own start.  On retail character models AFM2 is a
# hundred-odd bytes of colour keys while AFSB holds every bone's keyframes.
def _skeleton_rigged_model(sequences: int = 2):
    """Sequence 1 external: 2 colour keys, 2 bone keys and 2 attachment keys,
    each at offset 0 of its own chunk."""
    model = F.build_modern_model(sequences=sequences, external_sequence=1)
    model.bones_from_skeleton = model.attachments_from_skeleton = True
    model.colors[0]["color"] = F.external_track("vec3", sequences, 1, 0)
    model.bones[0]["translation"] = F.external_track("vec3", sequences, 1, 0)
    model.attachments[0]["animate_attached"] = F.external_track(
        "u8", sequences, 1, 0)
    return model


AFM2 = bytes(range(32))                        # 8 timestamp + 24 vec3 bytes
AFSB = bytes(range(100, 132))                  # the same shape, for a bone
AFSA = bytes(range(200, 210))                  # 8 timestamp + 2 u8 bytes


def test_skeleton_keyframes_follow_the_model_tracks_and_are_repointed():
    model = _skeleton_rigged_model()
    out, res = convert_anim(F.build_anim(AFM2, afsb=AFSB, afsa=AFSA), "t.anim",
                            Options(), model, anim_id=1, sub_id=0)
    assert out == AFM2 + AFSB + AFSA           # 32 + 32, then 64 + 10
    bone = model.bones[0]["translation"]
    assert bone.timestamp_spans[1] == (2, 32) and bone.value_spans[1] == (2, 40)
    att = model.attachments[0]["animate_attached"]
    assert att.timestamp_spans[1] == (2, 64) and att.value_spans[1] == (2, 72)
    assert model.colors[0]["color"].timestamp_spans[1] == (2, 0)   # stays
    # Nothing was thrown away, so nothing is lossy.
    assert res.status is Status.OK
    assert [n.detail["base"] for n in res.notes
            if n.code == "anim.skeleton.placed"] == [32, 64]


def test_skeleton_keyframes_are_aligned_to_four_bytes():
    model = _skeleton_rigged_model()
    out, _res = convert_anim(F.build_anim(AFM2 + b"\xEE", afsb=AFSB, afsa=None),
                             "t.anim", Options(), model, anim_id=1, sub_id=0)
    assert out[36:68] == AFSB
    assert model.bones[0]["translation"].timestamp_spans[1] == (2, 36)


def test_an_alias_is_repointed_with_the_sequence_it_plays():
    # Retail aliases (flag 0x40) carry the same spans as their target and
    # have no .anim of their own; left unmoved they address the wrong bytes.
    model = _skeleton_rigged_model(sequences=3)
    model.sequences[2].update(flags=0x40, alias_next=1)
    bone = model.bones[0]["translation"]
    bone.timestamp_spans[2], bone.value_spans[2] = (2, 0), (2, 8)
    bone.external.add(2)
    convert_anim(F.build_anim(AFM2, afsb=AFSB, afsa=None), "t.anim", Options(),
                 model, anim_id=1, sub_id=0)
    assert bone.timestamp_spans[2] == bone.timestamp_spans[1] == (2, 32)


def test_a_skeleton_only_anim_is_flattened_not_passed_through():
    model = _skeleton_rigged_model()
    model.colors[0]["color"] = F.make_track("vec3", 2)
    out, res = convert_anim(F.build_anim(None, afsb=AFSB, afsa=None), "t.anim",
                            Options(), model, anim_id=1, sub_id=0)
    assert out == AFSB
    assert res.status is not Status.PASSTHROUGH
    assert model.bones[0]["translation"].timestamp_spans[1] == (2, 0)


def test_skeleton_keyframes_that_do_not_fit_are_reported_and_left_alone():
    model = _skeleton_rigged_model()
    model.bones[0]["translation"] = F.external_track("vec3", 2, 1, 9000)
    out, res = convert_anim(F.build_anim(AFM2, afsb=AFSB, afsa=None), "t.anim",
                            Options(), model, anim_id=1, sub_id=0)
    assert out == AFM2
    assert model.bones[0]["translation"].timestamp_spans[1] == (2, 9000)
    assert any(n.code == "anim.skeleton.unfit" for n in res.notes)


def test_a_model_with_its_own_rig_does_not_claim_skeleton_keyframes():
    model = _skeleton_rigged_model()
    model.bones_from_skeleton = False          # AFM2 is where its bones point
    _out, res = convert_anim(F.build_anim(AFM2, afsb=AFSB, afsa=None), "t.anim",
                             Options(), model, anim_id=1, sub_id=0)
    assert any(n.code == "anim.skeleton.unplaced" for n in res.notes)
    assert model.bones[0]["translation"].timestamp_spans[1] == (2, 0)


# ---------------------------------------------------------------------------
# Which tracks were read before their sequences were known
# ---------------------------------------------------------------------------
def test_external_bone_keys_are_not_read_out_of_the_skeleton():
    # The offset addresses the .anim, but 16 bytes into SKB1 is also a valid
    # place to read from -- which is what happened to 16,948 of 17,031 such
    # spans on a retail human model.
    skel = parse_skel(F.build_skel(sequences=2, external_sequence=1), "t.skel")
    track = skel.bones[0]["translation"]
    assert 1 in track.external
    assert track.timestamps[1] == [] and track.values[1] == []
    assert track.timestamp_spans[1] == (2, 16)
    assert track.timestamps[0]                 # the embedded one is kept


def test_a_parent_skeletons_sequences_settle_the_childs_bones(source, asset_dir):
    (asset_dir / "940000.skel").write_bytes(
        F.build_skel(bones=0, sequences=2, attachments=0, external_sequence=1))
    (asset_dir / "941000.skel").write_bytes(
        F.build_skel(bones=3, sequences=0, attachments=0, track_sequences=2,
                     external_sequence=1, parent_id=940000))
    merged = load_skeleton_chain(source.loader_for(".skel"), 941000, "child")
    track = merged.bones[0]["translation"]
    assert 1 in track.external and track.timestamps[1] == []


def test_a_rigged_models_own_tracks_are_settled_by_the_skeleton():
    m = F.build_modern_model(sequences=0, bones=0)
    m.bones = m.sequences = m.attachments = []
    m.key_bone_lookup = m.sequence_lookups = m.attachment_lookup = []
    m.colors[0]["color"] = F.external_track("vec3", 2, 1, 16)
    model = parse_m2(F.serialise_modern_m2(m, skeleton_id=940000, anim_ids=()),
                     "c.m2")
    # With no sequences of its own, the model read offset 16 out of itself.
    assert model.colors[0]["color"].timestamps[1]
    skel = parse_skel(F.build_skel(sequences=2, external_sequence=1), "t.skel")
    merge_skeleton(model, skel, FileResult(source="c.m2"))
    track = model.colors[0]["color"]
    assert 1 in track.external and track.timestamps[1] == []
    assert model.bones_from_skeleton and model.attachments_from_skeleton


def test_a_rigged_models_animation_survives_conversion(listfile, source,
                                                       asset_dir):
    """Every key of an external animation is where the written model says."""
    (asset_dir / "940000.skel").write_bytes(F.build_skel(
        bones=2, sequences=2, external_sequence=1, anim_ids=((1, 0, 950001),)))
    m = F.build_modern_model(sequences=0, bones=0)
    m.bones = m.sequences = m.attachments = []
    m.key_bone_lookup = m.sequence_lookups = m.attachment_lookup = []
    m.colors[0]["color"] = F.external_track("vec3", 2, 1, 8)
    afm2 = bytes(range(40))                    # colour keys at 8..40
    afsb = bytes(range(60, 108))               # bone keys at 16..48
    (asset_dir / "950001.anim").write_bytes(F.build_anim(afm2, afsb=afsb,
                                                         afsa=None))

    raw = F.serialise_modern_m2(m, skeleton_id=940000, anim_ids=())
    out, res, companions = convert_m2(raw, "char.m2", Options(), listfile,
                                      source)
    assert res.ok
    anim = next(c.data for c in companions if c.filename == "char0001-00.anim")
    back = parse_m2(out, "o.m2")

    def keys(track, spans, size):
        count, offset = spans[1]
        return anim[offset:offset + count * size]

    for bone in back.bones:
        t = bone["translation"]
        assert keys(t, t.timestamp_spans, 4) == afsb[16:24]
        assert keys(t, t.value_spans, 12) == afsb[24:48]
    color = back.colors[0]["color"]
    assert keys(color, color.timestamp_spans, 4) == afm2[8:16]
    assert keys(color, color.value_spans, 12) == afm2[16:40]


# ---------------------------------------------------------------------------
# The companion cache
# ---------------------------------------------------------------------------
def test_the_companion_cache_stays_inside_its_budget(asset_dir):
    # Unbounded, each worker of a retail build kept every skeleton, skin and
    # animation it had ever read: ~30 MB a minute apiece.
    from wotlkconv.listfile import Listfile
    from wotlkconv.resolve import AssetSource

    for fid in range(10):
        (asset_dir / f"{970000 + fid}.skel").write_bytes(bytes([fid]) * 1000)
    source = AssetSource(Listfile(), roots=[asset_dir], cache_bytes=3500)
    for fid in range(10):
        assert source.by_file_id(970000 + fid, ".skel") == bytes([fid]) * 1000
        assert source._cached_bytes <= 3500    
    # The most recent lookups are still served from memory...
    (asset_dir / "970009.skel").unlink()
    assert source.by_file_id(970009, ".skel") == bytes([9]) * 1000
    # ...and the evicted ones are simply read again.
    assert source.by_file_id(970000, ".skel") == bytes([0]) * 1000


def test_the_companion_cache_caps_entries_as_well_as_bytes(asset_dir):
    from wotlkconv.listfile import Listfile
    from wotlkconv.resolve import AssetSource

    source = AssetSource(Listfile(), roots=[asset_dir], cache_entries=5)
    for fid in range(50):                      # misses cost an entry each
        assert source.by_file_id(980000 + fid, ".skel") is None
    assert len(source._cache) <= 5


# ---------------------------------------------------------------------------
# Sequences with nothing to load
# ---------------------------------------------------------------------------
def test_sequences_with_no_keyframes_and_no_file_are_marked_embedded():
    from wotlkconv.m2.convert import ConvertedAsset, _embed_empty_sequences
    from wotlkconv.m2.model import M2Model
    from wotlkconv.m2.types import Track
    model = M2Model()
    model.sequences = [
        {"id": 0, "variation_index": 0, "flags": 0},      # nothing anywhere
        {"id": 1, "variation_index": 0, "flags": 0},      # keys in a track
        {"id": 2, "variation_index": 0, "flags": 0x40},   # alias
        {"id": 3, "variation_index": 1, "flags": 0},      # its .anim was written
        {"id": 4, "variation_index": 0, "flags": 0x20},   # already embedded
    ]
    model.bones = [{"translation": Track(timestamp_spans=[(0, 0), (5, 100), (0, 0), (0, 0), (0, 0)])}]
    anims = [ConvertedAsset("beast0003-01.anim", b"AFM2", FileResult(source="a", kind="anim"))]
    res = FileResult(source="beast.m2", kind="m2")
    _embed_empty_sequences(model, "beast", anims, res)
    assert [s["flags"] for s in model.sequences] == [0x20, 0, 0x40, 0, 0x20]
    assert any(n.code == "m2.sequence.embedded" for n in res.notes)
