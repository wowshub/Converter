"""End-to-end pipeline and CLI behaviour."""

import json
from pathlib import Path

import fixtures as F
import pytest
from conftest import LISTFILE_ENTRIES

from wotlkconv import detect
from wotlkconv.blp.blp import PreferredFormat
from wotlkconv.chunks import ChunkReader
from wotlkconv.cli import main
from wotlkconv.listfile import Listfile
from wotlkconv.options import Options
from wotlkconv.pipeline import plan, run
from wotlkconv.report import Status


@pytest.fixture
def extraction(tmp_path: Path) -> Path:
    """A synthetic extraction laid out the way wow.export dumps by FileDataID."""
    src = tmp_path / "in"
    src.mkdir()

    model = F.build_modern_model()
    (src / "123456.m2").write_bytes(
        F.serialise_modern_m2(model, skeleton_id=940000))
    for fid in (910000, 910001, 910002, 910003):
        (src / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    for fid in (920000, 920001):
        (src / f"{fid}.anim").write_bytes(F.build_anim())
    (src / "940000.skel").write_bytes(F.build_skel(bones=4, sequences=2))

    (src / "900000.blp").write_bytes(
        F.build_blp(F.build_gradient_image(32, 32), PreferredFormat.DXT5))
    (src / "900001.blp").write_bytes(F.build_bc5_blp(16, 16))

    wmo_dir = src / "world" / "wmo"
    wmo_dir.mkdir(parents=True)
    (wmo_dir / "House.wmo").write_bytes(F.build_modern_wmo_root())
    (src / "830001.wmo").write_bytes(F.build_modern_wmo_group())
    (src / "830002.wmo").write_bytes(F.build_modern_wmo_group())

    maps = src / "world" / "maps" / "azeroth"
    maps.mkdir(parents=True)
    root, tex, obj = F.build_split_adt(chunks=2)
    (maps / "Azeroth_32_48.adt").write_bytes(root)
    (maps / "Azeroth_32_48_tex0.adt").write_bytes(tex)
    (maps / "Azeroth_32_48_obj0.adt").write_bytes(obj)

    (tmp_path / "listfile.csv").write_text("\n".join(LISTFILE_ENTRIES) + "\n")
    return src


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def test_plan_classifies_every_input(extraction):
    jobs, skipped = plan([extraction])
    kinds = sorted(j.kind for j in jobs)
    assert kinds == [detect.ADT, detect.BLP, detect.BLP, detect.M2, detect.WMO_ROOT]
    # A .skel is the only thing here 3.3.5a has no place for on its own.
    truly_skipped = [s.kind for s in skipped if s.status is Status.SKIPPED]
    assert truly_skipped == ["skel"]


def test_every_input_file_is_accounted_for(extraction, tmp_path):
    """Nothing may leave a run without being mentioned somewhere."""
    on_disk = sum(1 for p in extraction.rglob("*") if p.is_file())
    report, _files = convert_tree(extraction, tmp_path / "out")
    assert report.accounting()["inputs"] == on_disk


def test_a_terrain_piece_folded_into_its_tile_is_recorded_as_merged(extraction):
    """Nothing else reports it: the tile is written as a single file."""
    _jobs, skipped = plan([extraction])
    merged = [s for s in skipped if s.status is Status.MERGED]
    assert {s.kind for s in merged} == {detect.ADT}
    assert sorted(Path(s.source).name for s in merged) == [
        "Azeroth_32_48_obj0.adt", "Azeroth_32_48_tex0.adt"]
    assert all(s.notes[0].code == "plan.merged" for s in merged)


def test_a_companion_is_reported_by_the_asset_that_converts_it(extraction,
                                                               tmp_path):
    """It is converted under the name the client globs for, and counted once."""
    report, _files = convert_tree(extraction, tmp_path / "out")
    sources = [f.source for f in report.files]
    assert len(sources) == len(set(sources)), "a file was counted twice"
    assert any(s.endswith("00.skin") for s in sources)


def test_split_terrain_pieces_are_grouped(extraction):
    jobs, _ = plan([extraction])
    adt = next(j for j in jobs if j.kind == detect.ADT)
    assert sorted(adt.extra) == ["obj0", "tex0"]
    assert adt.source.name == "Azeroth_32_48.adt"


def test_companions_are_not_planned_as_separate_jobs(extraction):
    jobs, _ = plan([extraction])
    names = {j.source.name for j in jobs}
    assert not (names & {"910000.skin", "920000.anim", "830001.wmo"})


def test_companion_claiming_can_be_turned_off(extraction):
    jobs, _ = plan([extraction], claim_companions=False)
    names = {j.source.name for j in jobs}
    assert {"910000.skin", "920000.anim", "830001.wmo"} <= names


def test_split_pieces_without_a_root_are_skipped(tmp_path):
    d = tmp_path / "partial"
    d.mkdir()
    _root, tex, obj = F.build_split_adt(chunks=1)
    (d / "Foo_1_1_tex0.adt").write_bytes(tex)
    (d / "Foo_1_1_obj0.adt").write_bytes(obj)
    jobs, skipped = plan([d])
    assert jobs == []
    assert any(n.code == "adt.no_root" for s in skipped for n in s.notes)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def convert_tree(extraction: Path, out: Path, **kw) -> tuple:
    listfile = Listfile.load(extraction.parent / "listfile.csv")
    jobs, skipped = plan([extraction])
    report = run(jobs, Options(**kw), listfile, out, roots=[str(extraction)],
                 listfile_path=str(extraction.parent / "listfile.csv"),
                 skipped=skipped)
    return report, sorted(p.relative_to(out).as_posix()
                          for p in out.rglob("*") if p.is_file())


def test_outputs_use_the_layout_the_client_globs_for(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out")
    assert files == [
        "creature/testbeast/testbeast.m2",
        "creature/testbeast/testbeast00.skin",
        "creature/testbeast/testbeast0000-00.anim",
        "creature/testbeast/testbeast0001-00.anim",
        "creature/testbeast/testbeast01.skin",
        "creature/testbeast/testbeast02.skin",
        "creature/testbeast/testbeast03.skin",
        "creature/testbeast/testbeast_normal.blp",
        "creature/testbeast/testbeast_skin.blp",
        "world/maps/azeroth/Azeroth_32_48.adt",
        "world/wmo/House.wmo",
        "world/wmo/House_000.wmo",
        "world/wmo/House_001.wmo",
    ]


def test_every_output_is_loadable_by_the_wrath_parsers(extraction, tmp_path):
    out = tmp_path / "out"
    convert_tree(extraction, out)
    from wotlkconv.adt import inspect_adt
    from wotlkconv.blp import inspect_blp
    from wotlkconv.m2 import inspect_m2, inspect_skin
    from wotlkconv.wmo import inspect_wmo_root

    m2 = inspect_m2((out / "creature/testbeast/testbeast.m2").read_bytes(), "m")
    assert m2["version"] == 264 and not m2["chunked"]
    assert inspect_skin(
        (out / "creature/testbeast/testbeast00.skin").read_bytes(),
        "s")["header"] == "wotlk"
    assert inspect_blp(
        (out / "creature/testbeast/testbeast_normal.blp").read_bytes(),
        "b")["wotlk_compatible"]
    assert inspect_wmo_root(
        (out / "world/wmo/House.wmo").read_bytes(), "w")["wotlk_compatible"]
    assert inspect_adt(
        (out / "world/maps/azeroth/Azeroth_32_48.adt").read_bytes(),
        "a")["wotlk_compatible"]


def test_numeric_names_can_be_kept(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out",
                                  name_from_listfile=False)
    assert "123456.m2" in files
    assert "123456" + "00.skin" in files


def test_flatten_writes_everything_to_the_root(extraction, tmp_path):
    _report, files = convert_tree(extraction, tmp_path / "out", flatten=True)
    assert all("/" not in f for f in files)


def test_dry_run_writes_nothing(extraction, tmp_path):
    report, files = convert_tree(extraction, tmp_path / "out", dry_run=True)
    assert files == []
    assert report.counts()


def test_existing_files_are_kept_unless_overwrite(extraction, tmp_path):
    out = tmp_path / "out"
    convert_tree(extraction, out)
    target = out / "creature/testbeast/testbeast.m2"
    target.write_bytes(b"SENTINEL")
    convert_tree(extraction, out)
    assert target.read_bytes() == b"SENTINEL"
    convert_tree(extraction, out, overwrite=True)
    assert target.read_bytes() != b"SENTINEL"


def test_parallel_and_serial_runs_agree(extraction, tmp_path):
    _r1, serial = convert_tree(extraction, tmp_path / "a")
    _r2, parallel = convert_tree(extraction, tmp_path / "b", jobs=3)
    assert serial == parallel
    for name in serial:
        assert (tmp_path / "a" / name).read_bytes() == \
            (tmp_path / "b" / name).read_bytes()


def test_the_pool_is_fed_a_window_at_a_time_and_in_order():
    from concurrent.futures import ThreadPoolExecutor

    from wotlkconv.pipeline import _bounded_map

    def work(item):
        return item * 2

    class CountingPool(ThreadPoolExecutor):
        submitted = 0

        def submit(self, fn, *args):
            CountingPool.submitted += 1
            return super().submit(fn, *args)

    with CountingPool(max_workers=2) as pool:
        seen = []
        for index, value in _bounded_map(pool, work, list(range(50)), 4):
            # Never more than the window handed over ahead of what came back.
            assert CountingPool.submitted - len(seen) <= 4
            seen.append((index, value))
    assert seen == [(i, i * 2) for i in range(50)]


def test_a_broken_pool_redoes_only_the_unfinished_jobs(extraction, tmp_path,
                                                       monkeypatch):
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    from wotlkconv import pipeline

    listfile = Listfile.load(extraction.parent / "listfile.csv")
    jobs, skipped = plan([extraction])
    assert len(jobs) > 3

    class DyingPool:
        """Runs the first two jobs, then loses its workers."""

        def __init__(self, *args, **kwargs):
            self.calls = 0
            self.converter = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, job):
            self.calls += 1
            future = Future()
            if self.calls > 2:
                future.set_exception(BrokenProcessPool("worker died"))
                return future
            if self.converter is None:
                source = pipeline.AssetSource(listfile, roots=[str(extraction)])
                self.converter = pipeline.Converter(
                    Options(jobs=2), listfile, tmp_path / "out", source)
            outputs = self.converter.convert(job)
            self.converter.write(outputs)
            future.set_result(outputs)
            return future

    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", DyingPool)
    report = run(jobs, Options(jobs=2), listfile, tmp_path / "out",
                 roots=[str(extraction)], skipped=skipped)
    sources = [f.source for f in report.files]
    assert len(sources) == len(set(sources))   # nothing reported twice
    serial = run(jobs, Options(), listfile, tmp_path / "serial",
                 roots=[str(extraction)], skipped=skipped)
    assert sorted(sources) == sorted(f.source for f in serial.files)


def test_a_corrupt_file_fails_alone(extraction, tmp_path):
    (extraction / "broken.m2").write_bytes(b"MD20" + b"\xff" * 600)
    report, files = convert_tree(extraction, tmp_path / "out")
    assert [f.source for f in report.failed] == ["broken.m2"]
    assert any(f.startswith("creature/") for f in files)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_convert(extraction, tmp_path, capsys):
    out = tmp_path / "cli-out"
    code = main(["convert", str(extraction), "-o", str(out),
                 "--listfile", str(extraction.parent / "listfile.csv"),
                 "--report", str(tmp_path / "r.json")])
    assert code == 0
    assert (out / "creature/testbeast/testbeast.m2").is_file()
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["target"]["build"] == 12340
    assert report["counts"]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_big_cli_run_spools_its_results_and_says_the_same(extraction, tmp_path,
                                                             monkeypatch, jobs):
    from wotlkconv import cli

    args = ["convert", str(extraction), "--listfile",
            str(extraction.parent / "listfile.csv"), "-j", str(jobs)]
    assert main([*args, "-o", str(tmp_path / "a"), "--report", str(tmp_path / "held.json")]) == 0
    monkeypatch.setattr(cli, "SPOOL_THRESHOLD", 0)
    assert main([*args, "-o", str(tmp_path / "b"), "--report", str(tmp_path / "spooled.json")]) == 0

    held = json.loads((tmp_path / "held.json").read_text())
    spooled = json.loads((tmp_path / "spooled.json").read_text())
    assert spooled["counts"] == held["counts"]
    assert spooled["accounting"] == held["accounting"]
    def key(f):
        return f["source"]
    def strip(f):
        return {k: v for k, v in f.items()
                           if k not in ("elapsed_ms", "target")}
    assert [strip(f) for f in sorted(spooled["files"], key=key)] == \
        [strip(f) for f in sorted(held["files"], key=key)]
    assert not (tmp_path / "spooled.results.jsonl").exists()   # cleaned up


def test_cli_convert_reports_failures_in_its_exit_code(tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    (src / "broken.m2").write_bytes(b"MD20" + b"\xff" * 600)
    assert main(["convert", str(src), "-o", str(tmp_path / "o")]) == 1


def test_cli_inspect_json(extraction, capsys):
    code = main(["inspect", str(extraction / "123456.m2"), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["version"] == 272
    assert payload[0]["wotlk_compatible"] is False


def test_cli_plan_lists_jobs(extraction, capsys):
    assert main(["plan", str(extraction)]) == 0
    out = capsys.readouterr().out
    assert "Azeroth_32_48.adt (+obj0, tex0)" in out
    assert "940000.skel" in out


def test_cli_listfile_summary(extraction, capsys):
    path = extraction.parent / "listfile.csv"
    assert main(["listfile", str(path), "--lookup", "123456",
                 "--lookup", "world/wmo/tex1.blp"]) == 0
    out = capsys.readouterr().out
    assert "creature/testbeast/testbeast.m2".replace("/", "\\") in out
    assert "800001" in out


def test_cli_rejects_an_empty_input_set(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["convert", str(empty), "-o", str(tmp_path / "o")]) == 1


# ---------------------------------------------------------------------------
# Convert / copy / skip classification
# ---------------------------------------------------------------------------
@pytest.fixture
def mixed_tree(tmp_path: Path) -> Path:
    src = tmp_path / "mixed"
    src.mkdir()
    (src / "art.blp").write_bytes(
        F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT1))
    (src / "sound.wav").write_bytes(b"RIFF\0\0\0\0WAVEfmt ")
    (src / "ui.lua").write_bytes(b"-- script\n")
    (src / "db.db2").write_bytes(b"WDC5" + b"\0" * 40)
    (src / "hd.tex").write_bytes(b"\0" * 40)
    (src / "rig.skel").write_bytes(F.build_skel(bones=1, sequences=1))
    return src


def test_formats_the_client_reads_unchanged_are_copied(mixed_tree, tmp_path):
    jobs, _skipped = plan([mixed_tree])
    actions = {j.source.name: j.action for j in jobs}
    assert actions == {"art.blp": detect.CONVERT,
                       "sound.wav": detect.COPY,
                       "ui.lua": detect.COPY}


def test_copied_files_arrive_byte_for_byte(mixed_tree, tmp_path):
    out = tmp_path / "out"
    jobs, skipped = plan([mixed_tree])
    run(jobs, Options(), Listfile(), out, skipped=skipped)
    assert (out / "sound.wav").read_bytes() == (mixed_tree / "sound.wav").read_bytes()
    assert (out / "ui.lua").read_bytes() == (mixed_tree / "ui.lua").read_bytes()


def test_unusable_formats_are_skipped_with_a_specific_reason(mixed_tree):
    _jobs, skipped = plan([mixed_tree])
    reasons = {Path(s.source).name: s.notes[0].message for s in skipped}
    assert "wotlkconv db convert" in reasons["db.db2"]
    assert "high-resolution" in reasons["hd.tex"]
    assert "merged into the model" in reasons["rig.skel"]


def test_databases_join_a_run_once_definitions_are_available(mixed_tree):
    jobs, skipped = plan([mixed_tree], convert_databases=True)
    assert detect.DB2 in {j.kind for j in jobs}
    assert "db.db2" not in {Path(s.source).name for s in skipped}


def test_copying_can_be_turned_off(mixed_tree):
    jobs, skipped = plan([mixed_tree], copy_unconverted=False)
    assert {j.source.name for j in jobs} == {"art.blp"}
    assert any("--no-copy-unconverted" in s.notes[0].message for s in skipped)


# ---------------------------------------------------------------------------
# Converting straight out of a CASC install
# ---------------------------------------------------------------------------
@pytest.fixture
def casc_install(tmp_path: Path):
    import casc_fixtures as CF
    root, tex, obj = F.build_split_adt(chunks=2)
    files = {
        123456: F.serialise_modern_m2(F.build_modern_model(), skeleton_id=940000),
        910000: F.build_skin(legion=True), 910001: F.build_skin(legion=True),
        910002: F.build_skin(legion=True), 910003: F.build_skin(legion=True),
        920000: F.build_anim(), 920001: F.build_anim(),
        940000: F.build_skel(bones=4, sequences=2),
        900000: F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT5),
        900001: F.build_bc5_blp(16, 16),
        830000: F.build_modern_wmo_root(),
        830001: F.build_modern_wmo_group(), 830002: F.build_modern_wmo_group(),
        700100: root, 700101: tex, 700102: obj,
        600001: b"RIFF\0\0\0\0WAVEfmt ",
        600003: b"WDC5" + b"\0" * 40,
    }
    CF.build_install(tmp_path / "wow", files)
    entries = ["123456;creature/testbeast/testbeast.m2", "910000;creature/testbeast/testbeast00.skin", "910001;creature/testbeast/testbeast01.skin", "910002;creature/testbeast/testbeast02.skin", "910003;creature/testbeast/testbeast03.skin", "920000;creature/testbeast/testbeast0000-00.anim", "920001;creature/testbeast/testbeast0001-00.anim", "940000;creature/testbeast/testbeast.skel", "900000;creature/testbeast/testbeast_skin.blp", "900001;creature/testbeast/testbeast_normal.blp", "830000;world/wmo/house/house.wmo", "830001;world/wmo/house/house_000.wmo", "830002;world/wmo/house/house_001.wmo", "700100;world/maps/azeroth/azeroth_32_48.adt", "700101;world/maps/azeroth/azeroth_32_48_tex0.adt", "700102;world/maps/azeroth/azeroth_32_48_obj0.adt", "600001;sound/creature/bear/attack.wav", "600003;dbfilesclient/creaturedisplayinfo.db2", *LISTFILE_ENTRIES]
    (tmp_path / "listfile.csv").write_text("\n".join(entries) + "\n")
    return tmp_path / "wow", tmp_path / "listfile.csv"


def test_casc_selection_groups_and_claims_like_the_disk_planner(casc_install):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
        # Skins, anims and WMO groups are claimed by the model and root that
        # pull them in, so they are not planned separately; the .wav is
        # recognised as audio and copied.
        assert sorted(j.kind for j in jobs) == [
            detect.ADT, detect.BLP, detect.BLP, detect.M2,
            detect.WAV, detect.WMO_ROOT]
        assert [j.action for j in jobs if j.kind == detect.WAV] == [detect.COPY]
        # The tile's three pieces became one job.
        adt = next(j for j in jobs if j.kind == detect.ADT)
        assert sorted(adt.extra_ids) == ["obj0", "tex0"]
        assert any("db convert" in s.notes[0].message for s in skipped)


def test_casc_convert_produces_the_client_layout(casc_install, tmp_path):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    out = tmp_path / "out"
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
        report = run(jobs, Options(), listfile, out, skipped=skipped,
                     storage=storage)
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                   if p.is_file())
    assert files == [
        "creature/testbeast/testbeast.m2",
        "creature/testbeast/testbeast00.skin",
        "creature/testbeast/testbeast0000-00.anim",
        "creature/testbeast/testbeast0001-00.anim",
        "creature/testbeast/testbeast01.skin",
        "creature/testbeast/testbeast02.skin",
        "creature/testbeast/testbeast03.skin",
        "creature/testbeast/testbeast_normal.blp",
        "creature/testbeast/testbeast_skin.blp",
        "sound/creature/bear/attack.wav",
        "world/maps/azeroth/azeroth_32_48.adt",
        "world/wmo/house/house.wmo",
        "world/wmo/house/house_000.wmo",
        "world/wmo/house/house_001.wmo",
    ]
    assert not report.failed


def test_casc_convert_merges_the_split_tile(casc_install, tmp_path):
    from wotlkconv.adt import inspect_adt
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile_path = casc_install
    listfile = Listfile.load(listfile_path)
    out = tmp_path / "out"
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["world/maps/**"])
        run(jobs, Options(), listfile, out, skipped=skipped, storage=storage)
    info = inspect_adt(
        (out / "world/maps/azeroth/azeroth_32_48.adt").read_bytes(), "a")
    assert info["wotlk_compatible"] and info["map_chunks"] == 2


