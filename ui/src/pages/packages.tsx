import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { Code, day, Empty, Loading, Markdown, Page, prPath, RepoName, Revisions, Section, Status, Table, td, Tier, useApi, Verdict, type Row } from "../ui";

export function PackagesPage() {
  const query = useApi<Row[]>("/api/packages");
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("");
  const [eco, setEco] = useState("");
  const rows = useMemo(() => {
    const f = filter.toLowerCase();
    return (query.data ?? []).filter(
      (p) =>
        (!status || p.status === status) &&
        (!eco || p.ecosystem === eco) &&
        (!f || p.name.toLowerCase().includes(f) || p.gist.toLowerCase().includes(f) || (p.names ?? "").toLowerCase().includes(f)),
    );
  }, [query.data, filter, status, eco]);
  const select = "rounded-md border border-stone-300 bg-white px-2 py-1.5 text-sm dark:border-stone-700 dark:bg-stone-900";
  return (
    <Page title="Packages" subtitle="The banked upstream analysis per package. The exposure check is re-run every review.">
      <div className="flex flex-wrap gap-2">
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by name or gist" className={`${select} w-64`} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} className={select}>
          <option value="">Any status</option>
          <option>TRAP</option>
          <option>CHECK</option>
          <option>routine</option>
        </select>
        <select value={eco} onChange={(e) => setEco(e.target.value)} className={select}>
          <option value="">Any ecosystem</option>
          <option>npm</option>
          <option>composer</option>
          <option>actions</option>
          <option>images</option>
        </select>
      </div>
      <Loading query={query}>
        {() => (
          <Section title={`${rows.length} packages`}>
            <Table head={["Package", "Status", "Analysed", "Repos", "Gist"]}>
              {rows.map((p) => (
                <tr key={p.id}>
                  <td className={td}>
                    <Link to={`/packages/${p.id}`} className="font-medium hover:underline">
                      {p.name}
                    </Link>
                    <div className="text-xs text-stone-500">{p.ecosystem}</div>
                  </td>
                  <td className={td}>
                    <Status status={p.status} />
                  </td>
                  <td className={`${td} whitespace-nowrap tabular-nums`}>{p.analysed}</td>
                  <td className={`${td} text-center tabular-nums`}>{p.repo_count || ""}</td>
                  <td className={`${td} max-w-xl text-stone-600 dark:text-stone-400`}>
                    <div className="line-clamp-2">{p.gist}</div>
                  </td>
                </tr>
              ))}
            </Table>
          </Section>
        )}
      </Loading>
    </Page>
  );
}

/** The latest bump of the package in each repo, then the full history underneath. */
function AcrossRepos({ history }: { history: Row[] }) {
  if (!history.length) return <Empty>No PR in any repo has moved this package yet.</Empty>;
  const latest = new Map<string, Row>();
  for (const h of history) if (!latest.has(h.repo)) latest.set(h.repo, h);
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {[...latest.values()].map((h) => (
          <Link
            key={h.repo}
            to={h.review_id ? `/reviews/${h.review_id}` : prPath(h.repo, h.number)}
            className="rounded-md border border-stone-200 px-3 py-2 hover:border-teal-600 dark:border-stone-800"
          >
            <div className="flex items-center justify-between gap-2 text-sm">
              <span className="font-medium">{h.repo.split("/")[1]}</span>
              {h.review_id ? <Verdict verdict={h.verdict} title={h.verdict_text} /> : <span className="text-xs text-stone-500">triaged</span>}
            </div>
            <div className="mt-1 font-mono text-xs">
              {h.name} {h.from_version || "?"} → {h.to_version || "?"}
            </div>
            <div className="text-xs text-stone-500">
              #{h.number} · {day(h.reviewed_at ?? h.triaged_at ?? h.opened_at)}
            </div>
          </Link>
        ))}
      </div>
      <details>
        <summary className="cursor-pointer text-sm text-stone-600 dark:text-stone-400">Every PR ({history.length})</summary>
        <Table head={["Repo", "PR", "Move", "Verdict", "Date"]}>
          {history.map((h) => (
            <tr key={`${h.pr_id}:${h.name}:${h.review_id}`}>
              <td className={td}>
                <RepoName name={h.repo} />
              </td>
              <td className={td}>
                <Link to={prPath(h.repo, h.number)} className="hover:underline">
                  #{h.number}
                </Link>
              </td>
              <td className={`${td} font-mono text-xs`}>
                {h.name} {h.from_version || "?"} → {h.to_version || "?"}
              </td>
              <td className={td}>
                {h.review_id ? (
                  <Link to={`/reviews/${h.review_id}`} className="inline-flex gap-2">
                    <Verdict verdict={h.verdict} title={h.verdict_text} /> <Tier tier={h.tier} />
                  </Link>
                ) : (
                  <span className="text-xs text-stone-500">triaged only</span>
                )}
              </td>
              <td className={`${td} whitespace-nowrap text-stone-500`}>{day(h.reviewed_at ?? h.triaged_at ?? h.opened_at)}</td>
            </tr>
          ))}
        </Table>
      </details>
    </div>
  );
}

export function PackagePage() {
  const { id } = useParams();
  const query = useApi(`/api/packages/${id}`);
  return (
    <Loading query={query}>
      {() => {
        const { package: p, names, doc, checks, sharedWith, history, notes, revisions } = query.data;
        return (
          <Page
            title={p.name}
            subtitle={
              <span className="inline-flex flex-wrap items-center gap-2">
                <Status status={p.status} /> {p.ecosystem} · analysed to {p.analysed} · answers to {names.join(", ")}
              </span>
            }
          >
            <p className="max-w-4xl">{p.gist}</p>
            {checks.length > 0 && (
              <Section title="Exposure checks (re-run every review)">
                <div className="space-y-3">
                  {checks.map((c: Row) => (
                    <div key={c.id}>
                      {c.label && <div className="mb-1 text-sm font-medium">{c.label}</div>}
                      <Code>{c.commands}</Code>
                    </div>
                  ))}
                </div>
              </Section>
            )}
            <Section title="Across repos">
              <AcrossRepos history={history} />
            </Section>
            {notes.length > 0 && (
              <Section title="Repo notes tagged for it">
                <ul className="space-y-3 text-sm">
                  {notes.map((n: Row) => (
                    <li key={n.id}>
                      <RepoName name={n.repo} /> ·{" "}
                      <Link to={`/repos/${n.repo}/notes/${n.slug ?? n.id}`} className="hover:underline">
                        {n.gist}
                      </Link>
                    </li>
                  ))}
                </ul>
              </Section>
            )}
            {doc && (
              <Section
                title={`Analysis (${doc.slug})`}
                aside={
                  <span className="text-xs text-stone-500">
                    updated {day(doc.updated_at)}
                    {sharedWith.length > 0 && ` · shared with ${sharedWith.map((s: Row) => s.name).join(", ")}`}
                  </span>
                }
              >
                <Markdown text={doc.body} />
              </Section>
            )}
            <Revisions revisions={revisions} />
          </Page>
        );
      }}
    </Loading>
  );
}
