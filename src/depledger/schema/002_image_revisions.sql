-- Image records become editable (`depledger image edit`), so they keep history like every other
-- prose-bearing row.
ALTER TABLE image_digests ADD COLUMN updated_at TEXT;

CREATE TRIGGER image_digests_revision_update AFTER UPDATE ON image_digests
WHEN OLD.facts IS NOT NEW.facts OR OLD.notes IS NOT NEW.notes OR OLD.line IS NOT NEW.line OR OLD.tag IS NOT NEW.tag
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('image_digests', OLD.id, 'update', json_object('image', OLD.image, 'tag', OLD.tag, 'digest', OLD.digest, 'line', OLD.line, 'facts', OLD.facts, 'notes', OLD.notes, 'updated_at', OLD.updated_at));
END;
CREATE TRIGGER image_digests_revision_delete AFTER DELETE ON image_digests
BEGIN
    INSERT INTO revisions (table_name, row_id, action, snapshot)
    VALUES ('image_digests', OLD.id, 'delete', json_object('image', OLD.image, 'tag', OLD.tag, 'digest', OLD.digest, 'line', OLD.line, 'facts', OLD.facts, 'notes', OLD.notes, 'updated_at', OLD.updated_at));
END;
