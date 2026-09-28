import types

import pytest

from wotlkconv.db.grounddoodad import GroundDoodadNames
from wotlkconv.db.mapping import MappingLibrary
from wotlkconv.listfile import Listfile
from wotlkconv.minimap import build_translate, hashed_path, minimap_entry, relocated_path


# ---------------------------------------------------------------------------
# Minimaps
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,entry", [
    ("world/minimaps/azeroth/map32_48.blp", ("azeroth", "azeroth\\map32_48.blp")),
    ("World\\Minimaps\\WMO\\Azeroth\\Buildings\\Castle\\castle01_000_00_00.blp",
     ("wmo\\azeroth\\buildings\\castle",
      "wmo\\azeroth\\buildings\\castle\\castle01_000_00_00.blp")),
    ("world/minimaps/azeroth/noliquid_map23_40.blp", None),   # no 3.3.5a use
    ("world/minimaps/wmo/pvp/ctf_003_00_00.blp.meta", None),
    ("world/minimaps/kalimdor/tanaris/ruins/ruins_000_00_00.blp", None),
    ("world/maps/azeroth/azeroth_32_48.adt", None),
])
def test_minimap_tiles_are_recognised(path, entry):
    assert minimap_entry(path) == entry


def test_a_tile_is_written_under_a_stable_hashed_name():
    first = relocated_path("world/minimaps/azeroth/map32_48.blp")
    assert first == relocated_path("WORLD\\MINIMAPS\\Azeroth\\MAP32_48.BLP")
    assert first.startswith("textures\\minimap\\") and first.endswith(".blp")
    assert len(first) == len("textures\\minimap\\") + 32 + 4
    assert relocated_path("world/maps/azeroth/azeroth.wdt") is None


def test_the_index_matches_the_client_s_own_layout():
    text = build_translate(
        ["world\\minimaps\\wmo\\azeroth\\castle\\castle01_000_00_00.blp",
         "world\\minimaps\\azeroth\\map32_49.blp",
         "world\\minimaps\\azeroth\\map32_48.blp",
         "world\\minimaps\\azeroth\\noliquid_map32_48.blp",
         "world\\minimaps\\ahnqiraj\\map27_46.blp"],
        directories={"azeroth": "Azeroth", "ahnqiraj": "AhnQiraj"})
    lines = text.decode("latin-1").split("\r\n")
    assert lines[-1] == ""                         # every line ends in CRLF
    assert lines[:-1] == [
        "dir: AhnQiraj",
        "AhnQiraj\\map27_46.blp\t" + hashed_path("ahnqiraj\\map27_46.blp")[17:],
        "dir: Azeroth",
        "Azeroth\\map32_48.blp\t" + hashed_path("azeroth\\map32_48.blp")[17:],
        "Azeroth\\map32_49.blp\t" + hashed_path("azeroth\\map32_49.blp")[17:],
        "dir: WMO\\azeroth\\castle",
        "WMO\\azeroth\\castle\\castle01_000_00_00.blp\t"
        + hashed_path("wmo\\azeroth\\castle\\castle01_000_00_00.blp")[17:],
    ]


# ---------------------------------------------------------------------------
# Ground clutter
# ---------------------------------------------------------------------------
def _tables(rows):
    table = types.SimpleNamespace(rows={i: {"ID": i, "ModelFileID": fid}
                                        for i, fid in rows.items()})
    return types.SimpleNamespace(get=lambda name: table if name == "GroundEffectDoodad" else None)


def test_ground_clutter_is_named_the_way_the_client_loads_it():
    lf = Listfile("<test>")
    lf.update(["100;world/nodxt/detail/elwflo01.m2",
               "200;models/world/nodxt/detail/snowgrass.m2",
               "300;world/expansion08/doodads/ardenweald/elwflo01.m2"])
    names = GroundDoodadNames(_tables({1: 100, 2: 200, 3: 300, 4: 0, 5: 999}), lf)
    assert names.names == {1: "elwflo01.mdl", 2: "snowgrass.mdl",
                           3: "elwflo01_300.mdl", 4: "", 5: ""}
    assert names.aliases() == {200: {"world\\nodxt\\detail\\snowgrass.m2"},
                               300: {"world\\nodxt\\detail\\elwflo01_300.m2"}}


def test_the_ground_clutter_mapping_is_built_in():
    column = MappingLibrary().get("GroundEffectDoodad").columns[0]
    assert column.resolve == "grounddoodad.path" and column.target == ("Doodadpath",)