def test_casc_selection_by_file_id_needs_no_listfile(casc_install, tmp_path):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, _listfile_path = casc_install
    with CascStorage.open(install) as storage:
        jobs, _skipped = plan_casc(storage, Listfile(), file_ids=[900000])
    assert len(jobs) == 1
    assert jobs[0].file_id == 900000
    assert jobs[0].relpath.startswith("unknown/")


def test_cli_convert_from_casc(casc_install, tmp_path):
    install, listfile_path = casc_install
    out = tmp_path / "cli-out"
    code = main(["convert", "--casc", str(install), "-l", str(listfile_path),
                 "--include", "creature/**", "-o", str(out)])
    assert code == 0
    assert (out / "creature/testbeast/testbeast.m2").is_file()
    assert (out / "creature/testbeast/testbeast00.skin").is_file()


def test_cli_casc_selection_is_required(casc_install, tmp_path):
    install, listfile_path = casc_install
    assert main(["convert", "--casc", str(install), "-l", str(listfile_path),
                 "-o", str(tmp_path / "o")]) == 1


def test_cli_casc_info(casc_install, capsys):
    install, _ = casc_install
    assert main(["casc", "info", "--casc", str(install)]) == 0
    assert "product    wow" in capsys.readouterr().out


def test_cli_casc_list(casc_install, capsys):
    install, listfile_path = casc_install
    assert main(["casc", "list", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "creature/**"]) == 0
    out = capsys.readouterr().out
    assert "creature\\testbeast\\testbeast.m2" in out
    assert "world\\wmo" not in out


