"""Convert a SoundFont 3 (Ogg Vorbis compressed samples) into a plain SoundFont 2.

The Unity player uses MeltySynth, which reads SF2 only. The one General MIDI bank already on
this machine with a permissive license is MuseScore's ``MS Basic.sf3`` (MIT, derived from
FluidR3), so this script decodes it once into ``data/soundfonts/MS_Basic.sf2`` and copies its
license file next to it (the MIT notice must travel with derivative works).

SF3 is SF2 with one change, introduced by MuseScore's sftools and followed by FluidSynth:
in ``shdr`` a sample whose ``sfSampleType`` has bit 0x10 set holds Ogg Vorbis data, its
``dwStart``/``dwEnd`` are **byte** offsets of that Ogg stream inside the ``smpl`` chunk (end
exclusive), and ``dwStartloop``/``dwEndloop`` are **relative to the sample start**, in decoded
sample frames. Everything else (presets, instruments, generators, modulators) is plain SF2.

Conversion: decode every sample to 16-bit PCM (python-soundfile / libsndfile >= 1.0.29 decodes
Vorbis), lay the samples out back to back with the 46 zero points the SF2 spec requires after
each one, rewrite start/end/loop as absolute sample indices, clear the compression bit, set
``ifil`` to 2.01, and copy the other chunks unchanged. Uncompressed samples inside an SF3
(allowed, rarely used) are copied as they are.

Then, because MeltySynth ignores SoundFont modulators, velocity/key modulators are baked into
generators (see "modulator baking" below; ``--keep-modulators`` skips this for synths that
implement modulators, e.g. FluidSynth).

Usage::

    python tools/sf3_to_sf2.py [--input "C:/Program Files/MuseScore 4/sound/MS Basic.sf3"]
                               [--output data/soundfonts/MS_Basic.sf2] [--force] [--keep-modulators]
"""

from __future__ import annotations

import argparse
import io
import math
import shutil
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from musichistory import config  # noqa: E402

DEFAULT_INPUT = Path(r"C:\Program Files\MuseScore 4\sound\MS Basic.sf3")
DEFAULT_OUTPUT = config.SOUNDFONTS / "MS_Basic.sf2"

SHDR_SIZE = 46                  # bytes per sample header record
SHDR_FORMAT = "<20sIIIIIBbHH"   # name, start, end, startloop, endloop, rate, key, correction, link, type
ZERO_PAD = 46                   # zero sample points after every sample (SF2 2.01 §7.10)
VORBIS_FLAG = 0x10              # sfSampleType bit marking an Ogg Vorbis sample (SF3)
ROM_FLAG = 0x8000


class SoundFontError(ValueError):
    pass


@dataclass
class Chunk:
    """A RIFF sub-chunk: id plus raw payload (no padding byte)."""

    cid: bytes
    data: bytes


@dataclass
class SampleHeader:
    name: bytes
    start: int
    end: int
    start_loop: int
    end_loop: int
    sample_rate: int
    original_key: int
    correction: int
    link: int
    sample_type: int

    @classmethod
    def unpack(cls, raw: bytes, offset: int) -> "SampleHeader":
        return cls(*struct.unpack_from(SHDR_FORMAT, raw, offset))

    def pack(self) -> bytes:
        return struct.pack(SHDR_FORMAT, self.name, self.start, self.end, self.start_loop, self.end_loop,
                           self.sample_rate, self.original_key, self.correction, self.link, self.sample_type)

    @property
    def compressed(self) -> bool:
        return bool(self.sample_type & VORBIS_FLAG)


@dataclass
class SoundFontFile:
    info: list[Chunk]
    sdta: list[Chunk]
    pdta: list[Chunk]

    def chunk(self, lst: list[Chunk], cid: bytes) -> Chunk:
        for c in lst:
            if c.cid == cid:
                return c
        raise SoundFontError(f"missing {cid.decode()} chunk")


@dataclass
class ConversionStats:
    samples: int = 0
    compressed: int = 0
    shared: int = 0
    frames: int = 0
    output_bytes: int = 0
    seconds: float = 0.0
    baked_generators: int = 0
    baked_modulators: int = 0
    warnings: list[str] = field(default_factory=list)


