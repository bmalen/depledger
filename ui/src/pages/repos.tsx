import { Link, useParams } from "react-router";
import { day, Empty, Loading, Markdown, Page, prPath, PrState, RepoName, Revisions, Section, Table, td, Tier, useApi, Verdict, type Row } from "../ui";

const splitTags = (text: string) =>
  text
    .replace(/\s*\([^)]*\)/g, "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

export function ReposPage() {
  const query = useApi<Row[]>("/api/repos");
  return (
    <Page title="Repos">
      <Loading query={query}>
        {() => (
          <Section title={`${query.data!.length} registered`}>
            <Table head={["Repo", "Reviews", "Not safe", "Notes", "Last review", "Checkout"]}>
              {query.data!.map((r) => (
                <tr key={r.id}>
                  <td className={td}>
                    <RepoName name={r.full_name} />
                  </td>
                  <td className={`${td} tabular-nums`}>{r.review_count}</td>
                  <td className={`${td} tabular-nums`}>{r.not_safe_count || ""}</td>
                  <td className={`${td} tabular-nums`}>{r.note_count}</td>
                  <td className={`${td} text-stone-500`}>{day(r.last_reviewed)}</td>
                  <td className={`${td} font-mono text-xs text-stone-500`}>{r.local_path?.replace(/^\/Users\/[^/]+/, "~")}</td>
                </tr>
              ))}
            </Table>
          </Section>
        )}
      </Loading>
    </Page>
  );
}

function NoteItem({ note, repo }: { note: Row; repo: string }) {
  const tags = splitTags(note.when_tags);
  return (
    <li className="py-2.5">
      <div className="flex items-start gap-2 text-sm">
        <div className="min-w-0 flex-1">
          <Markdown text={note.gist} repo={repo} className="[&_p]:my-0" />
          <div className="mt-1 flex flex-wrap gap-1.5">
            {tags.map((t) => (
              <span key={t} className="rounded bg-teal-50 px-1.5 py-0.5 font-mono text-[0.7rem] text-teal-800 dark:bg-teal-950 dark:text-teal-300">
                {t}
              </span>
            ))}
            {note.unverified ? <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[0.7rem] text-amber-800 dark:bg-amber-950 dark:text-amber-300">unverified</span> : null}
          </div>
        </div>
        <Link to={`/repos/${repo}/notes/${note.slug ?? note.id}`} className="shrink-0 font-mono text-xs text-stone-400 hover:text-teal-700">
          {note.slug ?? `#${note.id}`}
        </Link>
      </div>
      {note.body && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-stone-500">detail</summary>
          <Markdown text={note.body} repo={repo} className="mt-2 border-l-2 border-stone-200 pl-3 dark:border-stone-700" />
        </details>
      )}
    </li>
  );
}

export function RepoPage() {
  const { owner, name } = useParams();
  const repo = `${owner}/${name}`;
  const query = useApi(`/api/repos/${repo}`);
  return (
    <Loading query={query}>
      {() => {
        const { notes, reviews } = query.data;
        const always = notes.filter((n: Row) => !splitTags(n.when_tags).length);
        const tagged = notes.filter((n: Row) => splitTags(n.when_tags).length);
        return (
          <Page title={repo} subtitle={`${reviews.length} reviews · ${notes.length} notes`}>
            <div className="grid gap-6 lg:grid-cols-2">
              <Section title={`Printed on every triage (${always.length})`}>
                {always.length ? (
                  <ul className="divide-y divide-stone-100 dark:divide-stone-800">
                    {always.map((n: Row) => (
                      <NoteItem key={n.id} note={n} repo={repo} />
                    ))}
                  </ul>
                ) : (
                  <Empty>None.</Empty>
                )}
              </Section>
              <Section title={`Printed when a tag matches (${tagged.length})`}>
                {tagged.length ? (
                  <ul className="divide-y divide-stone-100 dark:divide-stone-800">
                    {tagged.map((n: Row) => (
                      <NoteItem key={n.id} note={n} repo={repo} />
                    ))}
                  </ul>
                ) : (
                  <Empty>None.</Empty>
                )}
              </Section>
            </div>
            <Section title="Reviews">
              <Table head={["PR", "Title", "Verdict", "PR state", "Reviewed"]}>
                {reviews.map((r: Row) => (
                  <tr key={r.id}>
                    <td className={td}>
                      <Link to={prPath(repo, r.number)} className="text-stone-500 hover:underline">
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
                    <td className={td}>
                      <PrState state={r.state} />
                    </td>
                    <td className={`${td} whitespace-nowrap text-stone-500`}>{day(r.reviewed_at)}</td>
                  </tr>
                ))}
              </Table>
            </Section>
          </Page>
        );
      }}
    </Loading>
  );
}

export function NotePage() {
  const { owner, name, ref } = useParams();
  const repo = `${owner}/${name}`;
  const query = useApi(`/api/repos/${repo}/notes/${ref}`);
  return (
    <Loading query={query}>
      {() => {
        const { note, revisions } = query.data;
        return (
          <Page
            title={note.slug ?? `Note ${note.id}`}
            subtitle={
              <span>
                <RepoName name={repo} /> · {note.when_tags ? `when: ${note.when_tags}` : "printed on every triage"} · updated {day(note.updated_at)}
                {note.unverified ? " · unverified" : ""}
              </span>
            }
          >
            <Section title="Gist">
              <Markdown text={note.gist} repo={repo} />
            </Section>
            {note.body && (
              <Section title="Detail">
                <Markdown text={note.body} repo={repo} />
              </Section>
            )}
            <Revisions revisions={revisions} />
          </Page>
        );
      }}
    </Loading>
  );
}