def test_cli_casc_list_respects_the_limit(casc_install, capsys):
    install, listfile_path = casc_install
    assert main(["casc", "list", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "**",
                 "--limit", "2"]) == 0
    assert "stopping at --limit 2" in capsys.readouterr().out


def test_cli_casc_list_needs_a_listfile(casc_install, tmp_path, monkeypatch):
    install, _listfile_path = casc_install
    monkeypatch.chdir(tmp_path / "empty" if (tmp_path / "empty").is_dir()
                      else tmp_path)
    (tmp_path / "listfile.csv").unlink(missing_ok=True)
    assert main(["casc", "list", "--casc", str(install), "--include", "**"]) == 1


def test_cli_casc_extract_is_raw(casc_install, tmp_path, capsys):
    install, listfile_path = casc_install
    out = tmp_path / "raw"
    assert main(["casc", "extract", "--casc", str(install),
                 "-l", str(listfile_path), "--include", "creature/**/*.m2",
                 "-o", str(out)]) == 0
    extracted = out / "creature/testbeast/testbeast.m2"
    assert extracted.is_file()
    # extract does not convert: the file is still the chunked original
    assert extracted.read_bytes()[:4] == b"MD21"


def test_a_wdl_is_recognised_and_converted(tmp_path):
    """It used to be listed as unsupported; now it goes through the pipeline."""
    src = tmp_path / "in"
    (src / "world/maps/azeroth").mkdir(parents=True)
    wdl = src / "world/maps/azeroth/azeroth.wdl"
    wdl.write_bytes(F.build_wdl())

    assert detect.detect(wdl.read_bytes(), str(wdl)) == detect.WDL
    assert detect.classify(detect.WDL, str(wdl))[0] == "convert"

    out = tmp_path / "out"
    jobs, skipped = plan([src])
    report = run(jobs, Options(), Listfile(), out, roots=[str(src)],
                 skipped=skipped)
    assert [r.status.value for r in report.files] == ["lossy"]
    written = out / "world/maps/azeroth/azeroth.wdl"
    assert written.exists()
    assert "MLHD" not in [c.name for c in ChunkReader(written.read_bytes(),
                                                      reverse=True)]


