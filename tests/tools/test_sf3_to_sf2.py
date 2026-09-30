"""tools/sf3_to_sf2.py: SF3 -> SF2 decoding, offsets/loops, padding, and modulator baking.

A tiny SF3 is synthesized in memory (two Vorbis-compressed sine samples, one preset, one
instrument with velocity/key/controller modulators), converted, and parsed back. The real
MuseScore bank is only checked if its converted output already exists (converting it takes
~15 s and writes ~490 MB).
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import sf3_to_sf2 as conv  # noqa: E402

RATE = 22050
VEL, KEY, CC = 2, 3, 0x80   # modulator source indices (general controllers) and the CC flag


def _ogg(signal: np.ndarray) -> bytes:
    buf = io.BytesIO()
    soundfile.write(buf, signal, RATE, format="OGG", subtype="VORBIS")
    return buf.getvalue()


def _chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def _list(kind: bytes, *chunks: bytes) -> bytes:
    body = kind + b"".join(chunks)
    return b"LIST" + struct.pack("<I", len(body)) + body


def _shdr(name: bytes, start: int, end: int, ls: int, le: int, key: int, typ: int, link: int = 0) -> bytes:
    return struct.pack(conv.SHDR_FORMAT, name.ljust(20, b"\0"), start, end, ls, le, RATE, key, 0, link, typ)


def _mod(src: int, dest: int, amount: int, amt_src: int = 0, trans: int = 0) -> bytes:
    return struct.pack("<HHhHH", src, dest, amount, amt_src, trans)


def _gen(oper: int, amount: int) -> bytes:
    return struct.pack("<Hh", oper, amount) if oper not in conv.RANGE_GENS else struct.pack("<HH", oper, amount)


def make_sf3() -> tuple[bytes, list[np.ndarray]]:
    t = np.arange(4000) / RATE
    s1 = (0.5 * np.sin(2 * np.pi * 440 * t[:2000])).astype(np.float32)
    s2 = (0.4 * np.sin(2 * np.pi * 220 * t[:3000])).astype(np.float32)
    o1, o2 = _ogg(s1), _ogg(s2)
    smpl = o1 + o2
    shdr = b"".join([
        _shdr(b"sine440", 0, len(o1), 100, 1900, 69, 1 | conv.VORBIS_FLAG),
        _shdr(b"sine220", len(o1), len(o1) + len(o2), 0, 0, 57, 1 | conv.VORBIS_FLAG),
        _shdr(b"sine440b", 0, len(o1), 10, 20, 69, 1 | conv.VORBIS_FLAG),   # shares sample 0's data
        _shdr(b"EOS", 0, 0, 0, 0, 0, 0),
    ])
    # Preset "Test" (bank 0, program 0) -> instrument 0.
    phdr = (struct.pack("<20sHHHIII", b"Test", 0, 0, 0, 0, 0, 0)
            + struct.pack("<20sHHHIII", b"EOP", 0, 0, 1, 0, 0, 0))
    pbag = struct.pack("<HH", 0, 0) + struct.pack("<HH", 1, 0)
    pmod = _mod(0, 0, 0)
    pgen = _gen(41, 0) + _gen(0, 0)
    # Instrument: global zone (key -> modEnvToFilterFc, bipolar), two local zones.
    inst = struct.pack("<20sH", b"Inst", 0) + struct.pack("<20sH", b"EOI", 3)
    g_mods = [_mod(KEY | (1 << 9), 11, 1200)]                          # key, +, bipolar, linear
    z1_mods = [_mod(VEL, 8, -2400),                                      # velocity -> filter cutoff (baked)
               _mod(VEL | (1 << 8) | (1 << 10), 48, 960),                # velocity -> attenuation (kept)
               _mod(CC | 1, 5, 50)]                                      # CC1 -> modLfoToPitch (kept)
    z2_mods: list[bytes] = []
    g_gens = [_gen(8, 9000)]
    z1_gens = [_gen(43, 60 | (71 << 8)), _gen(53, 0)]                  # keys 60..71, sample 0
    z2_gens = [_gen(43, 72 | (127 << 8)), _gen(44, 0 | (63 << 8)), _gen(53, 1)]   # keys 72..127, vel 0..63
    ibag = b""
    gi = mi = 0
    for gens, mods in ((g_gens, g_mods), (z1_gens, z1_mods), (z2_gens, z2_mods)):
        ibag += struct.pack("<HH", gi, mi)
        gi += len(gens)
        mi += len(mods)
    ibag += struct.pack("<HH", gi, mi)
    igen = b"".join(g_gens + z1_gens + z2_gens) + _gen(0, 0)
    imod = b"".join(g_mods + z1_mods + z2_mods) + _mod(0, 0, 0)
    body = (b"sfbk"
            + _list(b"INFO", _chunk(b"ifil", struct.pack("<HH", 3, 1)), _chunk(b"INAM", b"test bank\0"))
            + _list(b"sdta", _chunk(b"smpl", smpl))
            + _list(b"pdta", _chunk(b"phdr", phdr), _chunk(b"pbag", pbag), _chunk(b"pmod", pmod),
                    _chunk(b"pgen", pgen), _chunk(b"inst", inst), _chunk(b"ibag", ibag),
                    _chunk(b"imod", imod), _chunk(b"igen", igen), _chunk(b"shdr", shdr)))
    return b"RIFF" + struct.pack("<I", len(body)) + body, [s1, s2]


def _zones(sf: conv.SoundFontFile) -> list[conv.Zone]:
    data = {c.cid: c.data for c in sf.pdta}
    _owners, zones = conv._parse_zones(data[b"inst"], 22, 20, data[b"ibag"], data[b"igen"], data[b"imod"])
    return zones


@pytest.fixture(scope="module")
def converted() -> tuple[conv.SoundFontFile, conv.ConversionStats, list[np.ndarray]]:
    raw, originals = make_sf3()
    stats = conv.ConversionStats()
    out = conv.convert_bytes(raw, stats)
    return conv.read_soundfont(out), stats, originals


def test_header_and_version(converted):
    sf, stats, _ = converted
    assert struct.unpack("<HH", sf.chunk(sf.info, b"ifil").data) == (2, 1)
    assert sf.chunk(sf.info, b"INAM").data.startswith(b"test bank")
    assert stats.samples == 3 and stats.compressed == 2 and stats.shared == 1


def test_samples_decoded_padded_and_rebased(converted):
    sf, _, originals = converted
    smpl = np.frombuffer(sf.chunk(sf.sdta, b"smpl").data, dtype="<i2")
    h = conv.read_headers(sf.chunk(sf.pdta, b"shdr").data)
    assert len(h) == 4 and h[-1].name.rstrip(b"\0") == b"EOS"
    assert [x.end - x.start for x in h[:3]] == [2000, 3000, 2000]
    assert h[0].start == 0 and h[1].start == 2000 + conv.ZERO_PAD
    assert len(smpl) == 2000 + 3000 + 2 * conv.ZERO_PAD
    for x in h[:2]:
        assert not x.sample_type & conv.VORBIS_FLAG
        assert np.all(smpl[x.end:x.end + conv.ZERO_PAD] == 0), "46 zero points after every sample"
    # Loops become absolute; the shared header reuses sample 0's data with its own loop.
    assert (h[0].start_loop, h[0].end_loop) == (100, 1900)
    assert (h[1].start_loop, h[1].end_loop) == (h[1].start, h[1].start)
    assert (h[2].start, h[2].end, h[2].start_loop, h[2].end_loop) == (0, 2000, 10, 20)
    # Vorbis is lossy but the waveform survives.
    for x, orig in zip(h[:2], originals):
        dec = smpl[x.start:x.end].astype(np.float64) / 32768
        corr = np.corrcoef(dec, orig.astype(np.float64))[0, 1]
        assert corr > 0.99


def test_velocity_modulator_baked_into_generator(converted):
    sf, stats, _ = converted
    zones = _zones(sf)
    z1 = zones[1]
    # zone 1: full velocity range -> velocity 100; -2400 * 100/128 = -1875 on top of the global 9000.
    assert conv._signed(z1.gen(8)) == 9000 - 1875
    # key 65 (middle of 60..71): bipolar (2*65/128 - 1) * 1200 = 18.75 -> 19 cents of modEnvToFilterFc
    assert conv._signed(z1.gen(11)) == 19
    kept = {(m[0], m[1]) for m in z1.mods}
    assert (VEL, 8) not in {(m[0], m[1]) for m in z1.mods if m[2] != 0}, "baked modulator removed"
    assert (VEL | (1 << 8) | (1 << 10), 48) in kept, "velocity->attenuation stays (MeltySynth does it)"
    assert (CC | 1, 5) in kept, "controller modulators stay"
    # The global key modulator is neutralized locally by an amount-0 override.
    assert (KEY | (1 << 9), 11, 0, 0, 0) in z1.mods
    # Generators stay ordered: key range first, sampleID last.
    assert z1.gens[0][0] == 43 and z1.gens[-1][0] == 53
    assert stats.baked_generators >= 3 and stats.baked_modulators >= 3


def test_zone_ranges_pick_representative_values(converted):
    sf, _, _ = converted
    z2 = _zones(sf)[2]
    # keys 72..127 -> key 99: (2*99/128 - 1) * 1200 = 656.25 -> 656; velocity range 0..63 has no velocity mod.
    assert conv._signed(z2.gen(11)) == 656
    assert z2.gen(8) is None
    assert z2.gens[0][0] == 43 and z2.gens[1][0] == 44 and z2.gens[-1][0] == 53


def test_keep_modulators_leaves_pdta_alone():
    raw, _ = make_sf3()
    sf_in = conv.read_soundfont(raw)
    sf_out = conv.read_soundfont(conv.convert_bytes(raw, bake=False))
    for cid in (b"pbag", b"pgen", b"pmod", b"ibag", b"igen", b"imod", b"phdr", b"inst"):
        assert sf_out.chunk(sf_out.pdta, cid).data == sf_in.chunk(sf_in.pdta, cid).data


def test_source_curves():
    assert conv._curve(0, 1) == 0 and conv._curve(1, 1) == 1          # concave
    assert conv._curve(0, 2) == 0 and conv._curve(1, 2) == 1          # convex
    assert conv._curve(0.49, 3) == 0 and conv._curve(0.5, 3) == 1     # switch
    assert 0 < conv._curve(0.5, 1) < 0.5 < conv._curve(0.5, 2) < 1
    assert conv.source_value(0, 100, 60) == 1.0                         # "no controller" = 1
    assert conv.source_value(VEL, 64, 60) == 0.5
    assert conv.source_value(VEL | (1 << 8), 64, 60) == 0.5            # negative direction
    assert conv.source_value(KEY | (1 << 9), 100, 64) == 0.0           # bipolar centre (key 64)
    assert conv.source_value(CC | 7, 100, 60) is None                   # controllers are not baked


def test_convert_file_writes_output_and_license(tmp_path):
    raw, _ = make_sf3()
    src = tmp_path / "Bank.sf3"
    src.write_bytes(raw)
    (tmp_path / "Bank_License.md").write_text("MIT, test", encoding="utf-8")
    dst = tmp_path / "out" / "Bank.sf2"
    stats = conv.convert(src, dst)
    assert stats is not None and dst.is_file()
    assert (tmp_path / "out" / "Bank_License.md").read_text(encoding="utf-8") == "MIT, test"
    assert conv.convert(src, dst) is None, "second run is a no-op"
    assert conv.main(["--input", str(src), "--output", str(dst), "--force"]) == 0
    assert conv.main(["--input", str(tmp_path / "missing.sf3"), "--output", str(dst)]) == 1


def test_rejects_non_soundfont():
    with pytest.raises(conv.SoundFontError):
        conv.convert_bytes(b"RIFF\x04\0\0\0WAVE")


REAL = conv.DEFAULT_OUTPUT


@pytest.mark.skipif(not REAL.is_file(), reason="data/soundfonts/MS_Basic.sf2 not converted yet")
def test_real_output_structure():
    raw = REAL.read_bytes()
    sf = conv.read_soundfont(raw)
    assert struct.unpack("<HH", sf.chunk(sf.info, b"ifil").data) == (2, 1)
    smpl = np.frombuffer(sf.chunk(sf.sdta, b"smpl").data, dtype="<i2")
    headers = conv.read_headers(sf.chunk(sf.pdta, b"shdr").data)[:-1]
    assert len(headers) == 2194
    assert not any(h.sample_type & conv.VORBIS_FLAG for h in headers)
    for h in headers:
        assert 0 <= h.start <= h.start_loop <= h.end_loop <= h.end <= len(smpl) - conv.ZERO_PAD
    for h in headers[::97]:
        assert not smpl[h.end:h.end + conv.ZERO_PAD].any()
    assert (REAL.parent / "MS_Basic_License.md").is_file()
