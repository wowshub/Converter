"""CASC reading: BLTE, indices, encoding, root, and the whole chain."""

import struct

import casc_fixtures as CF
import pytest

from wotlkconv.casc import CascStorage, KeyRing, blte
from wotlkconv.casc.blte import EncryptedChunkError
from wotlkconv.casc.config import parse_build_info, parse_config
from wotlkconv.casc.encoding import EncodingTable
from wotlkconv.casc.index import bucket_for, parse_index
from wotlkconv.casc.root import (
    CONTENT_LOAD_ON_MACOS,
    CONTENT_LOAD_ON_WINDOWS,
    CONTENT_LOW_VIOLENCE,
    CONTENT_NO_NAME_HASH,
    LOCALE_ALL,
    RootTable,
)
from wotlkconv.casc.salsa20 import arc4, salsa20
from wotlkconv.casc.storage import FileNotInstalledError
from wotlkconv.errors import MalformedFileError, MissingDependencyError


# ---------------------------------------------------------------------------
# Ciphers (published test vectors)
# ---------------------------------------------------------------------------
def test_salsa20_matches_the_reference_vector():
    # ECRYPT set 1, 256-bit all-zero key and nonce.
    assert salsa20(bytes(32), bytes(8), bytes(64))[:8].hex() == "9a97f65b9b4c721b"


def test_salsa20_accepts_a_128_bit_key():
    assert len(salsa20(bytes(16), bytes(8), bytes(32))) == 32


def test_salsa20_is_its_own_inverse():
    key, nonce = bytes(range(32)), bytes(range(8))
    plain = b"the quick brown fox" * 7
    assert salsa20(key, nonce, salsa20(key, nonce, plain)) == plain


def test_arc4_matches_the_classic_vector():
    assert arc4(b"Key", b"", b"Plaintext").hex() == "bbf316e8d940af0ad3"


# ---------------------------------------------------------------------------
# BLTE
# ---------------------------------------------------------------------------
def test_single_chunk_stream_round_trips():
    payload = b"hello casc " * 300
    assert blte.decode(CF.make_blte(payload)) == payload


def test_multi_chunk_stream_round_trips():
    payload = bytes(range(256)) * 40
    assert blte.decode(CF.make_blte(payload, chunk_size=97)) == payload


def test_headerless_stream_round_trips():
    payload = b"small file"
    assert blte.decode(CF.make_blte_single(payload)) == payload


def test_stored_chunks_round_trip():
    payload = b"incompressible" * 10
    assert blte.decode(CF.make_blte(payload, mode=b"N")) == payload


def test_nested_frames_are_decoded():
    payload = b"nested" * 50
    inner = CF.make_blte(payload)
    outer = b"BLTE" + struct.pack(">I", 0) + b"F" + inner
    assert blte.decode(outer) == payload


def test_encrypted_chunk_without_a_key_says_which_key():
    key_name = 0x0123456789ABCDEF
    body = b"F" + CF.make_blte(b"secret")
    encrypted = salsa20(bytes(16), b"\0" * 4, body)
    chunk = (b"E" + bytes([8]) + key_name.to_bytes(8, "little")
             + bytes([4]) + b"\0" * 4 + b"S" + encrypted)
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    with pytest.raises(EncryptedChunkError) as exc:
        blte.decode(stream, KeyRing())
    assert f"{key_name:016X}" in str(exc.value)


def test_encrypted_chunk_decodes_with_the_key():
    key_name = 0x0123456789ABCDEF
    key = bytes(range(16))
    payload = b"unreleased content"
    body = b"N" + payload
    iv = b"\x01\x02\x03\x04"
    mixed = bytearray(iv)
    for i in range(4):
        mixed[i] ^= (0 >> (i * 8)) & 0xFF   # chunk index 0
    chunk = (b"E" + bytes([8]) + key_name.to_bytes(8, "little")
             + bytes([4]) + iv + b"S"
             + salsa20(key, bytes(mixed), body))
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    ring = KeyRing()
    ring.add(key_name, key)
    assert blte.decode(stream, ring) == payload


def test_a_non_blte_buffer_is_rejected():
    with pytest.raises(MalformedFileError):
        blte.decode(b"NOPE" + b"\0" * 20)