def _terrain_tree(tmp_path):
    """A map folder with every piece a modern build writes for one tile."""
    src = tmp_path / "in"
    maps = src / "world/maps/az"
    maps.mkdir(parents=True)
    root, tex, obj = F.build_split_adt(chunks=4)
    for name, data in (("Az_1_1.adt", root), ("Az_1_1_tex0.adt", tex),
                       ("Az_1_1_obj0.adt", obj), ("Az_1_1_tex1.adt", tex),
                       ("Az_1_1_obj1.adt", obj), ("Az_1_1_lod.adt", root)):
        (maps / name).write_bytes(data)
    return src, maps


def test_terrain_pieces_the_tile_cannot_hold_are_reported_not_dropped(tmp_path):
    """They used to be grouped, unused, and never mentioned again."""
    src, maps = _terrain_tree(tmp_path)
    jobs, skipped = plan([src])

    assert len(jobs) + len(skipped) == len(list(maps.iterdir())) == 6

    unused = [s for s in skipped if s.status is Status.SKIPPED]
    reasons = {Path(s.source).name: s.notes[0].message for s in unused}
    assert set(reasons) == {"Az_1_1_tex1.adt", "Az_1_1_obj1.adt",
                            "Az_1_1_lod.adt"}
    assert "high-detail texture" in reasons["Az_1_1_tex1.adt"]
    assert "LOD terrain mesh" in reasons["Az_1_1_lod.adt"]
    assert all(s.notes[0].code == "adt.piece_unused" for s in unused)

    # and the two that were used say so rather than going unmentioned
    merged = [Path(s.source).name for s in skipped
              if s.status is Status.MERGED]
    assert sorted(merged) == ["Az_1_1_obj0.adt", "Az_1_1_tex0.adt"]


