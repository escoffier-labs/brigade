CREATE TABLE conversations (
  fts_rowid INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  scope TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  is_archived INTEGER NOT NULL,
  root_fingerprint TEXT,
  cache_fingerprint TEXT
);
CREATE VIRTUAL TABLE conversation_fts USING fts5(title, body);
INSERT INTO conversations VALUES
  (1, 'local', '', 'cursor-fixture-001', 'Synthetic migration plan', 1784116800000, 0, 'fixture-root-a', NULL),
  (2, 'local', '', 'cursor-fixture-002', 'Synthetic crawler review', 1784120400000, 1, 'fixture-root-b', NULL);
INSERT INTO conversation_fts(rowid, title, body) VALUES
  (1, 'Synthetic migration plan', 'Write the synthetic migration checklist for demo-project.'),
  (2, 'Synthetic crawler review', 'Confirm Cursor conversation search ingestion without private data.');
