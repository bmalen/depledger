import { Database } from "bun:sqlite";
import { homedir } from "node:os";
import { join } from "node:path";

/** Same rule as store.py: $DEPLEDGER_DB, else $DEPLEDGER_HOME, else $XDG_DATA_HOME/depledger. */
const home = process.env.DEPLEDGER_HOME ?? join(process.env.XDG_DATA_HOME ?? join(homedir(), ".local", "share"), "depledger");
const path = process.env.DEPLEDGER_DB ?? join(home, "depledger.db");

export const db = new Database(path, { readwrite: true, create: false });
db.exec("PRAGMA busy_timeout = 5000");
db.exec("PRAGMA query_only = ON");

type Row = Record<string, any>;

const all = (sql: string, ...args: any[]): Row[] => db.query(sql).all(...args) as Row[];
const get = (sql: string, ...args: any[]): Row | null => (db.query(sql).get(...args) as Row) ?? null;

function globToRegExp(glob: string): RegExp {
  const escaped = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`);
}

/** Tags from a comma list, parentheticals dropped first (same rule as store.split_tags). */
export function splitTags(text: string | null): string[] {
  return (text ?? "")
    .replace(/\s*\([^)]*\)/g, "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

const HISTORY_SQL = `
  SELECT b.kind, b.ecosystem, b.name, b.from_version, b.to_version, b.bump_type, b.section,
         p.id AS pr_id, p.number, p.title AS pr_title, p.state, p.url, p.triaged_at, p.opened_at,
         r.full_name AS repo, r.id AS repo_id,
         v.id AS review_id, v.verdict, v.verdict_text, v.tier, v.reviewed_at
  FROM pr_bumps b
  JOIN prs p ON p.id = b.pr_id
  JOIN repos r ON r.id = p.repo_id
  LEFT JOIN review_prs rp ON rp.pr_id = p.id
  LEFT JOIN reviews v ON v.id = rp.review_id`;

/** Every PR that moved a name matching one of the patterns, newest first, one row per PR and name. */
export function bumpHistory(patterns: string[]): Row[] {
  if (!patterns.length) return [];
  const where = patterns.map(() => "b.name GLOB ?").join(" OR ");
  const rows = all(
    `${HISTORY_SQL} WHERE ${where} ORDER BY coalesce(v.reviewed_at, p.triaged_at, p.opened_at) DESC, v.id DESC`,
    ...patterns,
  );
  const seen = new Set<string>();
  return rows.filter((r) => {
    const key = `${r.pr_id}:${r.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function summary() {
  const count = (sql: string) => (get(sql)?.n as number) ?? 0;
  return {
    counts: {
      repos: count("SELECT count(*) AS n FROM repos"),
      packages: count("SELECT count(*) AS n FROM packages"),
      traps: count("SELECT count(*) AS n FROM packages WHERE status = 'TRAP'"),
      checks: count("SELECT count(*) AS n FROM packages WHERE status = 'CHECK'"),
      reviews: count("SELECT count(*) AS n FROM reviews"),
      safe: count("SELECT count(*) AS n FROM reviews WHERE verdict = 'safe'"),
      notSafe: count("SELECT count(*) AS n FROM reviews WHERE verdict = 'not_safe'"),
      notes: count("SELECT count(*) AS n FROM notes"),
      digests: count("SELECT count(*) AS n FROM image_digests"),
      actions: count("SELECT count(*) AS n FROM action_shas"),
    },
    recent: listReviews({ limit: 12 }),
    trapPackages: all(
      `SELECT p.id, p.ecosystem, p.name, p.analysed, p.gist FROM packages p WHERE p.status = 'TRAP' ORDER BY p.ecosystem, p.position`,
    ),
    perMonth: all(
      `SELECT substr(v.reviewed_at, 1, 7) AS month, r.full_name AS repo, count(*) AS n
       FROM reviews v JOIN repos r ON r.id = v.repo_id GROUP BY 1, 2 ORDER BY 1`,
    ),
  };
}

export function listRepos(): Row[] {
  return all(`
    SELECT r.*,
      (SELECT count(*) FROM reviews v WHERE v.repo_id = r.id) AS review_count,
      (SELECT count(*) FROM reviews v WHERE v.repo_id = r.id AND v.verdict = 'not_safe') AS not_safe_count,
      (SELECT max(reviewed_at) FROM reviews v WHERE v.repo_id = r.id) AS last_reviewed,
      (SELECT count(*) FROM notes n WHERE n.repo_id = r.id) AS note_count
    FROM repos r ORDER BY r.full_name`);
}

export function repoDetail(fullName: string) {
  const repo = get("SELECT * FROM repos WHERE full_name = ?", fullName);
  if (!repo) return null;
  return {
    repo,
    notes: all("SELECT * FROM notes WHERE repo_id = ? ORDER BY position, id", repo.id),
    reviews: listReviews({ repoId: repo.id, limit: 500 }),
  };
}

export function noteDetail(fullName: string, ref: string) {
  const repo = get("SELECT * FROM repos WHERE full_name = ?", fullName);
  if (!repo) return null;
  const note = /^\d+$/.test(ref)
    ? get("SELECT * FROM notes WHERE repo_id = ? AND id = ?", repo.id, Number(ref))
    : get("SELECT * FROM notes WHERE repo_id = ? AND slug = ?", repo.id, ref);
  if (!note) return null;
  return { repo, note, revisions: revisions("notes", note.id) };
}

export function listPackages(): Row[] {
  return all(`
    SELECT p.*, d.slug AS doc_slug,
      (SELECT count(DISTINCT pr.repo_id) FROM package_names n JOIN pr_bumps b ON b.name GLOB n.pattern
         JOIN prs pr ON pr.id = b.pr_id WHERE n.package_id = p.id) AS repo_count,
      (SELECT group_concat(pattern, ', ') FROM package_names n WHERE n.package_id = p.id) AS names
    FROM packages p LEFT JOIN docs d ON d.id = p.doc_id
    ORDER BY p.ecosystem, p.position, p.id`);
}

export function packageDetail(id: number) {
  const pkg = get("SELECT p.*, d.slug AS doc_slug FROM packages p LEFT JOIN docs d ON d.id = p.doc_id WHERE p.id = ?", id);
  if (!pkg) return null;
  const names = all("SELECT pattern FROM package_names WHERE package_id = ? ORDER BY id", id).map((r) => r.pattern as string);
  const doc = pkg.doc_id ? get("SELECT * FROM docs WHERE id = ?", pkg.doc_id) : null;
  const lowered = names.map((n) => n.toLowerCase());
  const notes = all(
    `SELECT n.*, r.full_name AS repo FROM notes n JOIN repos r ON r.id = n.repo_id WHERE n.when_tags != '' ORDER BY r.full_name, n.position`,
  ).filter((n) =>
    splitTags(n.when_tags).some((tag) => {
      const t = tag.toLowerCase();
      return lowered.some((x) => x === t || (t.includes("*") && globToRegExp(t).test(x)) || (x.includes("*") && globToRegExp(x).test(t)));
    }),
  );
  return {
    package: pkg,
    names,
    doc,
    checks: doc ? all("SELECT * FROM exposure_checks WHERE doc_id = ? ORDER BY position, id", doc.id) : [],
    sharedWith: doc ? all("SELECT id, name, status FROM packages WHERE doc_id = ? AND id != ?", doc.id, id) : [],
    history: bumpHistory(names),
    notes,
    revisions: revisions("packages", id).concat(doc ? revisions("docs", doc.id) : []),
  };
}

export function docDetail(slug: string) {
  const doc = get("SELECT * FROM docs WHERE slug = ?", slug);
  if (!doc) return null;
  return {
    doc,
    checks: all("SELECT * FROM exposure_checks WHERE doc_id = ? ORDER BY position, id", doc.id),
    packages: all("SELECT id, ecosystem, name, status, analysed FROM packages WHERE doc_id = ? ORDER BY position", doc.id),
    traps: all("SELECT * FROM traps WHERE doc_id = ? ORDER BY position", doc.id),
    revisions: revisions("docs", doc.id),
  };
}

export function patterns() {
  return {
    traps: all("SELECT t.*, d.slug AS doc_slug FROM traps t LEFT JOIN docs d ON d.id = t.doc_id ORDER BY t.position, t.id"),
    docs: all("SELECT id, slug, title, updated_at FROM docs WHERE kind = 'pattern' ORDER BY slug"),
  };
}

export function listReviews(opts: { repoId?: number; verdict?: string; limit?: number } = {}): Row[] {
  const where: string[] = [];
  const args: any[] = [];
  if (opts.repoId) {
    where.push("v.repo_id = ?");
    args.push(opts.repoId);
  }
  if (opts.verdict === "none") where.push("v.verdict IS NULL");
  else if (opts.verdict) {
    where.push("v.verdict = ?");
    args.push(opts.verdict);
  }
  return all(
    `SELECT v.id, v.reviewed_at, v.verdict, v.verdict_text, v.tier, v.title, v.head_sha, v.source,
            r.full_name AS repo, p.number, p.state, p.url
     FROM reviews v JOIN repos r ON r.id = v.repo_id
     LEFT JOIN review_prs rp ON rp.review_id = v.id AND rp.is_primary = 1
     LEFT JOIN prs p ON p.id = rp.pr_id
     ${where.length ? "WHERE " + where.join(" AND ") : ""}
     ORDER BY v.reviewed_at DESC, v.id DESC LIMIT ?`,
    ...args,
    opts.limit ?? 1000,
  );
}

export function reviewDetail(id: number) {
  const review = get("SELECT v.*, r.full_name AS repo FROM reviews v JOIN repos r ON r.id = v.repo_id WHERE v.id = ?", id);
  if (!review) return null;
  const prs = all(
    `SELECT p.*, r.full_name AS repo, rp.is_primary FROM review_prs rp JOIN prs p ON p.id = rp.pr_id
     JOIN repos r ON r.id = p.repo_id WHERE rp.review_id = ? ORDER BY rp.is_primary DESC, p.number`,
    id,
  ).map((pr) => ({ ...pr, bumps: bumpsFor(pr.id) }));
  return { review, prs, revisions: revisions("reviews", id) };
}

export function prDetail(fullName: string, number: number) {
  const pr = get(
    "SELECT p.*, r.full_name AS repo FROM prs p JOIN repos r ON r.id = p.repo_id WHERE r.full_name = ? AND p.number = ?",
    fullName,
    number,
  );
  if (!pr) return null;
  return {
    pr,
    bumps: bumpsFor(pr.id),
    reviews: all(
      "SELECT v.* FROM reviews v JOIN review_prs rp ON rp.review_id = v.id WHERE rp.pr_id = ? ORDER BY v.reviewed_at, v.id",
      pr.id,
    ),
  };
}

function bumpsFor(prId: number): Row[] {
  const bumps = all("SELECT * FROM pr_bumps WHERE pr_id = ? ORDER BY kind, name", prId);
  const index = all(
    "SELECT n.pattern, p.id, p.status FROM package_names n JOIN packages p ON p.id = n.package_id ORDER BY p.position, p.id, n.id",
  );
  return bumps.map((b) => {
    const hit =
      index.find((r) => r.pattern === b.name) ?? index.find((r) => r.pattern.includes("*") && globToRegExp(r.pattern).test(b.name));
    return { ...b, package_id: hit?.id ?? null, package_status: hit?.status ?? null };
  });
}

export function images() {
  return {
    digests: all("SELECT * FROM image_digests ORDER BY image, position"),
    notes: get("SELECT slug, title FROM docs WHERE slug = 'image-notes'"),
  };
}

export function actions() {
  return {
    shas: all("SELECT * FROM action_shas ORDER BY action, position"),
    notes: get("SELECT slug, title FROM docs WHERE slug = 'action-notes'"),
  };
}

function ftsQuery(text: string): string {
  return text
    .split(/\s+/)
    .filter((w) => w.length >= 3)
    .map((w) => `"${w.replace(/"/g, '""')}"`)
    .join(" ");
}

export function search(text: string, kind?: string): Row[] {
  const q = ftsQuery(text);
  if (!q) return [];
  const rows = all(
    `SELECT kind, ref_id, repo, title, snippet(search, -1, char(1), char(2), '...', 64) AS snippet
     FROM search WHERE search MATCH ? ${kind ? "AND kind = ?" : ""} ORDER BY rank LIMIT 80`,
    ...(kind ? [q, kind] : [q]),
  );
  return rows.map((r) => {
    if (r.kind === "doc") return { ...r, slug: get("SELECT slug FROM docs WHERE id = ?", r.ref_id)?.slug };
    if (r.kind === "note") return { ...r, slug: get("SELECT slug FROM notes WHERE id = ?", r.ref_id)?.slug };
    if (r.kind === "trap") return { ...r, slug: get("SELECT d.slug FROM traps t LEFT JOIN docs d ON d.id = t.doc_id WHERE t.id = ?", r.ref_id)?.slug };
    return r;
  });
}

function revisions(table: string, rowId: number): Row[] {
  return all("SELECT * FROM revisions WHERE table_name = ? AND row_id = ? ORDER BY id DESC", table, rowId).map((r) => ({
    ...r,
    snapshot: JSON.parse(r.snapshot),
  }));
}

/** Registered repo names, for the open PR lookup. */
export function repoNames(): string[] {
  return all("SELECT full_name FROM repos ORDER BY full_name").map((r) => r.full_name as string);
}

export function prStatus(fullName: string, number: number) {
  return get(
    `SELECT p.id, p.head_sha, p.triaged_at, p.proposed_tier,
       (SELECT v.verdict FROM reviews v JOIN review_prs rp ON rp.review_id = v.id WHERE rp.pr_id = p.id ORDER BY v.reviewed_at DESC, v.id DESC LIMIT 1) AS verdict,
       (SELECT v.head_sha FROM reviews v JOIN review_prs rp ON rp.review_id = v.id WHERE rp.pr_id = p.id ORDER BY v.reviewed_at DESC, v.id DESC LIMIT 1) AS reviewed_head,
       (SELECT v.id FROM reviews v JOIN review_prs rp ON rp.review_id = v.id WHERE rp.pr_id = p.id ORDER BY v.reviewed_at DESC, v.id DESC LIMIT 1) AS review_id
     FROM prs p JOIN repos r ON r.id = p.repo_id WHERE r.full_name = ? AND p.number = ?`,
    fullName,
    number,
  );
}