def test_corrupt_zlib_is_reported_clearly():
    chunk = b"Z" + b"\xff" * 20
    stream = b"BLTE" + struct.pack(">I", 0) + chunk
    with pytest.raises(MalformedFileError, match="inflate"):
        blte.decode(stream)


def test_lz4_literal_only_block_decodes():
    # token 0x50 = 5 literals, no match
    payload = b"abcde"
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + bytes([0x50]) + payload
    assert blte.decode(stream) == payload


def test_lz4_match_copying_decodes():
    """The match path is what makes LZ4 a compressor; literals alone never
    exercise it."""
    # 4 literals "abcd", then a match of 4 bytes at offset 4 -> "abcdabcd"
    block = bytes([0x40]) + b"abcd" + bytes([0x04, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    assert blte.decode(stream) == b"abcdabcd"


def test_lz4_overlapping_match_repeats_a_run():
    # 1 literal "a", then a 7-byte match at offset 1: the classic run-length case
    block = bytes([0x13]) + b"a" + bytes([0x01, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    assert blte.decode(stream) == b"a" * 8


def test_lz4_extended_literal_length_decodes():
    """A literal run of 15 or more is extended by the bytes after the token."""
    literals = bytes(range(32))
    block = bytes([0xF0, 17]) + literals + bytes([0x20, 0x00])
    out = blte.decode(b"BLTE" + struct.pack(">I", 0) + b"4" + block)
    assert out.startswith(literals)
    assert out == (literals * 2)[:len(out)]


def test_a_zero_offset_lz4_match_is_rejected():
    block = bytes([0x40]) + b"abcd" + bytes([0x00, 0x00])
    stream = b"BLTE" + struct.pack(">I", 0) + b"4" + block
    with pytest.raises(MalformedFileError, match="zero match offset"):
        blte.decode(stream)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_build_info_columns_lose_their_type_suffix():
    rows = parse_build_info(
        "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16\nus|1|deadbeef\n")
    assert rows == [{"Branch": "us", "Active": "1", "Build Key": "deadbeef"}]


def test_config_values_split_on_whitespace():
    cfg = parse_config("encoding = aaaa bbbb\n# comment\nroot = cccc\n")
    assert cfg["encoding"] == ["aaaa", "bbbb"]
    assert cfg["root"] == ["cccc"]


def test_opening_a_directory_that_is_not_an_install_explains_itself(tmp_path):
    with pytest.raises(MissingDependencyError, match=r"\.build\.info"):
        CascStorage.open(tmp_path)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def test_index_round_trips_locations():
    ekey = bytes(range(16))
    raw = CF.make_index(bucket_for(ekey), {ekey: (3, 0x1234, 500)})
    entries = parse_index(raw, "test.idx")
    entry = entries[ekey[:9]]
    assert (entry.archive, entry.offset, entry.size) == (3, 0x1234, 500)


def test_index_bucket_matches_the_fixture():
    for i in range(32):
        ekey = bytes([i]) + bytes(range(15))
        assert bucket_for(ekey) == CF.bucket_for(ekey)


def test_unknown_index_version_is_rejected():
    raw = bytearray(CF.make_index(0, {}))
    struct.pack_into("<H", raw, 8, 9)
    with pytest.raises(MalformedFileError, match="version 9"):
        parse_index(bytes(raw), "bad.idx")


# ---------------------------------------------------------------------------
# Encoding and root
# ---------------------------------------------------------------------------
def test_encoding_table_maps_content_keys_to_encoding_keys():
    entries = {bytes([i]) * 16: (bytes([i + 100]) * 16, i * 1000)
               for i in range(1, 60)}
    table = EncodingTable.parse(CF.make_encoding(entries))
    assert len(table) == len(entries)
    for ckey, (ekey, size) in entries.items():
        assert table.ekey_for(ckey) == ekey
        assert table.size_for(ckey) == size


def test_encoding_table_spans_multiple_pages():
    # 4 KiB pages hold ~107 entries, so 400 forces several.
    entries = {i.to_bytes(16, "big"): (bytes([i % 251]) * 16, i)
               for i in range(400)}
    table = EncodingTable.parse(CF.make_encoding(entries, page_kib=4))
    assert len(table) == 400


def test_root_table_decodes_file_id_deltas():
    entries = {1: b"a" * 16, 5: b"b" * 16, 1000000: b"c" * 16}
    table = RootTable.parse(CF.make_root(entries))
    assert sorted(table.file_ids()) == [1, 5, 1000000]
    for fid, ckey in entries.items():
        assert table.ckey_for(fid) == ckey


def test_root_table_reads_the_all_locales_flag():
    # 0xFFFFFFFF must not be read as a negative signed value.
    entries = {7: b"z" * 16}
    table = RootTable.parse(CF.make_root(entries, locale_flags=0xFFFFFFFF))
    assert table.ckey_for(7) == b"z" * 16


@pytest.mark.parametrize("kwargs", [
    {"mfst": False}, {"mfst": True},
    {"header_version": 1}, {"header_version": 2},
])
def test_root_table_reads_every_layout(kwargs):
    entries = {3: b"q" * 16, 9: b"r" * 16}
    table = RootTable.parse(CF.make_root(entries, **kwargs))
    assert sorted(table.file_ids()) == [3, 9]
    assert table.ckey_for(9) == b"r" * 16


@pytest.mark.parametrize("header_version", [0, 1, 2])
def test_root_table_skips_name_hashes_when_present(header_version):
    # A second block after the hashes proves they were stepped over exactly.
    raw = CF.make_root_blocks([
        ({2: b"n" * 16, 4: b"m" * 16}, 0, LOCALE_ALL),
        ({6: b"o" * 16}, CONTENT_NO_NAME_HASH, LOCALE_ALL),
    ], header_version=header_version)
    table = RootTable.parse(raw)
    assert sorted(table.file_ids()) == [2, 4, 6]
    assert table.ckey_for(6) == b"o" * 16


def test_a_version_2_root_is_not_read_with_the_old_block_header():
    # 11.1 moved localeFlags ahead of the content flags and grew the header to
    # 17 bytes; reading it as 12 finds a garbage record count in the first
    # block and loses nearly the whole build.
    blocks = [({i * 3 + 1: bytes([i]) * 16 for i in range(50)},
               CONTENT_NO_NAME_HASH, 0x1F3F6),
              ({1000 + i: bytes([i + 60]) * 16 for i in range(50)},
               CONTENT_NO_NAME_HASH, 0x2)]
    table = RootTable.parse(CF.make_root_blocks(blocks, header_version=2))
    assert len(table) == 100 and not table.truncated
    assert table.version == 2 and table.blocks == 2


@pytest.mark.parametrize("header_version", [0, 2])
def test_root_table_prefers_the_normal_variant_over_low_violence(header_version):
    # Retail lists the low-violence copy of ~10k files *before* the normal
    # one, so taking the first entry ships the censored asset.
    raw = CF.make_root_blocks([
        ({5: b"L" * 16}, CONTENT_NO_NAME_HASH | CONTENT_LOW_VIOLENCE, 0x1F3F6),
        ({5: b"N" * 16}, CONTENT_NO_NAME_HASH, 0x1F3F6),
    ], header_version=header_version)
    assert RootTable.parse(raw).ckey_for(5) == b"N" * 16


def test_root_table_prefers_windows_over_macos():
    raw = CF.make_root_blocks([
        ({5: b"M" * 16}, CONTENT_NO_NAME_HASH | CONTENT_LOAD_ON_MACOS, 0x2),
        ({5: b"W" * 16}, CONTENT_NO_NAME_HASH | CONTENT_LOAD_ON_WINDOWS, 0x2),
    ], header_version=2)
    assert RootTable.parse(raw).ckey_for(5) == b"W" * 16


def test_root_table_prefers_the_locale_but_keeps_files_only_others_have():
    raw = CF.make_root_blocks([
        ({5: b"D" * 16, 6: b"d" * 16}, CONTENT_NO_NAME_HASH, 0x8),   # deDE
        ({5: b"E" * 16}, CONTENT_NO_NAME_HASH, 0x2),                 # enUS
    ], header_version=2)
    table = RootTable.parse(raw, locale=0x2)
    assert table.ckey_for(5) == b"E" * 16
    assert table.ckey_for(6) == b"d" * 16
    assert RootTable.parse(raw, locale=0x8).ckey_for(5) == b"D" * 16


def test_an_unknown_root_header_version_is_refused():
    raw = bytearray(CF.make_root({1: b"x" * 16}, header_version=2))
    struct.pack_into("<I", raw, 8, 3)
    with pytest.raises(MalformedFileError, match="version 3"):
        RootTable.parse(bytes(raw))


def test_an_empty_root_is_an_error():
    with pytest.raises(MalformedFileError):
        RootTable.parse(b"TSFM" + struct.pack("<II", 0, 0))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
@pytest.fixture
def install(tmp_path):
    files = {
        123456: b"MD21" + b"\x00" * 200,
        900000: b"BLP2" + b"\x01" * 500,
        700001: b"a" * 20000,
        1: b"tiny",
    }
    CF.build_install(tmp_path / "wow", files)
    return tmp_path / "wow", files


def test_every_file_reads_back_byte_for_byte(install):
    root, files = install
    with CascStorage.open(root) as storage:
        for file_id, expected in files.items():
            assert storage.read_file_id(file_id) == expected


def test_stats_describe_the_build(install):
    root, files = install
    with CascStorage.open(root) as storage:
        stats = storage.stats()
        assert stats.product == "wow"
        assert stats.version == "11.0.5.57212"
        assert stats.files == len(files)
        assert stats.index_buckets >= 1


def test_a_file_outside_the_build_is_reported(install):
    root, _files = install
    with CascStorage.open(root) as storage:
        data, why = storage.try_read_file_id(999999)
        assert data is None and "root table" in why


def test_an_install_with_no_archive_fails_to_open(tmp_path):
    CF.build_install(tmp_path / "wow", {5: b"payload" * 100})
    (tmp_path / "wow" / "Data" / "data" / "data.000").unlink()
    with pytest.raises(FileNotInstalledError, match=r"data\.000"):
        CascStorage.open(tmp_path / "wow")


def test_a_file_streamed_from_the_cdn_is_reported_not_guessed(install):
    """Partial installs leave files out of the local index entirely."""
    root, _files = install
    with CascStorage.open(root) as storage, pytest.raises(FileNotInstalledError, match="CDN"):
        storage._read_by_ekey(b"\xee" * 16, "absent file")


def test_an_install_with_no_indices_explains_itself(tmp_path):
    CF.build_install(tmp_path / "wow", {5: b"x" * 50})
    for idx in (tmp_path / "wow" / "Data" / "data").glob("*.idx"):
        idx.unlink()
    with pytest.raises(MissingDependencyError, match="idx"):
        CascStorage.open(tmp_path / "wow")


def test_multi_chunk_storage_reads(tmp_path):
    files = {9: bytes(range(256)) * 200}
    CF.build_install(tmp_path / "wow", files, chunk_size=1024)
    with CascStorage.open(tmp_path / "wow") as storage:
        assert storage.read_file_id(9) == files[9]


def test_asking_for_the_wrong_product_lists_what_is_there(install):
    root, _files = install
    with pytest.raises(MissingDependencyError, match="wow"):
        CascStorage.open(root, product="wow_classic_era")


def test_an_unknown_locale_is_rejected(install):
    root, _files = install
    with pytest.raises(MissingDependencyError, match="unknown locale"):
        CascStorage.open(root, locale="elvish")


# ---------------------------------------------------------------------------
# Key ring
# ---------------------------------------------------------------------------
def test_key_ring_parses_the_community_format(tmp_path):
    path = tmp_path / "WoW.txt"
    path.write_text("FA505078126ACB3E 393ED9CD8AF8E5E0E31D9B6D0B0F3B0B\n"
                    "# a comment\n"
                    "BAD LINE\n")
    ring = KeyRing.load(path)
    assert len(ring) == 1
    assert ring.get(0xFA505078126ACB3E) == bytes.fromhex(
        "393ED9CD8AF8E5E0E31D9B6D0B0F3B0B")


def test_an_empty_key_ring_is_falsy():
    assert not KeyRing()


# ---------------------------------------------------------------------------
# How much of the build is actually here
# ---------------------------------------------------------------------------
@pytest.fixture
def partial_install(tmp_path):
    """An install like a real one: listed in full, downloaded in part."""
    return CF.build_install(
        tmp_path / "game",
        {100: b"stored one", 101: b"stored two"},
        not_downloaded={200: b"on the cdn", 201: b"also on the cdn",
                        202: b"and this"},
        no_encoding={300: b"no key at all"})


def test_coverage_separates_stored_from_streamed(partial_install):
    with CascStorage.open(partial_install) as storage:
        coverage = storage.coverage()
    assert coverage.listed == 6
    assert coverage.local == 2
    assert coverage.not_downloaded == 3
    assert coverage.no_encoding == 1
    assert coverage.fraction == pytest.approx(2 / 6)


def test_coverage_describes_itself_in_terms_of_the_cdn(partial_install):
    with CascStorage.open(partial_install) as storage:
        text = storage.coverage().describe()
    assert "2 of 6 files" in text and "33.3%" in text
    assert "3 would have to come from the CDN" in text


def test_a_file_that_is_only_listed_says_so_rather_than_being_fetched(
        partial_install):
    with CascStorage.open(partial_install) as storage:
        data, why = storage.try_read_file_id(200)
    assert data is None
    assert "streams it from the CDN rather than storing it" in why


def test_coverage_can_be_sampled_on_a_large_build(tmp_path):
    install = CF.build_install(tmp_path / "game",
                               dict.fromkeys(range(100), b"x" * 8))
    with CascStorage.open(install) as storage:
        full = storage.coverage()
        sampled = storage.coverage(sample=10)
    assert full.sampled == 0 and full.listed == 100 and full.local == 100
    assert sampled.listed == 100 and sampled.measured == 10
    assert sampled.local == 10 and sampled.fraction == 1.0


def test_a_sample_larger_than_the_build_measures_all_of_it(partial_install):
    with CascStorage.open(partial_install) as storage:
        assert storage.coverage(sample=1000).sampled == 0


def test_casc_info_states_the_cdn_boundary(partial_install, capsys):
    from wotlkconv.cli import main

    assert main(["casc", "info", "--casc", str(partial_install)]) == 0
    out = capsys.readouterr().out
    assert "local      2 of 6 files" in out
    assert "never fetches from the CDN" in out


def test_casc_info_on_a_complete_install_does_not_warn(tmp_path, capsys):
    install = CF.build_install(tmp_path / "game", {1: b"all here"})
    from wotlkconv.cli import main

    assert main(["casc", "info", "--casc", str(install)]) == 0
    out = capsys.readouterr().out
    assert "1 of 1 files (100.0%)" in out
    assert "never fetches from the CDN" not in out


# ---------------------------------------------------------------------------
# Files the install does not store, from the CDN
# ---------------------------------------------------------------------------
@pytest.fixture
def cdn_install(tmp_path):
    """Two files stored locally; the rest only on a CDN, archived or loose."""
    cdn_files = {400 + i: bytes([i]) * (300 + i * 37) for i in range(12)}
    cdn_files[500] = b"a loose file, kept outside any archive"
    root = CF.build_install(tmp_path / "game", {100: b"stored one", 101: b"stored two"},
                            cdn=cdn_files, cdn_loose={500}, chunk_size=64)
    return root, cdn_files


def test_a_cdn_index_is_searched_across_its_blocks(tmp_path):
    from wotlkconv.casc.cdn import CdnIndex

    entries = [(CF.md5(i.to_bytes(4, "big")), 100 + i, i % 7, 1000 * i)
               for i in range(600)]                  # ~4 blocks of 157
    path = tmp_path / "group.index"
    path.write_bytes(CF.make_cdn_index(entries, 6))
    index = CdnIndex(path)
    try:
        assert index.blocks == 4 and index.count == 600
        for ekey, size, archive, offset in entries:
            assert index.find(ekey) == (size, archive, offset)
        assert index.find(CF.md5(b"not there")) is None
        assert index.find(b"\xff" * 16) is None      # past the last block
    finally:
        index.close()


def test_files_the_install_does_not_store_are_fetched_and_verified(cdn_install, tmp_path):
    root, cdn_files = cdn_install
    requests: list = []
    with CascStorage.open(root, cdn_cache=tmp_path / "cache",
                          cdn_fetch=CF.serve_cdn(root, log=requests)) as storage:
        assert storage.read_file_id(100) == b"stored one"     # still local
        assert not requests
        for file_id, payload in cdn_files.items():
            assert storage.read_file_id(file_id) == payload
    ranged = [r for r in requests if r[2] is not None]
    whole = [r for r in requests if r[2] is None]
    assert len(ranged) == 12 and len(whole) == 1             # 12 archived, 1 loose
    assert all(host == CF.CDN_HOSTS[0] for host, *_ in requests)


def test_a_fetched_file_is_cached_and_never_fetched_twice(cdn_install, tmp_path):
    root, cdn_files = cdn_install
    first: list = []
    with CascStorage.open(root, cdn_cache=tmp_path / "cache",
                          cdn_fetch=CF.serve_cdn(root, log=first)) as storage:
        storage.read_file_id(400)
    again: list = []
    with CascStorage.open(root, cdn_cache=tmp_path / "cache",
                          cdn_fetch=CF.serve_cdn(root, log=again)) as storage:
        assert storage.read_file_id(400) == cdn_files[400]
    assert len(first) == 1 and again == []


def test_a_download_that_does_not_match_its_key_is_refused_and_the_next_host_asked(
        cdn_install, tmp_path):
    root, cdn_files = cdn_install
    fetch = CF.serve_cdn(root, corrupt_hosts={CF.CDN_HOSTS[0]})
    with CascStorage.open(root, cdn_cache=tmp_path / "cache", cdn_fetch=fetch) as storage:
        assert storage.read_file_id(401) == cdn_files[401]


def test_when_every_host_fails_the_file_is_reported_and_nothing_is_cached(
        cdn_install, tmp_path):
    root, _ = cdn_install
    fetch = CF.serve_cdn(root, corrupt_hosts={CF.CDN_HOSTS[0]},
                         dead_hosts={CF.CDN_HOSTS[1]})
    with CascStorage.open(root, cdn_cache=tmp_path / "cache", cdn_fetch=fetch) as storage:
        data, why = storage.try_read_file_id(402)
    assert data is None
    assert "does not hash to its key" in why and "unreachable" in why
    assert not list((tmp_path / "cache").rglob("*")) or \
        not any(p.is_file() for p in (tmp_path / "cache").rglob("*"))


def test_a_corrupted_cache_entry_is_fetched_again(cdn_install, tmp_path):
    root, cdn_files = cdn_install
    with CascStorage.open(root, cdn_cache=tmp_path / "cache",
                          cdn_fetch=CF.serve_cdn(root)) as storage:
        storage.read_file_id(403)
    cached = next(p for p in (tmp_path / "cache").rglob("*") if p.is_file())
    cached.write_bytes(b"BLTE" + bytes(40))
    requests: list = []
    with CascStorage.open(root, cdn_cache=tmp_path / "cache",
                          cdn_fetch=CF.serve_cdn(root, log=requests)) as storage:
        assert storage.read_file_id(403) == cdn_files[403]
    assert len(requests) == 1


def test_without_the_cdn_those_files_are_still_reported_not_fetched(cdn_install):
    root, _ = cdn_install
    with CascStorage.open(root) as storage:
        data, why = storage.try_read_file_id(400)
    assert data is None and "--casc-cdn" in why


def test_a_download_whose_body_was_altered_fails_its_content_key(tmp_path):
    # The encoding key covers only the BLTE header, so a damaged chunk body
    # still matches it; the decoded content has to match the content key too.
    payload = bytes(range(256)) * 4
    root = CF.build_install(tmp_path / "game", {1: b"local"}, cdn={600: payload},
                            chunk_size=256)
    inner = CF.serve_cdn(root)

    def tamper(url, offset, size):
        data = bytearray(inner(url, offset, size))
        header_size = struct.unpack_from(">I", data, 4)[0]
        # Chunks are zlib; rewrite the last chunk as stored ('N') bytes of the
        # same length so it still decodes, just to the wrong content.
        last = len(data) - 64
        if header_size and last > header_size:
            data[last] = ord("N")
        return bytes(data)

    with CascStorage.open(root, cdn_cache=tmp_path / "cache", cdn_fetch=tamper) as storage:
        data, why = storage.try_read_file_id(600)
    assert data is None
    assert "content key" in why or "inflate" in why or "expected" in why
