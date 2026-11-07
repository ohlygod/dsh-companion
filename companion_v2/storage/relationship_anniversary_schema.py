"""Auto-created relationship anniversaries discovered during daily reflection."""

RELATIONSHIP_ANNIVERSARY_STATEMENTS = (
    """CREATE TABLE relationship_anniversaries (
        anniversary_id TEXT PRIMARY KEY NOT NULL,
        owner_key TEXT NOT NULL,
        role_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        label TEXT NOT NULL,
        month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
        day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31),
        timezone TEXT NOT NULL,
        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
        evidence_json TEXT NOT NULL,
        source_run_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active', 'retracted')),
        created_at TEXT NOT NULL,
        UNIQUE(owner_key, month, day, label)
    )""",
    "CREATE INDEX relationship_anniversary_active_idx ON relationship_anniversaries(owner_key, state, month, day)",
    "CREATE UNIQUE INDEX relationship_anniversary_active_day_idx "
    "ON relationship_anniversaries(owner_key, month, day) WHERE state = 'active'",
)

RELATIONSHIP_ANNIVERSARY_TABLES = frozenset({"relationship_anniversaries"})