def test_the_tile_itself_is_still_merged_from_its_pieces(tmp_path):
    src, _maps = _terrain_tree(tmp_path)
    jobs, _skipped = plan([src])
    assert len(jobs) == 1
    assert sorted(jobs[0].extra) == ["obj0", "tex0"]


# ---------------------------------------------------------------------------
# Nothing leaves a run unaccounted for
# ---------------------------------------------------------------------------
def _every_format_tree(tmp_path):
    """One file of every kind this tool has an opinion about."""
    import test_detect as D

    src = tmp_path / "in"
    (src / "world/maps/az").mkdir(parents=True)
    (src / "creature/bear").mkdir(parents=True)

    files = {}
    # converted
    model = F.build_modern_model()
    files["creature/bear/bear.m2"] = F.serialise_modern_m2(model, skeleton_id=0)
    files["creature/bear/bear00.skin"] = F.build_skin(legion=True)
    files["creature/bear/bear.blp"] = F.build_blp(
        F.build_gradient_image(8, 8))
    files["world/wmo/house.wmo"] = F.build_modern_wmo_root(groups=1)
    files["world/maps/az/az.wdl"] = F.build_wdl()
    files["world/maps/az/az.wlw"] = F.build_liquid()
    root, tex, obj = F.build_split_adt(chunks=4)
    files["world/maps/az/az_1_1.adt"] = root
    files["world/maps/az/az_1_1_tex0.adt"] = tex
    files["world/maps/az/az_1_1_obj0.adt"] = obj
    files["world/maps/az/az_1_1_lod.adt"] = D.chunked(
        [("MVER", b"\0" * 4), ("MLHD", b"\0" * 16)])
    files["world/maps/az/az_lgt.wdt"] = D.chunked(
        [("MVER", b"\0" * 4), ("MPLT", b"\0" * 32)])
    # copied and skipped, by signature
    for kind, data in D.SIGNATURES.items():
        files[f"misc/thing{D.detect.EXTENSIONS[kind]}"] = data
    # and one nobody anticipated
    files["misc/mystery.qqq"] = b"\x00\x11\x22\x33" * 8

    for rel, data in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return src, len(files)


