import { Fragment } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { Code, day, Empty, Loading, Markdown, Page, Revisions, Section, Status, Table, td, useApi, type Row } from "../ui";

export function DocPage() {
  const { slug } = useParams();
  const query = useApi(`/api/docs/${slug}`);
  return (
    <Loading query={query}>
      {() => {
        const { doc, checks, packages, traps, revisions } = query.data;
        return (
          <Page
            title={doc.title}
            subtitle={
              <span className="inline-flex flex-wrap items-center gap-2">
                {doc.kind} doc <span className="font-mono">{doc.slug}</span> · updated {day(doc.updated_at)}
                {packages.map((p: Row) => (
                  <Link key={p.id} to={`/packages/${p.id}`} className="inline-flex items-center gap-1 hover:underline">
                    {p.name} <Status status={p.status} />
                  </Link>
                ))}
              </span>
            }
          >
            {traps.length > 0 && (
              <Section title="Standing traps that point here">
                <ul className="list-disc space-y-1 pl-5 text-sm">
                  {traps.map((t: Row) => (
                    <li key={t.id}>{t.headline}</li>
                  ))}
                </ul>
              </Section>
            )}
            {checks.length > 0 && (
              <Section title="Exposure checks">
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
            <Section title="Body">
              <Markdown text={doc.body} />
            </Section>
            <Revisions revisions={revisions} />
          </Page>
        );
      }}
    </Loading>
  );
}

export function PatternsPage() {
  const query = useApi("/api/patterns");
  return (
    <Page title="Traps & patterns" subtitle="Symptom-keyed traps printed by every triage, and the tooling patterns behind them.">
      <Loading query={query}>
        {() => (
          <>
            <Section title="Standing traps">
              <ol className="space-y-4">
                {query.data.traps.map((t: Row) => (
                  <li key={t.id} className="text-sm">
                    <div className="font-medium">{t.headline}</div>
                    <Markdown text={t.detail} className="text-stone-700 dark:text-stone-300 [&_p]:my-1" />
                    {t.doc_slug && (
                      <Link to={`/docs/${t.doc_slug}`} className="text-xs text-teal-700 hover:underline dark:text-teal-400">
                        {t.doc_slug}
                      </Link>
                    )}
                  </li>
                ))}
              </ol>
            </Section>
            <Section title="Pattern docs">
              <ul className="grid gap-2 text-sm sm:grid-cols-2">
                {query.data.docs.map((d: Row) => (
                  <li key={d.id}>
                    <Link to={`/docs/${d.slug}`} className="hover:underline">
                      {d.title}
                    </Link>{" "}
                    <span className="text-xs text-stone-500">{day(d.updated_at)}</span>
                  </li>
                ))}
              </ul>
            </Section>
          </>
        )}
      </Loading>
    </Page>
  );
}

export function ImagesPage() {
  const query = useApi("/api/images");
  return (
    <Page title="Image digests" subtitle="Probed digests, keyed by their first 12 characters.">
      <Loading query={query}>
        {() => {
          const groups = new Map<string, Row[]>();
          for (const d of query.data.digests as Row[]) groups.set(d.image, [...(groups.get(d.image) ?? []), d]);
          return (
            <>
              {query.data.notes && (
                <Link to={`/docs/${query.data.notes.slug}`} className="text-sm text-teal-700 hover:underline dark:text-teal-400">
                  {query.data.notes.title}
                </Link>
              )}
              {[...groups.entries()].map(([image, rows]) => {
                const factKeys = [...new Set(rows.flatMap((r) => Object.keys(JSON.parse(r.facts))))];
                return (
                  <Section key={image} title={<span className="font-mono normal-case">{image}</span>} aside={<span className="text-xs text-stone-500">{rows.length}</span>}>
                    <Table head={["Tag", "Digest", ...factKeys, "Notes"]}>
                      {rows.map((r) => {
                        const facts = JSON.parse(r.facts);
                        return (
                          <tr key={r.id}>
                            <td className={`${td} font-mono text-xs whitespace-nowrap`}>
                              {r.tag}
                              {r.line && <div className="text-stone-500">{r.line}</div>}
                            </td>
                            <td className={`${td} font-mono text-xs`}>{r.digest12}</td>
                            {factKeys.map((k) => (
                              <td key={k} className={`${td} text-xs`}>
                                {facts[k]}
                              </td>
                            ))}
                            <td className={`${td} max-w-md text-xs text-stone-600 dark:text-stone-400`}>
                              <Markdown text={r.notes} className="[&_p]:my-0" />
                            </td>
                          </tr>
                        );
                      })}
                    </Table>
                  </Section>
                );
              })}
            </>
          );
        }}
      </Loading>
    </Page>
  );
}

export function ActionsPage() {
  const query = useApi("/api/actions");
  return (
    <Page title="Pinned action SHAs" subtitle="Tag and SHA pairs already confirmed through the GitHub API.">
      <Loading query={query}>
        {() => (
          <>
            {query.data.notes && (
              <Link to={`/docs/${query.data.notes.slug}`} className="text-sm text-teal-700 hover:underline dark:text-teal-400">
                {query.data.notes.title}
              </Link>
            )}
            <Section title={`${query.data.shas.length} pairs`}>
              <Table head={["Action", "Tag", "Commit", "Type", "Note"]}>
                {query.data.shas.map((a: Row) => (
                  <tr key={a.id}>
                    <td className={`${td} font-mono text-xs`}>{a.action}</td>
                    <td className={`${td} font-mono text-xs`}>{a.tag}</td>
                    <td className={`${td} font-mono text-xs`}>{a.sha.slice(0, 12)}</td>
                    <td className={`${td} text-xs`}>
                      {a.tag_type}
                      {a.annotated_sha && <div className="font-mono text-stone-500">tag {a.annotated_sha.slice(0, 12)}</div>}
                    </td>
                    <td className={`${td} text-xs text-stone-500`}>{a.note}</td>
                  </tr>
                ))}
              </Table>
            </Section>
          </>
        )}
      </Loading>
    </Page>
  );
}

function hitLink(h: Row) {
  switch (h.kind) {
    case "doc":
      return `/docs/${h.slug}`;
    case "package":
      return `/packages/${h.ref_id}`;
    case "note":
      return `/repos/${h.repo}/notes/${h.slug ?? h.ref_id}`;
    case "review":
      return `/reviews/${h.ref_id}`;
    case "trap":
      return h.slug ? `/docs/${h.slug}` : "/patterns";
    case "image":
      return "/images";
    default:
      return "/";
  }
}

function Snippet({ text }: { text: string }) {
  const parts = (text ?? "").split(/(\u0001|\u0002)/);
  let on = false;
  return (
    <>
      {parts.map((p, i) => {
        if (p === "\u0001") {
          on = true;
          return null;
        }
        if (p === "\u0002") {
          on = false;
          return null;
        }
        return on ? (
          <mark key={i} className="rounded bg-amber-200/70 px-0.5 dark:bg-amber-700/50 dark:text-amber-50">
            {p}
          </mark>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        );
      })}
    </>
  );
}

export function SearchPage() {
  const [params] = useSearchParams();
  const q = params.get("q") ?? "";
  const query = useApi<Row[]>(q ? `/api/search?q=${encodeURIComponent(q)}` : null);
  return (
    <Page title={`Search: ${q}`} subtitle="Substring search, so package names, file names and digest prefixes all match.">
      {!q ? (
        <Empty>Type at least three characters in the search box.</Empty>
      ) : (
        <Loading query={query}>
          {() =>
            query.data!.length ? (
              <ul className="space-y-3">
                {query.data!.map((h) => (
                  <li key={`${h.kind}:${h.ref_id}`} className="rounded-lg border border-stone-200 bg-white p-3 dark:border-stone-800 dark:bg-stone-900">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[0.7rem] font-semibold tracking-wide text-stone-600 uppercase dark:bg-stone-800 dark:text-stone-300">
                        {h.kind}
                      </span>
                      {h.repo && <span className="text-xs text-stone-500">{h.repo}</span>}
                      <Link to={hitLink(h)} className="min-w-0 font-medium break-words hover:underline">
                        {h.title.length > 160 ? h.title.slice(0, 157) + "..." : h.title}
                      </Link>
                    </div>
                    <p className="mt-1 text-sm text-stone-600 dark:text-stone-400">
                      <Snippet text={h.snippet} />
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No matches.</Empty>
            )
          }
        </Loading>
      )}
    </Page>
  );
}
