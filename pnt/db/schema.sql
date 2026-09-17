-- PokerNow Tracker schema.
--
-- Three layers, each rebuildable from the one above it:
--
--   1. raw_entries      immutable truth, exactly as PokerNow emitted it
--   2. hands/actions    parsed, DISPOSABLE, rebuilt by re-running the importer
--   3. stats            SQL at read time -- nothing aggregated is ever stored
--
-- No counter is persisted anywhere. That is deliberate: a counter that misses one
-- hand is permanently wrong with no way to detect or repair it, whereas raw
-- actions let a re-import fix any gap and let a stat invented next month compute
-- retroactively across all history.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- layer 1 ----

-- Every log line ever seen. Keeping these means a parser improvement can
-- re-derive everything WITHOUT needing the original CSV again. This is what
-- makes "a re-import repairs any gap" actually true rather than aspirational.
CREATE TABLE IF NOT EXISTS raw_entries (
    game_id TEXT NOT NULL,
    ord     INTEGER NOT NULL,   -- epoch_ms * 100 + seq; globally unique, monotonic
    at      TEXT NOT NULL,
    entry   TEXT NOT NULL,
    PRIMARY KEY (game_id, ord)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS imports (
    import_id   INTEGER PRIMARY KEY,
    game_id     TEXT NOT NULL,
    source      TEXT NOT NULL,          -- 'csv:<path>' | 'api' | 'extension'
    ingested_at TEXT NOT NULL,
    n_entries   INTEGER NOT NULL,       -- entries offered
    n_new       INTEGER NOT NULL        -- entries actually inserted (0 == pure re-import)
);

-- Which log-folder files each game has been read from or written to. This is what
-- lets deleting a CSV remove its game: a game is dropped once EVERY file on record
-- for it is gone. A game with no row here was imported from somewhere else, or
-- captured with PNT_SAVE_LOGS=0, and no file's absence says anything about it.
-- mtime_ns and size are how a sync tells a file it has already read from a new one.
CREATE TABLE IF NOT EXISTS log_files (
    path     TEXT PRIMARY KEY,   -- resolved, absolute
    game_id  TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_log_files_game ON log_files(game_id);

-- Lines the grammar did not recognize. Never silently dropped: an empty table is
-- a claim that the parse was total, and a non-empty one tells you exactly what to
-- teach the grammar next.
CREATE TABLE IF NOT EXISTS parse_misses (
    game_id TEXT NOT NULL,
    ord     INTEGER NOT NULL,
    entry   TEXT NOT NULL,
    reason  TEXT NOT NULL,
    PRIMARY KEY (game_id, ord)
) WITHOUT ROWID;

-- ------------------------------------------------------- identity (layer 2) --

-- A canonical person. Stats are always requested for one of these.
CREATE TABLE IF NOT EXISTS players (
    player_id  INTEGER PRIMARY KEY,
    alias      TEXT NOT NULL UNIQUE,
    notes      TEXT,
    created_at TEXT NOT NULL
);

-- Many PokerNow IDs -> one person. IDs are stable per browser/device and survive
-- quit/rejoin and renames within and across games (gpP9uUffpu appears as
-- "genericpoker" in one session and "500" in another), but the SAME human on a
-- second device gets a different ID -- which is what this table exists for.
-- Merging two people is one UPDATE here; because no stat is stored, nothing
-- needs recomputing afterwards.
CREATE TABLE IF NOT EXISTS player_identities (
    pn_id          TEXT PRIMARY KEY,
    player_id      INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    last_seen_name TEXT,
    last_seen_at   TEXT,
    first_seen_at  TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_identities_player ON player_identities(player_id);

-- ----------------------------------------------------- judgement (layer 2) --

-- The hands you have already looked at. A mark is a judgement you made, not a
-- fact any log holds, so -- alone among the layer-2 tables -- it is never thrown
-- away and a rebuild does not touch it. It is the same kind of row as an
-- identity merge or `players.notes`.
--
-- Keyed on (game_id, hand_number), the log's own name for a hand, and NOT on
-- hand_id. hand_id is a rowid the importer hands out, and `rebuild_game` deletes
-- every hand of a game before re-inserting it, so those ids move: a mark keyed
-- on one would come back after a rebuild silently pointing at a different hand.
-- The same reasoning rules out a foreign key to hands, whose ON DELETE CASCADE
-- would clear every mark at exactly the moment you most want them kept -- and
-- whose absence lets a mark outlive a game deleted and imported again later.
CREATE TABLE IF NOT EXISTS hand_reviews (
    game_id     TEXT NOT NULL,
    hand_number INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (game_id, hand_number)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- layer 2 ----

CREATE TABLE IF NOT EXISTS games (
    game_id    TEXT PRIMARY KEY,
    started_at TEXT,
    sb         INTEGER,
    bb         INTEGER,
    ante       INTEGER DEFAULT 0,
    variant    TEXT DEFAULT 'nlhe',
    hero_pn_id TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hands (
    hand_id          INTEGER PRIMARY KEY,
    game_id          TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    hand_number      INTEGER NOT NULL,
    table_hand_id    TEXT,
    ts               TEXT,
    ord              INTEGER NOT NULL,
    dealer_seat      INTEGER,          -- NULL on a dead button
    dead_button      INTEGER NOT NULL DEFAULT 0,
    blinds_irregular INTEGER NOT NULL DEFAULT 0,  -- a dead blind left a position slot empty
    n_dealt_in       INTEGER NOT NULL, -- stored RAW; never bucketed into "6-max" etc.
    bb               INTEGER,          -- this hand's actual BB post; blind levels move
    board_json       TEXT,             -- list of runs, so run-it-twice needs no new columns
    run_count        INTEGER NOT NULL DEFAULT 1,
    went_to_showdown INTEGER NOT NULL DEFAULT 0,
    UNIQUE (game_id, hand_number)
);

CREATE INDEX IF NOT EXISTS idx_hands_game ON hands(game_id);

-- Populated from the `Player stacks:` line -- the DEALT-IN roster -- never from
-- join/quit events and never from whoever happened to act. A player sitting out
-- is still at the table; getting this wrong silently deflates every rate for
-- exactly the players who sit out most.
CREATE TABLE IF NOT EXISTS hand_players (
    hand_id           INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    pn_id             TEXT NOT NULL,
    seat              INTEGER NOT NULL,
    seats_from_button INTEGER,   -- position is DERIVED from this at query time
    starting_stack    INTEGER,
    hole_cards        TEXT,      -- known at showdown, or for hero on every hand
    contributed       INTEGER NOT NULL DEFAULT 0,
    collected         INTEGER NOT NULL DEFAULT 0,
    -- Signed 7-2 side-bet result. Kept OUT of collected/contributed so those two
    -- continue to describe the pot alone and keep balancing against each other.
    bounty            INTEGER NOT NULL DEFAULT 0,
    folded            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hand_id, pn_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_hp_pn ON hand_players(pn_id);

CREATE TABLE IF NOT EXISTS actions (
    hand_id     INTEGER NOT NULL REFERENCES hands(hand_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,     -- monotonic within the hand
    street      TEXT NOT NULL,        -- preflop | flop | turn | river
    pn_id       TEXT NOT NULL,
    action_type TEXT NOT NULL,        -- post | fold | check | call | bet | raise
    amount      INTEGER NOT NULL DEFAULT 0,  -- INCREMENTAL chips (see parser.py)
    amount_to   INTEGER,              -- raw cumulative figure from the log, for audit
    is_forced   INTEGER NOT NULL DEFAULT 0,
    all_in      INTEGER NOT NULL DEFAULT 0,
    post_kind   TEXT,                 -- sb | bb | ante | missed_bb | missing_sb | straddle
    PRIMARY KEY (hand_id, seq)
) WITHOUT ROWID;

-- Supports "every time this player did X on this street" -- the filter queries.
CREATE INDEX IF NOT EXISTS idx_actions_pn ON actions(pn_id, street, action_type);
-- NOTE: no index on (hand_id, ...) is needed. This is a WITHOUT ROWID table whose
-- primary key is already (hand_id, seq), so rows for one hand are physically
-- contiguous and ordered. An extra index there is never chosen by the planner and
-- only costs write time on every rebuild.

-- Cards shown AFTER `-- ending hand #N --`: a player who folded and then showed,
-- a winner who took it down uncontested and showed, a showdown loser who mucked
-- and revealed later. PokerNow logs all of these past the hand-end line, in the
-- same slot as the 7-2 bounty.
--
-- Deliberately NOT merged into hand_players.hole_cards. Showdown cards are the
-- hands that got there; these are the hands a player CHOSE to reveal -- the bluff
-- they are proud of, the fold they want credit for -- a bias running the other
-- way. Kept apart, a range view can opt in; merged, that choice is gone for good.
--
-- One row per player per hand. A player may show one card and then the other as
-- two separate lines, so `cards` is the union, in the order shown.
CREATE TABLE IF NOT EXISTS voluntary_shows (
    hand_id INTEGER NOT NULL,
    pn_id   TEXT NOT NULL,
    cards   TEXT NOT NULL,     -- same encoding as hole_cards; may be one card
    ord     INTEGER NOT NULL,  -- raw_entries.ord of the latest show line, for audit
    PRIMARY KEY (hand_id, pn_id),
    -- Only a player dealt into the hand can show from it.
    FOREIGN KEY (hand_id, pn_id) REFERENCES hand_players(hand_id, pn_id) ON DELETE CASCADE
) WITHOUT ROWID;

-- ------------------------------------------------------- derived (layer 3) ---

-- Showdown equities, memoized. DERIVED and DISPOSABLE: drop the table and the
-- next all-in EV request refills it. It is the one exception to "nothing derived
-- is stored", and it is safe because the key is the cards and the pot layout
-- alone -- never a hand_id or an identity -- so no rebuild, merge or rename can
-- make a row wrong. A preflop all-in costs half a second of sampling; without
-- this the page would pay that for every hand on every request.
CREATE TABLE IF NOT EXISTS equity_cache (
    key         TEXT PRIMARY KEY,   -- hole cards | board | pot layout (equity.cache_key)
    expected    TEXT NOT NULL,      -- JSON list: expected chips collected, per player
    method      TEXT NOT NULL,      -- exact | sampled
    n           INTEGER NOT NULL,   -- boards enumerated, or deals sampled
    computed_at TEXT NOT NULL
) WITHOUT ROWID;

-- ------------------------------------------------------------------ views ----

-- Resolves the identity layer once so every stat query can join on player_id
-- without repeating the alias plumbing.
CREATE VIEW IF NOT EXISTS v_hand_players AS
SELECT
    hp.*,
    h.game_id,
    h.hand_number,
    h.n_dealt_in,
    h.dead_button,
    h.went_to_showdown,
    h.ts,
    pi.player_id,
    p.alias
FROM hand_players hp
JOIN hands   h  ON h.hand_id = hp.hand_id
LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id
LEFT JOIN players p            ON p.player_id = pi.player_id;

CREATE VIEW IF NOT EXISTS v_actions AS
SELECT
    a.*,
    h.game_id,
    h.hand_number,
    h.n_dealt_in,
    pi.player_id,
    p.alias
FROM actions a
JOIN hands   h  ON h.hand_id = a.hand_id
LEFT JOIN player_identities pi ON pi.pn_id = a.pn_id
LEFT JOIN players p            ON p.player_id = pi.player_id;

-- A voluntary show with the context that makes it readable: did the player fold,
-- did anyone reach showdown, how far the board ran. "Folded, then showed, before
-- the river" is `folded = 1 AND board_cards < 5`.
CREATE VIEW IF NOT EXISTS v_voluntary_shows AS
SELECT
    vs.*,
    length(vs.cards) / 2 AS n_cards,
    hp.folded,
    hp.seats_from_button,
    hp.contributed,
    hp.collected,
    h.game_id,
    h.hand_number,
    h.n_dealt_in,
    h.went_to_showdown,
    COALESCE(json_array_length(h.board_json, '$[0]'), 0) AS board_cards,  -- first run
    h.board_json,
    h.ts,
    pi.player_id,
    p.alias
FROM voluntary_shows vs
JOIN hand_players hp ON hp.hand_id = vs.hand_id AND hp.pn_id = vs.pn_id
JOIN hands        h  ON h.hand_id = vs.hand_id
LEFT JOIN player_identities pi ON pi.pn_id = vs.pn_id
LEFT JOIN players p            ON p.player_id = pi.player_id;

-- Bumped whenever something makes previously derived facts stale: a rebuild, or a
-- change to who an identity belongs to. Read-side caches key on it, so they can
-- hold a result for as long as it is still true and never longer.
--
-- Note what does NOT bump it: appending raw entries. Those change nothing derived
-- until a rebuild reads them, so live capture between hands leaves caches warm.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