def test_every_file_of_every_kind_is_accounted_for(tmp_path):
    """The whole point: a format nobody thought about cannot vanish."""
    src, count = _every_format_tree(tmp_path)
    jobs, skipped = plan([src])
    out = tmp_path / "out"
    report = run(jobs, Options(), Listfile(), out, roots=[str(src)],
                 skipped=skipped)
    books = report.accounting()
    assert books["inputs"] == count
    assert (books["written"] + books["merged"] + books["skipped"]
            + books["failed"]) == count


def test_the_accounting_survives_the_run_not_just_the_plan(tmp_path):
    """Results come back from workers; the totals must still add up."""
    src, count = _every_format_tree(tmp_path)
    jobs, skipped = plan([src])
    report = run(jobs, Options(), Listfile(), tmp_path / "out",
                 roots=[str(src)], skipped=skipped)
    assert len(report.files) == count
    assert len({id(f) for f in report.files}) == count   # no duplicates


def test_every_skipped_file_says_why(tmp_path):
    src, _count = _every_format_tree(tmp_path)
    _jobs, skipped = plan([src])
    for result in skipped:
        assert result.notes, f"{result.source} was skipped silently"
        assert result.notes[0].message, f"{result.source} has an empty reason"


def test_the_summary_states_the_accounting(tmp_path):
    src, count = _every_format_tree(tmp_path)
    jobs, skipped = plan([src])
    report = run(jobs, Options(), Listfile(), tmp_path / "out",
                 roots=[str(src)], skipped=skipped)
    line = next(ln for ln in report.summary_lines()
                if "every input accounted for" in ln)
    numbers = [int(w) for w in line.replace(",", " ").split() if w.isdigit()]
    assert sum(numbers) == count


