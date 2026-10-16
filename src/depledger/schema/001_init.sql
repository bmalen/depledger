-- depledger: a shared ledger of dependency-update reviews. Applied by store.migrate(), which wraps each file in a
-- transaction and bumps PRAGMA user_version to the file's number.

CREATE TABLE repos (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    local_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Long-form markdown shared by several rows: package analyses, tooling patterns, image and
-- action notes. One doc can back several package rows (postcss and postcss-nesting).
CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('package', 'pattern', 'image', 'action')),
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- The commands that decide whether a repo is exposed. Banked once, re-run every review.
CREATE TABLE exposure_checks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    commands TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE packages (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL CHECK (ecosystem IN ('npm', 'composer', 'actions', 'images', 'cargo', 'other')),
    name TEXT NOT NULL,
    analysed TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('routine', 'CHECK', 'TRAP')),
    gist TEXT NOT NULL DEFAULT '',
    doc_id INTEGER REFERENCES docs(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (ecosystem, name)
);

-- Every name a package row answers to: exact names, or globs such as `@tiptap/*`.
CREATE TABLE package_names (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    pattern TEXT NOT NULL,
    UNIQUE (package_id, pattern)
);
CREATE INDEX package_names_pattern ON package_names (pattern);

-- Symptom-keyed one-liners printed by every triage run.
CREATE TABLE traps (
    id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 0,
    headline TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    doc_id INTEGER REFERENCES docs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Standing facts about one repo. No tags: printed on every triage of that repo. Tagged: printed
-- only when a tag matches the PR (a bucket, a package name or glob, or text in the PR's title,
-- branch or signals).
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    slug TEXT,
    gist TEXT NOT NULL,
    body TEXT,
    when_tags TEXT NOT NULL DEFAULT '',
    unverified INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (repo_id, slug)
);

CREATE TABLE prs (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    branch TEXT,
    base TEXT,
    url TEXT,
    author TEXT,
    state TEXT,
    head_sha TEXT,
    opened_at TEXT,
    merged_at TEXT,
    triaged_at TEXT,
    proposed_tier INTEGER,
    synced_at TEXT,
    UNIQUE (repo_id, number)
);

-- What a PR moves: manifest entries, lock-only transitives, action pins and image digests.
CREATE TABLE pr_bumps (
    id INTEGER PRIMARY KEY,
    pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('manifest', 'lock', 'action', 'image')),
    ecosystem TEXT NOT NULL,
    name TEXT NOT NULL,
    section TEXT,
    from_version TEXT,
    to_version TEXT,
    bump_type TEXT,
    UNIQUE (pr_id, kind, name)
);
CREATE INDEX pr_bumps_name ON pr_bumps (name);

-- One review event. It can cover several PRs (a pin PR reviewed with its sibling), and a PR can
-- have several reviews (a second verdict after a fix lands at a new head).
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    reviewed_at TEXT NOT NULL,
    head_sha TEXT,
    verdict TEXT CHECK (verdict IN ('safe', 'not_safe')),
    verdict_text TEXT,
    tier INTEGER CHECK (tier IN (0, 1, 2)),
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'skill',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE review_prs (
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (review_id, pr_id)
);
CREATE INDEX review_prs_pr ON review_prs (pr_id);

