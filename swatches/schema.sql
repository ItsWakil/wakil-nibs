-- Ink swatch gallery tables for Wakil Nibs (pentracker.db)
-- Created on startup by swatches.init_swatches(). Never alters existing tables.

PRAGMA foreign_keys = ON;

-- One row per swatch photo. An ink can have several (different papers, nibs, lighting).
CREATE TABLE IF NOT EXISTS swatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ink_id      INTEGER NOT NULL REFERENCES inks(id) ON DELETE CASCADE,
    photo_path  TEXT    NOT NULL,              -- relative to static/, e.g. uploads/swatches/ab12.jpg
    paper       TEXT,                          -- e.g. "Tomoe River 52gsm"
    notes       TEXT,
    wb_r        REAL, wb_g REAL, wb_b REAL,    -- white-balance gains that were applied
    crop        TEXT,                          -- "left,top,right,bottom" in pixels, or NULL
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The extracted colors for each swatch. rank 0 is the dominant color.
-- Lab is stored so searches never have to recompute it.
CREATE TABLE IF NOT EXISTS swatch_colors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    swatch_id   INTEGER NOT NULL REFERENCES swatches(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,              -- 0 = largest share
    role        TEXT    NOT NULL,              -- Dominant / Pooling / Mid strokes / Light strokes
    hex         TEXT    NOT NULL,              -- '#5E1B24'
    lab_l       REAL    NOT NULL,
    lab_a       REAL    NOT NULL,
    lab_b       REAL    NOT NULL,
    share       REAL    NOT NULL,              -- 0..1 fraction of ink pixels
    UNIQUE (swatch_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_swatches_ink        ON swatches(ink_id);
CREATE INDEX IF NOT EXISTS idx_swatch_colors_sw    ON swatch_colors(swatch_id);

-- Handy view: one dominant color per ink (from its newest swatch).
CREATE VIEW IF NOT EXISTS ink_dominant AS
SELECT s.ink_id, c.hex, c.lab_l, c.lab_a, c.lab_b
FROM swatch_colors c
JOIN swatches s ON s.id = c.swatch_id
WHERE c.rank = 0
  AND s.id = (SELECT MAX(s2.id) FROM swatches s2 WHERE s2.ink_id = s.ink_id);
