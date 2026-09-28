import json
from pathlib import Path

from wotlkconv.report import FileResult, Report, Status


def test_status_escalates_but_never_downgrades():
    r = FileResult(source="a.m2")
    assert r.status is Status.OK
    r.info("x.info", "fine")
    assert r.status is Status.OK
    r.lossy("x.lossy", "lost something")
    assert r.status is Status.LOSSY
    r.info("x.info2", "still fine")
    assert r.status is Status.LOSSY
    r.fail("x.fail", "broke")
    assert r.status is Status.FAILED
    r.lossy("x.lossy2", "more loss")
    assert r.status is Status.FAILED
    assert not r.ok


def test_notes_carry_machine_readable_detail():
    r = FileResult(source="a.m2")
    r.lossy("m2.particle.multitexture", "dropped textures", emitters=3)
    note = r.as_dict()["notes"][0]
    assert note["code"] == "m2.particle.multitexture"
    assert note["detail"] == {"emitters": 3}


def test_report_counts_and_json(tmp_path):
    report = Report()
    ok = FileResult(source="a.blp", kind="blp")
    lossy = FileResult(source="b.m2", kind="m2")
    lossy.lossy("m2.x", "lost")
    failed = FileResult(source="c.m2", kind="m2")
    failed.fail("m2.y", "broke")
    report.extend([ok, lossy, failed])
    report.close()

    assert report.counts() == {"ok": 1, "lossy": 1, "failed": 1}
    assert [f.source for f in report.failed] == ["c.m2"]
    assert [f.source for f in report.lossy] == ["b.m2"]

    path = tmp_path / "report.json"
    report.write_json(path)
    data = json.loads(path.read_text())
    assert data["target"] == {"patch": "3.3.5a", "build": 12340}
    assert len(data["files"]) == 3


def test_summary_lists_failures_without_verbose():
    report = Report()
    failed = FileResult(source="c.m2")
    failed.fail("m2.y", "it broke")
    report.add(failed)
    lines = report.summary_lines()
    assert any("FAILED c.m2: it broke" in line for line in lines)


def _mixed_results():
    ok = FileResult(source="a.blp", kind="blp")
    lossy = FileResult(source="b.m2", kind="m2")
    lossy.lossy("m2.x", "lost", emitters=2)
    failed = FileResult(source="c.m2", kind="m2")
    failed.info("m2.i", "context that is not the reason")
    failed.fail("m2.y", "it broke")
    skipped = FileResult(source="d.tex", kind="tex", status=Status.SKIPPED)
    return [ok, lossy, failed, skipped]


def test_a_spooled_report_says_exactly_what_a_held_one_does(tmp_path):
    # A whole build reports on ~2M files; holding them grew the process by
    # gigabytes, so a big run writes each result to disk as it arrives.
    held, spooled = Report(), Report(spool=tmp_path / "spool.jsonl")
    for report in (held, spooled):
        report.extend(_mixed_results())
        report.close()

    assert spooled.files == []                     # nothing kept per file
    assert spooled.counts() == held.counts()
    assert spooled.accounting() == held.accounting()
    assert [(f.source, [n.message for n in f.notes]) for f in spooled.failed] \
        == [("c.m2", ["it broke"])]
    assert spooled.summary_lines() == held.summary_lines()
    assert spooled.summary_lines(verbose=True) == held.summary_lines(verbose=True)

    for name, report in (("held.json", held), ("spooled.json", spooled)):
        report.write_json(tmp_path / name)
    held_json = json.loads((tmp_path / "held.json").read_text())
    spooled_json = json.loads((tmp_path / "spooled.json").read_text())
    for key in ("counts", "accounting", "files", "target"):
        assert spooled_json[key] == held_json[key]


def test_a_report_spooled_to_nowhere_still_counts(tmp_path):
    import os

    report = Report(spool=Path(os.devnull))
    report.extend(_mixed_results())
    report.close()
    assert report.counts() == {"ok": 1, "lossy": 1, "failed": 1, "skipped": 1}
    assert report.accounting()["inputs"] == 4
    assert [f.source for f in report.failed] == ["c.m2"]


def test_a_report_spools_into_an_output_folder_that_does_not_exist_yet(tmp_path):
    # A fresh --out folder is only created when the first file is written, and
    # the planner's results reach the spool before that.
    spool = tmp_path / "new-patch" / "wotlkconv-report.results.jsonl"
    report = Report(spool=spool)
    report.extend(_mixed_results())
    report.close()
    report.write_json(tmp_path / "new-patch" / "wotlkconv-report.json")
    assert spool.is_file()
    assert report.counts()["failed"] == 1
