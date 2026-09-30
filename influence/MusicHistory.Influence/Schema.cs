namespace MusicHistory.Influence;

/// <summary>
/// DDL this stage needs. <see cref="Pipeline"/> is a verbatim copy of <c>musichistory/db.py</c>
/// <c>SCHEMA</c> (schema_version 1): the influence stage creates only its own three tables with it
/// when they are missing, and <c>make-fixture</c> uses all of it to write a synthetic pipeline DB.
/// A unit test compares this copy with db.py so the two cannot drift apart silently.
/// <see cref="Graph"/> is the graph database of DESIGN.md §10 (SQLite 3.15 compatible: no STRICT,
/// no generated columns, no window functions or UPSERT, journal_mode=DELETE).
/// </summary>
internal static class Schema
{
    public const int PipelineSchemaVersion = 1;
    public const int GraphSchemaVersion = 1;

    public const string Pipeline = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- ---------------------------------------------------------------- canon (song list)
CREATE TABLE IF NOT EXISTS source_list(
  list_id TEXT PRIMARY KEY,          -- e.g. 'billboard_ye_1975', 'tsort_5000', 'rs500_2021'
  name TEXT NOT NULL, edition TEXT, url TEXT, revision TEXT, sha256 TEXT,
  retrieved_at TEXT, license_note TEXT,
  weight REAL NOT NULL,              -- RRF weight W_l
  pseudo_rank INTEGER                -- rank used for unranked lists (Grammy HoF)
);
CREATE TABLE IF NOT EXISTS list_entry(
  list_id TEXT NOT NULL REFERENCES source_list(list_id),
  rank INTEGER NOT NULL, raw_title TEXT NOT NULL, raw_artist TEXT NOT NULL,
  list_year INTEGER, wiki_link TEXT,
  weight_factor REAL NOT NULL DEFAULT 1.0,   -- 0.5 for the B side of a double A-side
  work_id TEXT, resolution_method TEXT, resolution_confidence REAL,
  PRIMARY KEY(list_id, rank, raw_title, raw_artist)
);
CREATE TABLE IF NOT EXISTS work(
  work_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,               -- display title of the composition
  canonical_artist TEXT NOT NULL,    -- performer of the highest-scoring recording (for MIDI search)
  original_artist TEXT,              -- first performer, when known and different
  search_artists TEXT,               -- JSON array of artist names worth searching MIDI sources for
  work_date TEXT,                    -- ISO date of the original release (YYYY, YYYY-MM or YYYY-MM-DD)
  work_date_precision INTEGER,       -- 9 = year, 10 = month, 11 = day (Wikidata convention)
  work_year INTEGER,                 -- original year: the node's position in time
  effective_year INTEGER,            -- year of the famous (canonical) recording
  year_confidence TEXT,              -- 'high' | 'medium' | 'review'
  traditional INTEGER NOT NULL DEFAULT 0,
  wikidata_qid TEXT, mb_work_id TEXT,
  first_chart_week TEXT,             -- earliest weekly Hot 100 week of any recording (ISO date)
  rrf_score REAL NOT NULL DEFAULT 0,
  canon_rank INTEGER,                -- 1 = highest fused score
  in_pool INTEGER NOT NULL DEFAULT 0,   -- 1 = among the POOL_SIZE works handed to acquisition
  selected INTEGER NOT NULL DEFAULT 0   -- 1 = in the final TARGET_SONGS; 2 = validation-control extra
                                        -- outside the ranked set (both set by select)
);
CREATE INDEX IF NOT EXISTS work_rank ON work(canon_rank);
CREATE TABLE IF NOT EXISTS year_evidence(
  work_id TEXT NOT NULL, source TEXT NOT NULL, value TEXT NOT NULL,
  precision INTEGER, accepted INTEGER NOT NULL DEFAULT 1, reason TEXT
);
CREATE INDEX IF NOT EXISTS year_evidence_work ON year_evidence(work_id);
CREATE TABLE IF NOT EXISTS known_influence(   -- ground truth for validation only
  src_work_id TEXT NOT NULL, dst_work_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- 'wikidata_P144' | 'wikidata_P2550' | 'control_positive' | ...
  note TEXT,
  PRIMARY KEY(src_work_id, dst_work_id, kind)
);
-- Version controls whose recordings merged into one work have src_work_id = dst_work_id.
CREATE INDEX IF NOT EXISTS known_influence_dst ON known_influence(dst_work_id);

-- ---------------------------------------------------------------- fetch / select
CREATE TABLE IF NOT EXISTS fetch_log(
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, status INTEGER, content_type TEXT,
  bytes INTEGER, sha256 TEXT, elapsed_ms REAL, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS candidate(
  candidate_id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work(work_id),
  source TEXT NOT NULL,              -- 'lakh' | 'lakh_clean' | 'midicollection' | 'freemidi' | 'midiworld'
  source_ref TEXT,                   -- source-specific id (LMD md5, freemidi id, site path...)
  url TEXT, orig_name TEXT,          -- original path/filename as the source names it
  md5 TEXT NOT NULL,                 -- MD5 of the ORIGINAL bytes (Lakh identity; dedupe key)
  sha256 TEXT,                       -- SHA-256 of the SANITIZED file
  bytes INTEGER,
  title_score REAL, artist_score REAL, -- 0..100 from textnorm
  match_class TEXT,                  -- 'accept' | 'probable'
  lmd_match_score REAL,              -- Lakh audio match score when the file is in lmd_matched
  lmd_msd_id TEXT,
  sanitized_path TEXT,               -- data/candidates/<work_id>/<source>__<md5>.mid: relative to the
                                     -- repo root when DATA is inside it, else absolute;
                                     -- resolve with config.ROOT / path
  valid INTEGER NOT NULL DEFAULT 0,
  invalid_reason TEXT,
  features_json TEXT,                -- musichistory.midi.features output (see DESIGN.md)
  quality_score REAL,                -- static quality 0..1
  consensus_score REAL,              -- agreement with the song's other candidates 0..1
  hooktheory_score REAL,             -- agreement with Hooktheory annotations 0..1 (NULL = none)
  total_score REAL,
  analyzed INTEGER NOT NULL DEFAULT 0,  -- 1 = PatternPrep ran during selection
  chosen INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT,
  UNIQUE(work_id, md5)
);
CREATE INDEX IF NOT EXISTS candidate_work ON candidate(work_id);
CREATE TABLE IF NOT EXISTS selection(
  work_id TEXT PRIMARY KEY REFERENCES work(work_id),
  candidate_id INTEGER NOT NULL REFERENCES candidate(candidate_id),
  n_candidates INTEGER, n_sources INTEGER, n_analyzed INTEGER,
  reason TEXT                        -- short human-readable explanation of the pick
);

-- ---------------------------------------------------------------- analyze (identities)
CREATE TABLE IF NOT EXISTS song(
  work_id TEXT PRIMARY KEY REFERENCES work(work_id),
  candidate_id INTEGER REFERENCES candidate(candidate_id),
  midi_path TEXT NOT NULL,           -- data/songs/<work_id>/score.mid (sanitized, native key/tempo)
  normalized_midi_path TEXT,         -- data/normalized/<work_id>.mid (C major/A minor at TARGET_BPM)
  patterns_path TEXT,                -- slim analysis JSON (analysis.json) path
  analysis_ok INTEGER NOT NULL DEFAULT 0, error TEXT,
  resonance_commit TEXT, analyzed_at TEXT,
  n_bars INTEGER, n_notes INTEGER, duration_s REAL, end_beat REAL,
  style TEXT, form_grammar TEXT,
  tonic_pc INTEGER,                  -- native home tonic, C = 0
  mode TEXT,                         -- 'major' | 'minor'
  key_confidence REAL,               -- 0..1 ensemble agreement
  key_ambiguous_fifth INTEGER NOT NULL DEFAULT 0,
  key_review INTEGER NOT NULL DEFAULT 0,
  norm_shift INTEGER,                -- semitones added to native pitches to reach the target key, in [-5, 6]
  shift_parallel INTEGER,            -- same, for parallel normalization (tonic -> C)
  native_bpm REAL,                   -- beat-weighted median quarter-note BPM
  beats_per_bar REAL,                -- quarter-note beats per bar of the dominant meter
  first_downbeat REAL,               -- first bar line of the dominant-meter grid at or before the music
  melody_track INTEGER, melody_channel INTEGER,  -- sanitized-file track index (0-based), MIDI channel 1..16
  melody_method TEXT,                -- 'name' | 'lyric_timing' | 'classifier' | 'skyline'
  melody_confidence REAL,            -- 0..1
  interval_entropy REAL,             -- bits; melody informativeness gate
  n_melody_notes INTEGER,
  bass_track INTEGER, bass_channel INTEGER,
  main_loop TEXT,                    -- roman-numeral summary of the most-covering loop (C/Am frame)
  summary_json TEXT,                 -- small display facts (sections, grammar, chord summary)
  normalization TEXT,                -- 'relative' | 'parallel' used for this song's identities and normalized MIDI
  target_bpm REAL                    -- tempo of this song's normalized MIDI
);
CREATE TABLE IF NOT EXISTS key_region(
  work_id TEXT NOT NULL, start_beat REAL NOT NULL, end_beat REAL NOT NULL,
  tonic_pc INTEGER NOT NULL, mode TEXT NOT NULL,
  shift INTEGER NOT NULL,            -- normalization shift applied to this region
  PRIMARY KEY(work_id, start_beat)
);
-- Chord sequences over the normalized key frame.
--   L1 token = root_pc * 3 + q   with q: 0 maj, 1 min, 2 dim        (36 symbols)
--   L2 token = root_pc * 6 + q   with q: 0 '', 1 m, 2 7, 3 maj7, 4 m7, 5 dim   (72 symbols)
--   keyfree token (kind 'keyfree', level 'L1' only) = ((root_b - root_a) % 12) * 9 + qa * 3 + qb
--                                                      (108 symbols, L1 qualities)
-- kinds: 'chg' (changes, passing chords < 0.75 beat dropped, repeats collapsed),
--        'cd'  (chg tokens with duration class: token * 8 + clip(round(log2(beats)), -1, 4) + 1),
--        'beat' (one L1 token per beat; -1 = no chord), 'keyfree' (from chg).
CREATE TABLE IF NOT EXISTS chord_seq(
  work_id TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL,   -- level 'L1' | 'L2'
  tokens TEXT NOT NULL,              -- JSON int array
  starts TEXT NOT NULL,              -- JSON float array: start beat of each token
  durs TEXT NOT NULL,                -- JSON float array: duration in beats
  downbeat TEXT,                     -- JSON 0/1 array: token starts on a bar line
  PRIMARY KEY(work_id, kind, level)
);
-- Rotation-invariant loops from Resonance Patterns (families that repeat).
CREATE TABLE IF NOT EXISTS loop(
  work_id TEXT NOT NULL, family INTEGER NOT NULL,
  cycle_id TEXT NOT NULL,            -- Booth-minimal rotation of the primitive L1 cycle, tokens joined by '.'
  phase INTEGER NOT NULL,            -- rotation index of the song's own loop start within cycle_id
  rhythm_sig TEXT NOT NULL,          -- duration classes rotated like cycle_id, joined by '.'
  cycle_tokens TEXT NOT NULL,        -- JSON int array (L1), song's own phase
  roman TEXT,                        -- display, C/Am frame, e.g. 'I-V-vi-IV'
  loop_beats REAL NOT NULL, passes INTEGER NOT NULL, visits INTEGER NOT NULL,
  coverage_beats REAL NOT NULL,      -- beats of the song this family covers
  visit_starts TEXT NOT NULL,        -- JSON float array: absolute start beats of visits
  PRIMARY KEY(work_id, family)
);
-- Normalized note lines. role 'melody' = lead line; 'bass' = lowest line (riff channel).
CREATE TABLE IF NOT EXISTS melody_line(
  work_id TEXT NOT NULL, role TEXT NOT NULL,
  onsets TEXT NOT NULL,              -- JSON float array (beats, quantized to 1/12 of their bar's grid)
  durs TEXT NOT NULL,                -- JSON float array (beats): time to the next onset
                                     -- (rests absorbed); the last note keeps its own length
  pitches TEXT NOT NULL,             -- JSON int array: MIDI pitch + region shift (normalized)
  met TEXT NOT NULL,                 -- JSON int array: 0 downbeat, 1 beat, 2 eighth, 3 other
  PRIMARY KEY(work_id, role)
);

-- ---------------------------------------------------------------- influence (C#)
CREATE TABLE IF NOT EXISTS pair_score(
  a_id TEXT NOT NULL, b_id TEXT NOT NULL,   -- a earlier than b
  e_melody REAL, e_bass REAL, e_chord REAL, e_loop REAL,
  z_melody REAL, z_bass REAL, z_chord REAL, z_loop REAL,
  z_combined REAL, q REAL, s_bits REAL,
  pmi REAL, chord_identity REAL,
  significant INTEGER NOT NULL DEFAULT 0,
  relation TEXT,                     -- 'influence' | 'version' | 'contemporaneous' | 'none'
  segments_json TEXT,                -- [{channel, a_start, a_end, b_start, b_end, bits}]
  PRIMARY KEY(a_id, b_id)
);
CREATE TABLE IF NOT EXISTS influence_edge(
  a_id TEXT NOT NULL, b_id TEXT NOT NULL, kind TEXT NOT NULL,   -- 'tree' | 'secondary'
  s_bits REAL NOT NULL, credited INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(a_id, b_id)
);
CREATE TABLE IF NOT EXISTS tree_node(
  work_id TEXT PRIMARY KEY, parent_id TEXT, root_id TEXT NOT NULL, depth INTEGER NOT NULL,
  ref_count INTEGER NOT NULL, ref_norm REAL, katz REAL, descendants INTEGER NOT NULL
);
""";

    /// <summary>The influence stage's own tables (a subset of <see cref="Pipeline"/>).</summary>
    public static string InfluenceTables
    {
        get
        {
            int start = Pipeline.IndexOf("CREATE TABLE IF NOT EXISTS pair_score", StringComparison.Ordinal);
            return Pipeline[start..];
        }
    }

    public const string Graph = """
CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes(id INTEGER PRIMARY KEY,
  position_x REAL, position_y REAL, position_z REAL);
CREATE TABLE song_node(
  node_id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, artist TEXT NOT NULL,
  year INTEGER NOT NULL, release_date TEXT, date_precision INTEGER,
  time_value REAL NOT NULL, canon_rank INTEGER,
  tonic_pc INTEGER NOT NULL, mode TEXT NOT NULL, key_name TEXT NOT NULL,
  norm_shift INTEGER NOT NULL,
  native_bpm REAL NOT NULL, beats_per_bar REAL NOT NULL, first_downbeat REAL NOT NULL,
  midi_path TEXT NOT NULL,
  normalized_midi_path TEXT,
  midi_source TEXT,
  excerpt_start_beat REAL NOT NULL, excerpt_end_beat REAL NOT NULL,
  entry_tonic_pc INTEGER, entry_mode TEXT,
  exit_tonic_pc INTEGER, exit_mode TEXT,
  tree_parent_node INTEGER, tree_root_node INTEGER NOT NULL, tree_depth INTEGER NOT NULL,
  ref_count INTEGER NOT NULL, ref_norm REAL, katz REAL, descendants INTEGER NOT NULL,
  in_degree INTEGER NOT NULL, out_degree INTEGER NOT NULL,
  key_confidence REAL, melody_confidence REAL,
  main_loop TEXT,
  summary TEXT
);
CREATE TABLE influence_edges(
  id INTEGER PRIMARY KEY,
  source_node INTEGER NOT NULL, target_node INTEGER NOT NULL,
  kind TEXT NOT NULL,
  channels TEXT NOT NULL,
  primary_channel TEXT NOT NULL,
  score_bits REAL NOT NULL, z REAL NOT NULL, q REAL,
  similarity REAL NOT NULL,
  weight REAL NOT NULL,
  src_start_beat REAL, src_end_beat REAL, dst_start_beat REAL, dst_end_beat REAL,
  evidence TEXT,
  UNIQUE(source_node, target_node), CHECK(source_node < target_node));
""";
}