CREATE TABLE image_digests (
    id INTEGER PRIMARY KEY,
    image TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    digest12 TEXT NOT NULL,
    line TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX image_digests_digest12 ON image_digests (digest12);

CREATE TABLE action_shas (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    tag TEXT NOT NULL,
    sha TEXT NOT NULL,
    tag_type TEXT NOT NULL DEFAULT '' CHECK (tag_type IN ('', 'commit', 'annotated')),
    annotated_sha TEXT,
    note TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (action, tag)
);
CREATE INDEX action_shas_sha ON action_shas (sha);

-- History: the old row as JSON whenever a prose-bearing row changes or is deleted.
CREATE TABLE revisions (
    id INTEGER PRIMARY KEY,
    table_name TEXT NOT NULL,
    row_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('update', 'delete')),
    snapshot TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX revisions_row ON revisions (table_name, row_id);

CREATE TRIGGER docs_revision_update AFTER UPDATE ON docs
WHEN OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body OR OLD.slug IS NOT NEW.slug
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('docs', OLD.id, 'update', json_object('slug', OLD.slug, 'kind', OLD.kind, 'title', OLD.title, 'body', OLD.body, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER docs_revision_delete AFTER DELETE ON docs
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('docs', OLD.id, 'delete', json_object('slug', OLD.slug, 'kind', OLD.kind, 'title', OLD.title, 'body', OLD.body, 'updated_at', OLD.updated_at));
END;

CREATE TRIGGER exposure_checks_revision_update AFTER UPDATE ON exposure_checks
WHEN OLD.commands IS NOT NEW.commands OR OLD.label IS NOT NEW.label
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('exposure_checks', OLD.id, 'update', json_object('doc_id', OLD.doc_id, 'label', OLD.label, 'commands', OLD.commands, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER exposure_checks_revision_delete AFTER DELETE ON exposure_checks
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('exposure_checks', OLD.id, 'delete', json_object('doc_id', OLD.doc_id, 'label', OLD.label, 'commands', OLD.commands, 'updated_at', OLD.updated_at));
END;

CREATE TRIGGER packages_revision_update AFTER UPDATE ON packages
WHEN OLD.analysed IS NOT NEW.analysed OR OLD.status IS NOT NEW.status OR OLD.gist IS NOT NEW.gist
    OR OLD.doc_id IS NOT NEW.doc_id OR OLD.name IS NOT NEW.name
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('packages', OLD.id, 'update', json_object('ecosystem', OLD.ecosystem, 'name', OLD.name, 'analysed', OLD.analysed, 'status', OLD.status, 'gist', OLD.gist, 'doc_id', OLD.doc_id, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER packages_revision_delete AFTER DELETE ON packages
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('packages', OLD.id, 'delete', json_object('ecosystem', OLD.ecosystem, 'name', OLD.name, 'analysed', OLD.analysed, 'status', OLD.status, 'gist', OLD.gist, 'doc_id', OLD.doc_id, 'updated_at', OLD.updated_at));
END;

CREATE TRIGGER traps_revision_update AFTER UPDATE ON traps
WHEN OLD.headline IS NOT NEW.headline OR OLD.detail IS NOT NEW.detail OR OLD.doc_id IS NOT NEW.doc_id
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('traps', OLD.id, 'update', json_object('headline', OLD.headline, 'detail', OLD.detail, 'doc_id', OLD.doc_id, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER traps_revision_delete AFTER DELETE ON traps
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('traps', OLD.id, 'delete', json_object('headline', OLD.headline, 'detail', OLD.detail, 'doc_id', OLD.doc_id, 'updated_at', OLD.updated_at));
END;

CREATE TRIGGER notes_revision_update AFTER UPDATE ON notes
WHEN OLD.gist IS NOT NEW.gist OR OLD.body IS NOT NEW.body OR OLD.when_tags IS NOT NEW.when_tags
    OR OLD.slug IS NOT NEW.slug OR OLD.unverified IS NOT NEW.unverified
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('notes', OLD.id, 'update', json_object('repo_id', OLD.repo_id, 'slug', OLD.slug, 'gist', OLD.gist, 'body', OLD.body, 'when_tags', OLD.when_tags, 'unverified', OLD.unverified, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER notes_revision_delete AFTER DELETE ON notes
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('notes', OLD.id, 'delete', json_object('repo_id', OLD.repo_id, 'slug', OLD.slug, 'gist', OLD.gist, 'body', OLD.body, 'when_tags', OLD.when_tags, 'unverified', OLD.unverified, 'updated_at', OLD.updated_at));
END;

CREATE TRIGGER reviews_revision_update AFTER UPDATE ON reviews
WHEN OLD.verdict IS NOT NEW.verdict OR OLD.verdict_text IS NOT NEW.verdict_text OR OLD.tier IS NOT NEW.tier
    OR OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('reviews', OLD.id, 'update', json_object('repo_id', OLD.repo_id, 'reviewed_at', OLD.reviewed_at, 'head_sha', OLD.head_sha, 'verdict', OLD.verdict, 'verdict_text', OLD.verdict_text, 'tier', OLD.tier, 'title', OLD.title, 'body', OLD.body, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER reviews_revision_delete AFTER DELETE ON reviews
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('reviews', OLD.id, 'delete', json_object('repo_id', OLD.repo_id, 'reviewed_at', OLD.reviewed_at, 'head_sha', OLD.head_sha, 'verdict', OLD.verdict, 'verdict_text', OLD.verdict_text, 'tier', OLD.tier, 'title', OLD.title, 'body', OLD.body, 'updated_at', OLD.updated_at));
END;

-- Full text search over every piece of prose. Trigram so that `@tiptap/core`, `prosemirror-view`
-- and a 12 character digest prefix all match as substrings.
CREATE VIRTUAL TABLE search USING fts5 (
    kind UNINDEXED,
    ref_id UNINDEXED,
    repo UNINDEXED,
    title,
    body,
    tokenize = 'trigram'
);

CREATE TRIGGER docs_search_insert AFTER INSERT ON docs BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('doc', NEW.id, NULL, NEW.title, NEW.body);
END;
CREATE TRIGGER docs_search_update AFTER UPDATE ON docs BEGIN
    DELETE FROM search WHERE kind = 'doc' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('doc', NEW.id, NULL, NEW.title, NEW.body);
END;
CREATE TRIGGER docs_search_delete AFTER DELETE ON docs BEGIN
    DELETE FROM search WHERE kind = 'doc' AND ref_id = OLD.id;
END;

CREATE TRIGGER packages_search_insert AFTER INSERT ON packages BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('package', NEW.id, NULL, NEW.name, NEW.gist);
END;
CREATE TRIGGER packages_search_update AFTER UPDATE ON packages BEGIN
    DELETE FROM search WHERE kind = 'package' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('package', NEW.id, NULL, NEW.name, NEW.gist);
END;
CREATE TRIGGER packages_search_delete AFTER DELETE ON packages BEGIN
    DELETE FROM search WHERE kind = 'package' AND ref_id = OLD.id;
END;

CREATE TRIGGER traps_search_insert AFTER INSERT ON traps BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('trap', NEW.id, NULL, NEW.headline, NEW.detail);
END;
CREATE TRIGGER traps_search_update AFTER UPDATE ON traps BEGIN
    DELETE FROM search WHERE kind = 'trap' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body) VALUES ('trap', NEW.id, NULL, NEW.headline, NEW.detail);
END;
CREATE TRIGGER traps_search_delete AFTER DELETE ON traps BEGIN
    DELETE FROM search WHERE kind = 'trap' AND ref_id = OLD.id;
END;

CREATE TRIGGER notes_search_insert AFTER INSERT ON notes BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('note', NEW.id, (SELECT full_name FROM repos WHERE id = NEW.repo_id), NEW.gist, coalesce(NEW.body, ''));
END;
CREATE TRIGGER notes_search_update AFTER UPDATE ON notes BEGIN
    DELETE FROM search WHERE kind = 'note' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('note', NEW.id, (SELECT full_name FROM repos WHERE id = NEW.repo_id), NEW.gist, coalesce(NEW.body, ''));
END;
CREATE TRIGGER notes_search_delete AFTER DELETE ON notes BEGIN
    DELETE FROM search WHERE kind = 'note' AND ref_id = OLD.id;
END;

CREATE TRIGGER reviews_search_insert AFTER INSERT ON reviews BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('review', NEW.id, (SELECT full_name FROM repos WHERE id = NEW.repo_id), NEW.title, NEW.body);
END;
CREATE TRIGGER reviews_search_update AFTER UPDATE ON reviews BEGIN
    DELETE FROM search WHERE kind = 'review' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('review', NEW.id, (SELECT full_name FROM repos WHERE id = NEW.repo_id), NEW.title, NEW.body);
END;
CREATE TRIGGER reviews_search_delete AFTER DELETE ON reviews BEGIN
    DELETE FROM search WHERE kind = 'review' AND ref_id = OLD.id;
END;

CREATE TRIGGER image_digests_search_insert AFTER INSERT ON image_digests BEGIN
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('image', NEW.id, NULL, NEW.image || ' ' || NEW.tag || ' ' || NEW.digest, NEW.notes || ' ' || NEW.facts);
END;
CREATE TRIGGER image_digests_search_update AFTER UPDATE ON image_digests BEGIN
    DELETE FROM search WHERE kind = 'image' AND ref_id = OLD.id;
    INSERT INTO search (kind, ref_id, repo, title, body)
    VALUES ('image', NEW.id, NULL, NEW.image || ' ' || NEW.tag || ' ' || NEW.digest, NEW.notes || ' ' || NEW.facts);
END;
CREATE TRIGGER image_digests_search_delete AFTER DELETE ON image_digests BEGIN
    DELETE FROM search WHERE kind = 'image' AND ref_id = OLD.id;
END;
