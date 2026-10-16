import index from "./index.html";
import * as ledger from "./db";

const port = Number(process.env.DEPLEDGER_UI_PORT ?? 7420);
const BOTS = new Set(["app/renovate", "renovate", "renovate[bot]", "app/dependabot", "dependabot", "dependabot[bot]"]);
const OPEN_PRS_TTL = 5 * 60 * 1000;

const json = (data: unknown, status = 200) => Response.json(data, { status });
const notFound = () => json({ error: "not found" }, 404);

let openPrsCache: { at: number; data: unknown } | null = null;

/** Open dependency PRs across every registered repo, from gh, next to what the ledger knows. */
async function openPrs(force: boolean) {
  if (!force && openPrsCache && Date.now() - openPrsCache.at < OPEN_PRS_TTL) return openPrsCache.data;
  const repos = ledger.repoNames();
  const results = await Promise.all(
    repos.map(async (repo) => {
      const proc = Bun.spawn(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "60", "--json", "number,title,url,author,createdAt,headRefName,headRefOid"],
        { stdout: "pipe", stderr: "pipe" },
      );
      const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
      if (code !== 0) return { repo, error: err.trim().slice(0, 200), prs: [] };
      const prs = (JSON.parse(out) as any[])
        .filter((p) => BOTS.has(p.author?.login) || p.author?.is_bot || /^(renovate|dependabot)\//.test(p.headRefName))
        .map((p) => {
          const known = ledger.prStatus(repo, p.number);
          return {
            repo,
            number: p.number,
            title: p.title,
            url: p.url,
            createdAt: p.createdAt,
            branch: p.headRefName,
            head: p.headRefOid,
            triaged: Boolean(known?.triaged_at),
            verdict: known?.verdict ?? null,
            reviewId: known?.review_id ?? null,
            reviewedHead: known?.reviewed_head ?? null,
            stale: Boolean(known?.review_id && known?.reviewed_head && known.reviewed_head !== p.headRefOid),
          };
        });
      return { repo, error: null, prs };
    }),
  );
  const data = {
    fetchedAt: new Date().toISOString(),
    errors: results.filter((r) => r.error).map((r) => ({ repo: r.repo, error: r.error })),
    prs: results.flatMap((r) => r.prs).sort((a, b) => a.createdAt.localeCompare(b.createdAt)),
  };
  openPrsCache = { at: Date.now(), data };
  return data;
}

const orNotFound = (data: unknown) => (data ? json(data) : notFound());

const server = Bun.serve({
  hostname: "127.0.0.1",
  port,
  development: process.env.NODE_ENV !== "production",
  routes: {
    "/api/health": () => json({ ok: true }),
    "/api/summary": () => json(ledger.summary()),
    "/api/open-prs": async (req) => json(await openPrs(new URL(req.url).searchParams.has("refresh"))),
    "/api/repos": () => json(ledger.listRepos()),
    "/api/repos/:owner/:name": (req) => orNotFound(ledger.repoDetail(`${req.params.owner}/${req.params.name}`)),
    "/api/repos/:owner/:name/notes/:ref": (req) =>
      orNotFound(ledger.noteDetail(`${req.params.owner}/${req.params.name}`, req.params.ref)),
    "/api/repos/:owner/:name/prs/:number": (req) =>
      orNotFound(ledger.prDetail(`${req.params.owner}/${req.params.name}`, Number(req.params.number))),
    "/api/packages": () => json(ledger.listPackages()),
    "/api/packages/:id": (req) => orNotFound(ledger.packageDetail(Number(req.params.id))),
    "/api/docs/:slug": (req) => orNotFound(ledger.docDetail(req.params.slug)),
    "/api/patterns": () => json(ledger.patterns()),
    "/api/reviews": (req) => {
      const q = new URL(req.url).searchParams;
      return json(ledger.listReviews({ verdict: q.get("verdict") ?? undefined }));
    },
    "/api/reviews/:id": (req) => orNotFound(ledger.reviewDetail(Number(req.params.id))),
    "/api/images": () => json(ledger.images()),
    "/api/actions": () => json(ledger.actions()),
    "/api/search": (req) => {
      const q = new URL(req.url).searchParams;
      return json(ledger.search(q.get("q") ?? "", q.get("kind") ?? undefined));
    },
    "/api/*": () => notFound(),
    "/*": index,
  },
});

console.log(`depledger UI on ${server.url}`);