# ---------------------------------------------------------------------------
# Every file of an install accounted for, and none written over another
# ---------------------------------------------------------------------------
@pytest.fixture
def rigged_install(tmp_path: Path):
    """A skeleton-rigged model with LOD skins, a stale skin sitting on the
    model's companion name, and a texture whose file name is all digits."""
    import casc_fixtures as CF
    model = F.build_modern_model(sequences=0, bones=0)
    model.bones = model.sequences = model.attachments = []
    model.key_bone_lookup = model.sequence_lookups = model.attachment_lookup = []
    files = {
        123456: F.serialise_modern_m2(
            model, skeleton_id=940000, anim_ids=(),
            skin_ids=(910000, 910001, 910002, 910003, 910010, 910011)),
        910000: F.build_skin(legion=True), 910001: F.build_skin(legion=True),
        910002: F.build_skin(legion=True), 910003: F.build_skin(legion=True),
        910010: F.build_skin(legion=True), 910011: F.build_skin(legion=True),
        910099: F.build_skin(legion=True, vertices=9, triangles=3),
        940000: F.build_skel(bones=4, sequences=2,
                             anim_ids=((0, 0, 920000), (1, 0, 920001))),
        920000: F.build_anim(), 920001: F.build_anim(),
        900002: F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT5),
    }
    CF.build_install(tmp_path / "wow", files)
    entries = [
        "123456;creature/testbeast/testbeast.m2",
        "910000;creature/testbeast/testbeast_hd00.skin",
        "910001;creature/testbeast/testbeast_hd01.skin",
        "910002;creature/testbeast/testbeast_hd02.skin",
        "910003;creature/testbeast/testbeast_hd03.skin",
        "910010;creature/testbeast/testbeast_hd_lod01.skin",
        "910011;creature/testbeast/testbeast_hd_lod02.skin",
        "910099;creature/testbeast/testbeast00.skin",      # stale, unreferenced
        "940000;creature/testbeast/testbeast.skel",
        "920000;creature/testbeast/testbeast_skel0000-00.anim",
        "920001;creature/testbeast/testbeast_skel0001-00.anim",
        "900002;interface/worldmap/stvdiamondminebg/2.blp",
        "2;interface/cinematics/wow_intro_800.avi",         # FileDataID 2
    ]
    (tmp_path / "listfile.csv").write_text("\n".join(entries) + "\n")
    return tmp_path / "wow", Listfile.load(tmp_path / "listfile.csv"), files


def test_a_skeletons_animations_are_left_to_the_model_that_merges_it(rigged_install):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile, files = rigged_install
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
    assert sorted(j.file_id for j in jobs) == [123456, 900002]
    codes = {s.file_id: s.notes[0].code for s in skipped}
    assert codes[910010] == codes[910011] == "plan.skin_lod"
    assert codes[940000] == "plan.merged"
    assert codes[910099] == "plan.superseded"
    # Every file the build holds is mentioned exactly once by the plan.
    planned = {j.file_id for j in jobs} | set(codes)
    claimed = {910000, 910001, 910002, 910003, 920000, 920001}
    assert planned | claimed == set(files)


def test_the_written_patch_has_whole_animations_and_the_models_own_skins(
        rigged_install, tmp_path):
    from wotlkconv.casc import CascStorage
    from wotlkconv.pipeline import plan_casc
    install, listfile, files = rigged_install
    out = tmp_path / "out"
    with CascStorage.open(install) as storage:
        jobs, skipped = plan_casc(storage, listfile, include=["**"])
        report = run(jobs, Options(), listfile, out, storage=storage,
                     skipped=skipped)
    beast = out / "creature" / "testbeast"
    # The model writes its skeleton's animations; no model-less stub of them.
    anims = [f for f in report.files if f.kind == "anim"]
    assert sorted((Path(f.target).name, f.file_id) for f in anims) == [
        ("testbeast0000-00.anim", 920000), ("testbeast0001-00.anim", 920001)]
    assert not list(beast.glob("testbeast_skel*.anim"))
    # testbeast00.skin is the model's own profile, not the stale file.
    from wotlkconv.m2.skin import parse_skin
    assert len(parse_skin((beast / "testbeast00.skin").read_bytes(), "s").vertices) == \
        len(parse_skin(files[910000], "s").vertices)
    # "2.blp" is a texture called 2, not FileDataID 2.
    assert (out / "interface" / "worldmap" / "stvdiamondminebg" / "2.blp").is_file()
    assert not (out / "interface" / "cinematics").exists()
    books = report.accounting()
    assert books["inputs"] == len(files)
    assert books["written"] + books["merged"] + books["skipped"] + books["failed"] \
        == len(files)


