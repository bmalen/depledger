import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { ago, day, Empty, Loading, Page, prPath, RepoName, Section, Status, Table, td, Tier, useApi, Verdict, type Row } from "../ui";

function Stat({ label, value, detail }: { label: string; value: number; detail?: string }) {
  return (
    <div className="rounded-lg border border-stone-200 bg-white px-4 py-3 dark:border-stone-800 dark:bg-stone-900">
      <div className="text-xs tracking-wide text-stone-500 uppercase">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {detail && <div className="text-xs text-stone-500">{detail}</div>}
    </div>
  );
}

function OpenPrs() {
  const [refresh, setRefresh] = useState(0);
  const query = useApi(refresh ? `/api/open-prs?refresh=${refresh}` : "/api/open-prs");
  const client = useQueryClient();
  return (
    <Section
      title="Open dependency PRs"
      aside={
        <button
          className="text-xs text-teal-700 hover:underline dark:text-teal-400"
          onClick={() => {
            client.removeQueries({ queryKey: ["/api/open-prs"] });
            setRefresh(Date.now());
          }}
        >
          {query.data ? `fetched ${new Date(query.data.fetchedAt).toLocaleTimeString()}, refresh` : "refresh"}
        </button>
      }
    >
      <Loading query={query}>
        {() => (
          <>
            {query.data.errors.map((e: Row) => (
              <p key={e.repo} className="mb-2 text-xs text-red-700 dark:text-red-400">
                {e.repo}: {e.error}
              </p>
            ))}
            {query.data.prs.length === 0 ? (
              <Empty>No open Renovate or Dependabot PRs in the registered repos.</Empty>
            ) : (
              <Table head={["Repo", "PR", "Open for", "Ledger"]}>
                {query.data.prs.map((p: Row) => (
                  <tr key={`${p.repo}#${p.number}`}>
                    <td className={td}>
                      <RepoName name={p.repo} />
                    </td>
                    <td className={td}>
                      <a href={p.url} target="_blank" rel="noreferrer" className="hover:underline">
                        <span className="text-stone-500">#{p.number}</span> {p.title}
                      </a>
                    </td>
                    <td className={`${td} whitespace-nowrap text-stone-500`}>{ago(p.createdAt)}</td>
                    <td className={`${td} whitespace-nowrap`}>
                      {p.reviewId ? (
                        <Link to={`/reviews/${p.reviewId}`} className="inline-flex items-center gap-2">
                          <Verdict verdict={p.verdict} />
                          {p.stale && <span className="text-xs text-amber-700 dark:text-amber-400">new commits since</span>}
                        </Link>
                      ) : p.triaged ? (
                        <Link to={prPath(p.repo, p.number)} className="text-xs text-stone-600 hover:underline dark:text-stone-400">
                          triaged, no verdict
                        </Link>
                      ) : (
                        <span className="text-xs text-stone-500">not looked at</span>
                      )}
                    </td>
                  </tr>
                ))}
              </Table>
            )}
          </>
        )}
      </Loading>
    </Section>
  );
}

function Activity({ rows }: { rows: Row[] }) {
  const months = [...new Set(rows.map((r) => r.month))].slice(-6);
  const repos = [...new Set(rows.map((r) => r.repo))];
  const max = Math.max(1, ...months.map((m) => rows.filter((r) => r.month === m).reduce((a, r) => a + r.n, 0)));
  const palette = ["bg-teal-600", "bg-amber-500", "bg-violet-500", "bg-rose-500", "bg-sky-500", "bg-lime-600", "bg-stone-500"];
  return (
    <Section title="Reviews per month">
      <div className="space-y-2">
        {months.map((m) => {
          const total = rows.filter((r) => r.month === m).reduce((a, r) => a + r.n, 0);
          return (
            <div key={m} className="flex items-center gap-3 text-sm">
              <div className="w-16 shrink-0 text-stone-500 tabular-nums">{m}</div>
              <div className="flex h-4 flex-1 overflow-hidden rounded bg-stone-100 dark:bg-stone-800">
                {repos.map((repo, i) => {
                  const n = rows.find((r) => r.month === m && r.repo === repo)?.n ?? 0;
                  return n ? (
                    <div key={repo} title={`${repo}: ${n}`} className={palette[i % palette.length]} style={{ width: `${(n / max) * 100}%` }} />
                  ) : null;
                })}
              </div>
              <div className="w-8 text-right tabular-nums">{total}</div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-600 dark:text-stone-400">
        {repos.map((repo, i) => (
          <span key={repo} className="inline-flex items-center gap-1.5">
            <span className={`inline-block h-2.5 w-2.5 rounded-sm ${palette[i % palette.length]}`} />
            {repo.split("/")[1]}
          </span>
        ))}
      </div>
    </Section>
  );
}

export function Dashboard() {
  const query = useApi("/api/summary");
  return (
    <Page title="Dashboard" subtitle="Every Renovate review, package analysis and repo fact in one place.">
      <Loading query={query}>
        {() => {
          const { counts, recent, trapPackages, perMonth } = query.data;
          return (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Reviews" value={counts.reviews} detail={`${counts.safe} safe, ${counts.notSafe} not safe`} />
                <Stat label="Packages" value={counts.packages} detail={`${counts.traps} TRAP, ${counts.checks} CHECK`} />
                <Stat label="Repos" value={counts.repos} detail={`${counts.notes} notes`} />
                <Stat label="Digests and SHAs" value={counts.digests + counts.actions} detail={`${counts.digests} digests, ${counts.actions} action SHAs`} />
              </div>
              <OpenPrs />
              <div className="grid gap-6 lg:grid-cols-2">
                <Section title="Recent reviews" aside={<Link to="/reviews" className="text-xs text-teal-700 hover:underline dark:text-teal-400">all</Link>}>
                  <ul className="divide-y divide-stone-100 dark:divide-stone-800">
                    {recent.map((r: Row) => (
                      <li key={r.id} className="flex items-start gap-3 py-2 text-sm">
                        <Verdict verdict={r.verdict} title={r.verdict_text} />
                        <div className="min-w-0 flex-1">
                          <Link to={`/reviews/${r.id}`} className="hover:underline">
                            {r.title}
                          </Link>
                          <div className="text-xs text-stone-500">
                            {r.repo.split("/")[1]}#{r.number} · {day(r.reviewed_at)} <Tier tier={r.tier} />
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                </Section>
                <div className="space-y-6">
                  <Activity rows={perMonth} />
                  <Section title="TRAP rows">
                    <ul className="space-y-2 text-sm">
                      {trapPackages.map((p: Row) => (
                        <li key={p.id}>
                          <Link to={`/packages/${p.id}`} className="font-medium hover:underline">
                            {p.name}
                          </Link>{" "}
                          <Status status="TRAP" /> <span className="text-xs text-stone-500">@{p.analysed}</span>
                          <div className="line-clamp-2 text-xs text-stone-600 dark:text-stone-400">{p.gist}</div>
                        </li>
                      ))}
                    </ul>
                  </Section>
                </div>
              </div>
            </>
          );
        }}
      </Loading>
    </Page>
  );
}