def _sub_chunks(raw: bytes, offset: int, end: int) -> list[Chunk]:
    out: list[Chunk] = []
    while offset + 8 <= end:
        cid = raw[offset:offset + 4]
        size = struct.unpack_from("<I", raw, offset + 4)[0]
        if offset + 8 + size > end:
            raise SoundFontError(f"chunk {cid!r} at {offset} overruns its parent")
        out.append(Chunk(cid, raw[offset + 8:offset + 8 + size]))
        offset += 8 + size + (size & 1)
    return out


def read_soundfont(raw: bytes) -> SoundFontFile:
    """Splits an SF2/SF3 RIFF file into its three LIST bodies."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"sfbk":
        raise SoundFontError("not a RIFF sfbk file")
    riff_end = min(len(raw), 8 + struct.unpack_from("<I", raw, 4)[0])
    lists: dict[bytes, list[Chunk]] = {}
    for c in _sub_chunks(raw, 12, riff_end):
        if c.cid != b"LIST" or len(c.data) < 4:
            continue
        lists[c.data[:4]] = _sub_chunks(c.data, 4, len(c.data))
    missing = [k.decode() for k in (b"INFO", b"sdta", b"pdta") if k not in lists]
    if missing:
        raise SoundFontError(f"missing LIST {', '.join(missing)}")
    return SoundFontFile(lists[b"INFO"], lists[b"sdta"], lists[b"pdta"])


def read_headers(shdr: bytes) -> list[SampleHeader]:
    if len(shdr) % SHDR_SIZE:
        raise SoundFontError("shdr size is not a multiple of 46")
    return [SampleHeader.unpack(shdr, i) for i in range(0, len(shdr), SHDR_SIZE)]


def decode_vorbis(data: bytes) -> np.ndarray:
    """One Ogg Vorbis stream -> mono int16 frames."""
    pcm, _rate = soundfile.read(io.BytesIO(data), dtype="int16", always_2d=True)
    if pcm.shape[1] != 1:
        raise SoundFontError(f"expected a mono Vorbis sample, got {pcm.shape[1]} channels")
    return np.ascontiguousarray(pcm[:, 0])


# ------------------------------------------------------------------ modulator baking
#
# MeltySynth implements the SF2 generators but ignores the modulator lists (pmod/imod) apart
# from its built-in velocity->volume curve and MIDI controllers. MS Basic relies on modulators:
# its pianos have a 300 Hz base filter that velocity opens through the modulation envelope,
# so without them they render ~30 dB too quiet and muffled. Baking evaluates every modulator
# whose sources are note velocity and/or key number at a representative value for its zone
# (the midpoint of the zone's velocity/key range; velocity 100 and key 60 for full ranges)
# and adds the result to the zone's generator. Velocity->attenuation stays a modulator
# (MeltySynth's own velocity curve already does that job), as do controller-driven ones.

GEN_KEY_RANGE, GEN_VEL_RANGE, GEN_INSTRUMENT, GEN_SAMPLE_ID = 43, 44, 41, 53
GEN_ATTENUATION = 48
SRC_NONE, SRC_VELOCITY, SRC_KEY = 0, 2, 3
NOMINAL_VELOCITY, NOMINAL_KEY = 100, 60

# SF2 2.04 §8.1.3 default values and valid ranges of the generators baking can touch.
GEN_DEFAULT = {8: 13500, 21: -12000, 23: -12000, 25: -12000, 26: -12000, 27: -12000, 28: -12000,
               30: -12000, 33: -12000, 34: -12000, 35: -12000, 36: -12000, 38: -12000, 56: 100}
GEN_RANGE = {5: (-12000, 12000), 6: (-12000, 12000), 7: (-12000, 12000), 8: (1500, 13500), 9: (0, 960),
             10: (-12000, 12000), 11: (-12000, 12000), 13: (-960, 960), 15: (0, 1000), 16: (0, 1000),
             17: (-500, 500), 21: (-12000, 5000), 22: (-16000, 4500), 23: (-12000, 5000), 24: (-16000, 4500),
             25: (-12000, 5000), 26: (-12000, 8000), 27: (-12000, 5000), 28: (-12000, 8000), 29: (0, 1000),
             30: (-12000, 8000), 31: (-1200, 1200), 32: (-1200, 1200), 33: (-12000, 5000), 34: (-12000, 8000),
             35: (-12000, 5000), 36: (-12000, 8000), 37: (0, 1440), 38: (-12000, 8000), 39: (-1200, 1200),
             40: (-1200, 1200), 48: (0, 1440), 51: (-120, 120), 52: (-99, 99), 56: (0, 1200)}
RANGE_GENS = (GEN_KEY_RANGE, GEN_VEL_RANGE)


@dataclass
class Zone:
    gens: list[tuple[int, int]]                         # (operator, raw unsigned 16-bit amount)
    mods: list[tuple[int, int, int, int, int]]          # (src, dest, amount (signed), amount src, transform)

    def gen(self, oper: int) -> int | None:
        for o, v in self.gens:
            if o == oper:
                return v
        return None

    def span(self, oper: int) -> tuple[int, int]:
        v = self.gen(oper)
        return (0, 127) if v is None else (v & 0xFF, v >> 8)


def _signed(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v


def _curve(x: float, kind: int) -> float:
    """SF2 source curve on a unipolar input already oriented by its direction bit."""
    x = min(max(x, 0.0), 1.0)
    if kind == 1:   # concave: -20/96 * log10((1-x)^2)
        return 1.0 if x >= 1 else min(1.0, max(0.0, -(40.0 / 96.0) * math.log10(1.0 - x)))
    if kind == 2:   # convex
        return 1.0 - _curve(1.0 - x, 1)
    if kind == 3:   # switch
        return 1.0 if x >= 0.5 else 0.0
    return x        # linear


def source_value(oper: int, velocity: int, key: int) -> float | None:
    """Mapped value of a modulator source; None when the source is not velocity/key/none."""
    index, is_cc = oper & 0x7F, (oper >> 7) & 1
    negative, bipolar, kind = (oper >> 8) & 1, (oper >> 9) & 1, oper >> 10
    if is_cc:
        return None
    if index == SRC_NONE:
        return 1.0
    if index == SRC_VELOCITY:
        raw = velocity
    elif index == SRC_KEY:
        raw = key
    else:
        return None
    x = raw / 128.0
    if negative:
        x = 1.0 - x
    if not bipolar:
        return _curve(x, kind)
    t = 2.0 * x - 1.0
    if kind == 0:
        return t
    return (1.0 if t >= 0 else -1.0) * _curve(abs(t), kind)


def _representative(lo: int, hi: int, nominal: int) -> int:
    return nominal if (lo, hi) == (0, 127) else (lo + hi) // 2


def _parse_zones(hdr: bytes, hdr_size: int, bag_index_offset: int, bag: bytes, gen: bytes, mod: bytes
                 ) -> tuple[list[list[int]], list[Zone]]:
    """Returns (bag indices of each preset/instrument, zones)."""
    n_hdr = len(hdr) // hdr_size
    bag_start = [struct.unpack_from("<H", hdr, i * hdr_size + bag_index_offset)[0] for i in range(n_hdr)]
    bags = [struct.unpack_from("<HH", bag, i) for i in range(0, len(bag), 4)]
    gens = [struct.unpack_from("<HH", gen, i) for i in range(0, len(gen), 4)]
    mods = [struct.unpack_from("<HHhHH", mod, i) for i in range(0, len(mod), 10)]
    zones = [Zone(list(gens[bags[b][0]:bags[b + 1][0]]), list(mods[bags[b][1]:bags[b + 1][1]]))
             for b in range(len(bags) - 1)]
    owners = [list(range(bag_start[i], bag_start[i + 1])) for i in range(n_hdr - 1)]
    return owners, zones


def _bake_level(owners: list[list[int]], zones: list[Zone], terminal_gen: int, absolute: bool,
                counts: dict[str, int]) -> None:
    for bag_ids in owners:
        if not bag_ids:
            continue
        first = zones[bag_ids[0]]
        has_global = not first.gens or first.gens[-1][0] != terminal_gen
        global_zone = first if has_global and len(bag_ids) > 1 else None
        for b in bag_ids:
            zone = zones[b]
            if zone is global_zone or not zone.gens or zone.gens[-1][0] != terminal_gen:
                continue
            # Local modulators replace global ones with the same (src, dest, amount src).
            effective: dict[tuple[int, int, int], tuple[int, int, int, int, int]] = {}
            for m in (global_zone.mods if global_zone else []):
                effective[(m[0], m[1], m[3])] = m
            local_ids = {(m[0], m[1], m[3]) for m in zone.mods}
            for m in zone.mods:
                effective[(m[0], m[1], m[3])] = m
            vel = _representative(*zone.span(GEN_VEL_RANGE), NOMINAL_VELOCITY)
            key = _representative(*zone.span(GEN_KEY_RANGE), NOMINAL_KEY)
            deltas: dict[int, float] = {}
            baked: set[tuple[int, int, int]] = set()
            for ident, (src, dest, amount, amt_src, trans) in effective.items():
                if dest & 0x8000 or dest in RANGE_GENS or dest in (GEN_INSTRUMENT, GEN_SAMPLE_ID):
                    continue
                uses_velocity = (src & 0xFF) == SRC_VELOCITY or (amt_src & 0xFF) == SRC_VELOCITY
                if dest == GEN_ATTENUATION and uses_velocity:
                    continue    # MeltySynth's built-in velocity curve covers this one
                a, s = source_value(src, vel, key), source_value(amt_src, vel, key)
                if a is None or s is None or (src & 0x7F) == SRC_NONE:
                    continue
                value = amount * a * s
                if trans == 2:
                    value = abs(value)
                deltas[dest] = deltas.get(dest, 0.0) + value
                if amount != 0:
                    baked.add(ident)
            if not deltas:
                continue
            gens = list(zone.gens)
            for dest, delta in deltas.items():
                if round(delta) == 0:
                    continue
                current = zone.gen(dest)
                if current is not None:
                    base = _signed(current)
                elif global_zone is not None and global_zone.gen(dest) is not None:
                    base = _signed(global_zone.gen(dest))  # type: ignore[arg-type]
                else:
                    base = GEN_DEFAULT.get(dest, 0) if absolute else 0
                lo, hi = GEN_RANGE.get(dest, (-32768, 32767))
                if absolute:
                    value = min(max(round(base + delta), lo), hi)
                else:   # preset generators are offsets; keep them within the span of the range
                    value = min(max(round(base + delta), lo - hi), hi - lo)
                raw = value & 0xFFFF
                if current is not None:
                    gens = [(o, raw if o == dest else v) for o, v in gens]
                else:
                    gens.insert(len(gens) - 1, (dest, raw))  # before the instrument/sampleID terminal
                counts["generators"] += 1
            zone.gens = gens
            # A baked global modulator stays in the global zone for the zones it did not reach;
            # neutralize it locally with an amount-0 override so a compliant synth does not
            # apply it twice. Baked local modulators are simply dropped.
            new_mods = [m for m in zone.mods if (m[0], m[1], m[3]) not in baked]
            for ident in baked:
                if ident not in local_ids:
                    src, dest, _amount, amt_src, trans = effective[ident]
                    new_mods.append((src, dest, 0, amt_src, trans))
            counts["modulators"] += len(baked)
            zone.mods = new_mods


def _serialize_zones(zones: list[Zone]) -> tuple[bytes, bytes, bytes]:
    bag, gen, mod = bytearray(), bytearray(), bytearray()
    gi = mi = 0
    for z in zones:
        bag += struct.pack("<HH", gi, mi)
        for o, v in z.gens:
            gen += struct.pack("<HH", o, v)
        for m in z.mods:
            mod += struct.pack("<HHhHH", *m)
        gi += len(z.gens)
        mi += len(z.mods)
    bag += struct.pack("<HH", gi, mi)
    gen += struct.pack("<HH", 0, 0)
    mod += struct.pack("<HHhHH", 0, 0, 0, 0, 0)
    return bytes(bag), bytes(gen), bytes(mod)


def bake_modulators(pdta: list[Chunk]) -> tuple[list[Chunk], dict[str, int]]:
    """Folds velocity/key modulators into generators (see the section comment above)."""
    data = {c.cid: c.data for c in pdta}
    counts = {"generators": 0, "modulators": 0}
    p_owners, p_zones = _parse_zones(data[b"phdr"], 38, 24, data[b"pbag"], data[b"pgen"], data[b"pmod"])
    i_owners, i_zones = _parse_zones(data[b"inst"], 22, 20, data[b"ibag"], data[b"igen"], data[b"imod"])
    _bake_level(p_owners, p_zones, GEN_INSTRUMENT, absolute=False, counts=counts)
    _bake_level(i_owners, i_zones, GEN_SAMPLE_ID, absolute=True, counts=counts)
    pbag, pgen, pmod = _serialize_zones(p_zones)
    ibag, igen, imod = _serialize_zones(i_zones)
    replaced = {b"pbag": pbag, b"pgen": pgen, b"pmod": pmod, b"ibag": ibag, b"igen": igen, b"imod": imod}
    return [Chunk(c.cid, replaced.get(c.cid, c.data)) for c in pdta], counts


def _riff_list(kind: bytes, chunks: list[Chunk]) -> bytes:
    body = bytearray(kind)
    for c in chunks:
        body += c.cid + struct.pack("<I", len(c.data)) + c.data
        if len(c.data) & 1:
            body += b"\0"
    return b"LIST" + struct.pack("<I", len(body)) + bytes(body)


def convert_bytes(raw: bytes, stats: ConversionStats | None = None, *, bake: bool = True) -> bytes:
    """Converts SF3 bytes to SF2 bytes (an SF2 input is re-laid out, and baked when ``bake``)."""
    stats = stats if stats is not None else ConversionStats()
    sf = read_soundfont(raw)
    smpl = sf.chunk(sf.sdta, b"smpl").data
    headers = read_headers(sf.chunk(sf.pdta, b"shdr").data)
    if not headers:
        raise SoundFontError("shdr is empty")
    body, terminal = headers[:-1], headers[-1]   # the last record is the EOS terminator

    pieces: list[np.ndarray] = []
    cursor = 0
    placed: dict[tuple[int, int, bool], tuple[int, int]] = {}   # source span -> (start, frames) in output
    out_headers: list[SampleHeader] = []
    pad = np.zeros(ZERO_PAD, dtype=np.int16)
    for h in body:
        if h.sample_type & ROM_FLAG:
            raise SoundFontError(f"ROM sample {h.name!r} is not supported")
        key = (h.start, h.end, h.compressed)
        if key in placed:
            # Two headers over the same stored data: share the decoded copy.
            new_start, frames = placed[key]
            stats.shared += 1
        else:
            if h.compressed:
                if not (0 <= h.start < h.end <= len(smpl)):
                    raise SoundFontError(f"sample {h.name!r}: byte range {h.start}..{h.end} outside smpl")
                pcm = decode_vorbis(smpl[h.start:h.end])
                stats.compressed += 1
            else:
                if not (0 <= h.start <= h.end <= len(smpl) // 2):
                    raise SoundFontError(f"sample {h.name!r}: range {h.start}..{h.end} outside smpl")
                pcm = np.frombuffer(smpl, dtype="<i2", count=h.end - h.start, offset=2 * h.start).astype(np.int16)
            new_start, frames = cursor, len(pcm)
            pieces.append(pcm)
            pieces.append(pad)
            cursor += frames + ZERO_PAD
            placed[key] = (new_start, frames)
            stats.frames += frames
        # SF3 loop points are relative to the sample; plain SF2 ones are absolute.
        rel_start = h.start_loop if h.compressed else h.start_loop - h.start
        rel_end = h.end_loop if h.compressed else h.end_loop - h.start
        if not (0 <= rel_start <= rel_end <= frames):
            stats.warnings.append(f"{h.name.rstrip(bytes(1)).decode('latin-1')}: loop {rel_start}..{rel_end} "
                                  f"clamped to 0..{frames}")
            rel_start = min(max(rel_start, 0), frames)
            rel_end = min(max(rel_end, rel_start), frames)
        out_headers.append(SampleHeader(
            h.name, new_start, new_start + frames, new_start + rel_start, new_start + rel_end,
            h.sample_rate, h.original_key, h.correction, h.link, h.sample_type & ~VORBIS_FLAG))
        stats.samples += 1
    out_headers.append(SampleHeader(terminal.name, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    pcm_all = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)
    new_smpl = pcm_all.astype("<i2", copy=False).tobytes()

    info = [Chunk(b"ifil", struct.pack("<HH", 2, 1)) if c.cid == b"ifil" else c for c in sf.info]
    if not any(c.cid == b"ifil" for c in info):
        info.insert(0, Chunk(b"ifil", struct.pack("<HH", 2, 1)))
    # sm24 (24-bit extension) cannot describe decoded Vorbis data; drop it with the old smpl.
    sdta = [Chunk(b"smpl", new_smpl)]
    pdta = [Chunk(c.cid, b"".join(h.pack() for h in out_headers)) if c.cid == b"shdr" else c for c in sf.pdta]
    if bake:
        pdta, counts = bake_modulators(pdta)
        stats.baked_generators, stats.baked_modulators = counts["generators"], counts["modulators"]

    body_bytes = b"sfbk" + _riff_list(b"INFO", info) + _riff_list(b"sdta", sdta) + _riff_list(b"pdta", pdta)
    if len(body_bytes) >= 2**32:
        raise SoundFontError("decoded SoundFont exceeds the 4 GB RIFF limit")
    out = b"RIFF" + struct.pack("<I", len(body_bytes)) + body_bytes
    stats.output_bytes = len(out)
    return out


def convert(src: Path, dst: Path, *, force: bool = False, bake: bool = True) -> ConversionStats | None:
    """Converts ``src`` into ``dst`` (atomic write) and copies the license file next to it.

    Returns None when ``dst`` is already newer than ``src`` and ``force`` is False.
    """
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(src)
    if not force and dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        _copy_license(src, dst)
        return None
    t0 = time.perf_counter()
    stats = ConversionStats()
    data = convert_bytes(src.read_bytes(), stats, bake=bake)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst)
    _copy_license(src, dst)
    stats.seconds = time.perf_counter() - t0
    return stats


def _copy_license(src: Path, dst: Path) -> Path | None:
    """MuseScore ships ``<name>_License.md`` beside the bank; keep it beside the output."""
    for candidate in (src.with_name(f"{src.stem}_License.md"), src.with_name(f"{src.stem}_License.txt")):
        if candidate.is_file():
            target = dst.with_name(f"{dst.stem}_License{candidate.suffix}")
            shutil.copyfile(candidate, target)
            return target
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="SF3 (or SF2) file")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="SF2 file to write")
    parser.add_argument("--force", action="store_true", help="convert even if the output is up to date")
    parser.add_argument("--keep-modulators", action="store_true",
                        help="do not bake velocity/key modulators into generators (for synths that implement them)")
    args = parser.parse_args(argv)
    try:
        stats = convert(args.input, args.output, force=args.force, bake=not args.keep_modulators)
    except (OSError, SoundFontError, RuntimeError) as exc:
        print(f"sf3_to_sf2: {exc}", file=sys.stderr)
        return 1
    if stats is None:
        print(f"up to date: {args.output}")
        return 0
    for w in stats.warnings[:20]:
        print(f"warning: {w}", file=sys.stderr)
    print(f"wrote {args.output}: {stats.samples} samples ({stats.compressed} decoded, {stats.shared} shared), "
          f"{stats.frames} frames, {stats.output_bytes / 1e6:.1f} MB in {stats.seconds:.1f} s; baked "
          f"{stats.baked_modulators} modulators into {stats.baked_generators} generators"
          + (f", {len(stats.warnings)} loop warnings" if stats.warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