def test_an_input_several_results_name_is_counted_once():
    from wotlkconv.report import FileResult, Report
    report = Report()
    report.add(FileResult(source="a0000-00.anim", kind="anim", file_id=920000))
    report.add(FileResult(source="b0000-00.anim", kind="anim", file_id=920000))
    report.add(FileResult(source="c.blp", kind="blp", file_id=900000))
    books = report.accounting()
    assert books["inputs"] == 2 and books["results"] == 3
    assert books["extra_outputs"] == 1


def test_a_file_is_also_written_under_the_names_itemdisplayinfo_gives_it(tmp_path):
    # Wrath asks for helm_x_HuM.m2 (and its skins beside it); the build keeps
    # helm_x_hu_m.m2.  Armour textures and icons are copied the same way.
    from wotlkconv.pipeline import Converter, Job
    from wotlkconv.resolve import AssetSource

    src = tmp_path / "in"
    src.mkdir()
    for fid in (910000, 910001, 910002, 910003):
        (src / f"{fid}.skin").write_bytes(F.build_skin(legion=True))
    (src / "123456.m2").write_bytes(
        F.serialise_modern_m2(F.build_modern_model(), anim_ids=()))
    (src / "700001.blp").write_bytes(
        F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT5))
    listfile = Listfile("<test>")
    listfile.update(["123456;item/objectcomponents/head/helm_x_hu_m.m2",
                     "700001;item/texturecomponents/armuppertexture/plate_au_u_4876585.blp"])
    out = tmp_path / "out"
    converter = Converter(
        Options(), listfile, out, AssetSource(listfile, roots=[src]),
        aliases={123456: {"item/objectcomponents/head/helm_x_hum.m2"},
                 700001: {"item/texturecomponents/armuppertexture/plate_au_4876585_u.blp"}})
    for name, kind in (("123456.m2", detect.M2), ("700001.blp", detect.BLP)):
        outputs = converter.convert(Job(kind=kind, relpath=name, source=src / name))
        converter.write(outputs)
        assert any(n.code == "io.aliases" for n in outputs[0].result.notes)

    head = out / "item" / "objectcomponents" / "head"
    assert (head / "helm_x_hum.m2").read_bytes() == (head / "helm_x_hu_m.m2").read_bytes()
    assert (head / "helm_x_hum00.skin").read_bytes() == (head / "helm_x_hu_m00.skin").read_bytes()
    assert (head / "helm_x_hum03.skin").is_file()
    arm = out / "item" / "texturecomponents" / "armuppertexture"
    assert (arm / "plate_au_4876585_u.blp").read_bytes() == \
        (arm / "plate_au_u_4876585.blp").read_bytes()


def test_minimap_tiles_go_where_the_client_finds_them(tmp_path):
    from wotlkconv.minimap import relocated_path
    src = tmp_path / "src"
    tile = src / "world" / "minimaps" / "azeroth" / "map32_48.blp"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(F.build_blp(F.build_gradient_image(16, 16), PreferredFormat.DXT1))
    out = tmp_path / "out"
    assert main(["convert", str(src), "-o", str(out)]) == 0
    hashed = out / relocated_path("world/minimaps/azeroth/map32_48.blp").replace("\\", "/")
    assert hashed.is_file()
    assert not (out / "world" / "minimaps").exists()
    index = (out / "textures" / "minimap" / "md5translate.trs").read_bytes()
    assert index == (b"dir: azeroth\r\nazeroth\\map32_48.blp\t"
                     + hashed.name.encode() + b"\r\n")


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
def test_progress_is_logged_each_time_the_percentage_moves_on(capsys):
    from wotlkconv import log
    from wotlkconv.pipeline import Progress
    now = [0.0]
    log.set_level("info")
    progress = Progress(200, clock=lambda: now[0])
    now[0] = 30.0
    progress.advance()            # 0.5%: nothing to say yet
    progress.advance()            # 1%
    progress.advance()            # still 1%
    now[0] = 90.0
    progress.advance(7)           # 5%: one line, not four
    progress.advance(190)         # 100%
    lines = [x for x in capsys.readouterr().err.splitlines() if "progress" in x]
    assert lines == [
        "info: progress: 1% (2 of 200 files, 30s elapsed, about 49m 30s left)",
        "info: progress: 5% (10 of 200 files, 1m 30s elapsed, about 28m 30s left)",
        "info: progress: 100% (200 of 200 files, 1m 30s elapsed)",
    ]


def test_a_run_reports_its_progress(extraction, tmp_path, capsys):
    from wotlkconv import log
    log.set_level("info")
    main(["convert", str(extraction), "-o", str(tmp_path / "out"),
          "--listfile", str(extraction.parent / "listfile.csv")])
    lines = [x for x in capsys.readouterr().err.splitlines() if "progress:" in x]
    assert lines and "100%" in lines[-1]
