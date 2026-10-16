import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { day, Empty, Loading, Markdown, Page, prPath, PrState, RepoName, Revisions, Section, Status, Table, td, Tier, useApi, Verdict, type Row } from "../ui";

export function ReviewsPage() {
  const query = useApi<Row[]>("/api/reviews");
  const [verdict, setVerdict] = useState("");
  const [repo, setRepo] = useState("");
  const [filter, setFilter] = useState("");
  const repos = useMemo(() => [...new Set((query.data ?? []).map((r) => r.repo))].sort(), [query.data]);
  const rows = (query.data ?? []).filter(
    (r) =>
      (!repo || r.repo === repo) &&
      (!verdict || (verdict === "none" ? !r.verdict : r.verdict === verdict)) &&
      (!filter || r.title.toLowerCase().includes(filter.toLowerCase())),
  );
  const select = "rounded-md border border-stone-300 bg-white px-2 py-1.5 text-sm dark:border-stone-700 dark:bg-stone-900";
  return (
    <Page title="Reviews">
      <div className="flex flex-wrap gap-2">
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by title" className={`${select} w-64`} />
        <select value={repo} onChange={(e) => setRepo(e.target.value)} className={select}>
          <option value="">Every repo</option>
          {repos.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
        <select value={verdict} onChange={(e) => setVerdict(e.target.value)} className={select}>
          <option value="">Any verdict</option>
          <option value="safe">safe</option>
          <option value="not_safe">not safe</option>
          <option value="none">no verdict</option>
        </select>
      </div>
      <Loading query={query}>
        {() => (
          <Section title={`${rows.length} reviews`}>
            <Table head={["Repo", "PR", "Title", "Verdict", "Reviewed"]}>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className={td}>
                    <RepoName name={r.repo} />
                  </td>
                  <td className={td}>
                    <Link to={prPath(r.repo, r.number)} className="text-stone-500 hover:underline">
                      #{r.number}
                    </Link>
                  </td>
                  <td className={td}>
                    <Link to={`/reviews/${r.id}`} className="hover:underline">
                      {r.title}
                    </Link>
                  </td>
                  <td className={`${td} whitespace-nowrap`}>
                    <Verdict verdict={r.verdict} title={r.verdict_text} /> <Tier tier={r.tier} />
                  </td>
                  <td className={`${td} whitespace-nowrap text-stone-500`}>{day(r.reviewed_at)}</td>
                </tr>
              ))}
            </Table>
          </Section>
        )}
      </Loading>
    </Page>
  );
}

function Bumps({ bumps }: { bumps: Row[] }) {
  if (!bumps.length) return <Empty>No moves recorded.</Empty>;
  return (
    <Table head={["Kind", "Name", "From", "To", "Type", "Ledger"]}>
      {bumps.map((b) => (
        <tr key={b.id}>
          <td className={`${td} text-xs text-stone-500`}>{b.kind}</td>
          <td className={`${td} font-mono text-xs`}>
            {b.package_id ? (
              <Link to={`/packages/${b.package_id}`} className="hover:underline">
                {b.name}
              </Link>
            ) : (
              b.name
            )}
            {b.section && <div className="text-stone-500">{b.section}</div>}
          </td>
          <td className={`${td} font-mono text-xs`}>{b.from_version}</td>
          <td className={`${td} font-mono text-xs`}>{b.to_version}</td>
          <td className={`${td} text-xs text-stone-500`}>{b.bump_type}</td>
          <td className={td}>
            <Status status={b.package_status} />
          </td>
        </tr>
      ))}
    </Table>
  );
}

export function ReviewPage() {
  const { id } = useParams();
  const query = useApi(`/api/reviews/${id}`);
  return (
    <Loading query={query}>
      {() => {
        const { review: v, prs, revisions } = query.data;
        return (
          <Page
            title={v.title}
            subtitle={
              <span className="inline-flex flex-wrap items-center gap-2">
                <Verdict verdict={v.verdict} /> <Tier tier={v.tier} /> <RepoName name={v.repo} /> · reviewed {day(v.reviewed_at)}
                {v.head_sha && <span className="font-mono text-xs">at {v.head_sha.slice(0, 12)}</span>}
                {v.source !== "skill" && <span className="text-xs">(imported from markdown)</span>}
              </span>
            }
          >
            {v.verdict_text && <p className="max-w-4xl text-sm text-stone-600 italic dark:text-stone-400">"{v.verdict_text}"</p>}
            <Section title="Entry">
              <Markdown text={v.body} repo={v.repo} />
            </Section>
            {prs.map((pr: Row) => (
              <Section
                key={pr.id}
                title={
                  <span>
                    {pr.is_primary ? "" : "Also: "}
                    <Link to={prPath(pr.repo, pr.number)} className="normal-case hover:underline">
                      #{pr.number} {pr.title}
                    </Link>
                  </span>
                }
                aside={
                  <span className="flex items-center gap-3">
                    <PrState state={pr.state} />
                    {pr.url && (
                      <a href={pr.url} target="_blank" rel="noreferrer" className="text-xs text-teal-700 hover:underline dark:text-teal-400">
                        GitHub
                      </a>
                    )}
                  </span>
                }
              >
                <Bumps bumps={pr.bumps} />
              </Section>
            ))}
            <Revisions revisions={revisions} />
          </Page>
        );
      }}
    </Loading>
  );
}

export function PrPage() {
  const { owner, name, number } = useParams();
  const repo = `${owner}/${name}`;
  const query = useApi(`/api/repos/${repo}/prs/${number}`);
  return (
    <Loading query={query}>
      {() => {
        const { pr, bumps, reviews } = query.data;
        return (
          <Page
            title={`#${pr.number} ${pr.title}`}
            subtitle={
              <span className="inline-flex flex-wrap items-center gap-2">
                <RepoName name={repo} /> <PrState state={pr.state} /> · {pr.branch}
                {pr.triaged_at && <span>· triaged {day(pr.triaged_at)}, proposed tier {pr.proposed_tier ?? "?"}</span>}
                {pr.url && (
                  <a href={pr.url} target="_blank" rel="noreferrer" className="text-teal-700 hover:underline dark:text-teal-400">
                    GitHub
                  </a>
                )}
              </span>
            }
          >
            <Section title="Reviews">
              {reviews.length ? (
                <ul className="space-y-2 text-sm">
                  {reviews.map((v: Row) => (
                    <li key={v.id} className="flex flex-wrap items-center gap-2">
                      <Verdict verdict={v.verdict} title={v.verdict_text} /> <Tier tier={v.tier} />
                      <Link to={`/reviews/${v.id}`} className="hover:underline">
                        {v.title}
                      </Link>
                      <span className="text-xs text-stone-500">{day(v.reviewed_at)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>Triaged, no verdict saved yet.</Empty>
              )}
            </Section>
            <Section title="Moves">
              <Bumps bumps={bumps} />
            </Section>
          </Page>
        );
      }}
    </Loading>
  );
}
