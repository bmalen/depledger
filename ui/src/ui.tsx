import { useQuery } from "@tanstack/react-query";
import { Marked } from "marked";
import type { MouseEvent, ReactNode } from "react";
import { Link, useNavigate } from "react-router";

export type Row = Record<string, any>;

export function useApi<T = any>(path: string | null) {
  return useQuery<T>({
    queryKey: [path],
    enabled: path !== null,
    queryFn: async () => {
      const res = await fetch(path as string);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    },
  });
}

export function Loading({ query, children }: { query: { isPending: boolean; error: Error | null }; children: () => ReactNode }) {
  if (query.isPending) return <p className="py-8 text-sm text-stone-500">Loading...</p>;
  if (query.error) return <p className="py-8 text-sm text-red-700 dark:text-red-400">Could not load: {query.error.message}</p>;
  return <>{children()}</>;
}

export function Page({ title, subtitle, actions, children }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight break-words">{title}</h1>
          {subtitle && <div className="mt-1 text-sm text-stone-600 dark:text-stone-400">{subtitle}</div>}
        </div>
        {actions}
      </div>
      {children}
    </div>
  );
}

export function Section({ title, aside, children }: { title: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center justify-between gap-3 border-b border-stone-200 px-4 py-2.5 dark:border-stone-800">
        <h2 className="text-sm font-semibold tracking-wide text-stone-700 uppercase dark:text-stone-300">{title}</h2>
        {aside}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

const STATUS_STYLES: Record<string, string> = {
  TRAP: "bg-red-100 text-red-800 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-900",
  CHECK: "bg-amber-100 text-amber-900 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-900",
  routine: "bg-stone-100 text-stone-700 ring-stone-200 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-700",
};

export function Status({ status }: { status: string | null }) {
  if (!status) return null;
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 text-[0.7rem] font-semibold tracking-wide ring-1 ${STATUS_STYLES[status] ?? STATUS_STYLES.routine}`}>
      {status}
    </span>
  );
}

export function Verdict({ verdict, title }: { verdict: string | null; title?: string | null }) {
  const style =
    verdict === "safe"
      ? "bg-emerald-100 text-emerald-800 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-900"
      : verdict === "not_safe"
        ? "bg-red-100 text-red-800 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-900"
        : "bg-stone-100 text-stone-600 ring-stone-200 dark:bg-stone-800 dark:text-stone-400 dark:ring-stone-700";
  const label = verdict === "safe" ? "safe" : verdict === "not_safe" ? "not safe" : "no verdict";
  return (
    <span title={title ?? undefined} className={`inline-flex rounded px-1.5 py-0.5 text-[0.7rem] font-semibold whitespace-nowrap ring-1 ${style}`}>
      {label}
    </span>
  );
}

export function Tier({ tier }: { tier: number | null }) {
  if (tier === null || tier === undefined) return null;
  return <span className="text-xs whitespace-nowrap text-stone-500">tier {tier}</span>;
}

export function PrState({ state }: { state: string | null }) {
  if (!state) return null;
  const style =
    state === "MERGED" ? "text-violet-700 dark:text-violet-400" : state === "OPEN" ? "text-emerald-700 dark:text-emerald-400" : "text-stone-500";
  return <span className={`text-xs font-medium ${style}`}>{state.toLowerCase()}</span>;
}

export const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "");

export function ago(iso: string) {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  return days <= 0 ? "today" : days === 1 ? "1 day" : `${days} days`;
}

export const repoPath = (fullName: string) => `/repos/${fullName}`;
export const prPath = (fullName: string, number: number) => `/repos/${fullName}/prs/${number}`;

export function RepoName({ name }: { name: string }) {
  const [owner, repo] = name.split("/");
  return (
    <Link to={repoPath(name)} className="whitespace-nowrap hover:underline">
      <span className="text-stone-500">{owner}/</span>
      <span className="font-medium">{repo}</span>
    </Link>
  );
}

function escapeHtml(text: string) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Ledger links: `doc:<slug>` and `note:<slug>` (within the repo being shown). */
function resolveHref(href: string, repo?: string) {
  if (href.startsWith("doc:")) return `/docs/${href.slice(4)}`;
  if (href.startsWith("note:")) return repo ? `/repos/${repo}/notes/${href.slice(5)}` : "#";
  return href;
}

function markdown(repo?: string) {
  const md = new Marked({ gfm: true });
  md.use({
    renderer: {
      html({ text }) {
        return escapeHtml(text);
      },
      link({ href, title, tokens }) {
        const target = /^\s*javascript:/i.test(href) ? "#" : resolveHref(href, repo);
        const external = /^https?:/.test(target);
        const text = this.parser.parseInline(tokens);
        return `<a href="${escapeHtml(target)}"${title ? ` title="${escapeHtml(title)}"` : ""}${external ? ' target="_blank" rel="noreferrer"' : ""}>${text}</a>`;
      },
      codespan({ text }) {
        const m = /^(doc|note):([\w.-]+)$/.exec(text);
        if (m) return `<a href="${escapeHtml(resolveHref(text, repo))}"><code>${escapeHtml(text)}</code></a>`;
        return `<code>${escapeHtml(text)}</code>`;
      },
    },
  });
  return md;
}

export function Markdown({ text, repo, className = "" }: { text: string | null; repo?: string; className?: string }) {
  const navigate = useNavigate();
  if (!text?.trim()) return null;
  const html = markdown(repo).parse(text) as string;
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href");
    if (href && href.startsWith("/") && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      navigate(href);
    }
  };
  return <div className={`prose-ledger ${className}`} onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Code({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-md bg-stone-900 p-3 font-mono text-[0.8rem] leading-relaxed text-stone-100 dark:bg-black/60">
      {children}
    </pre>
  );
}

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full min-w-max text-left text-sm">
        <thead>
          <tr className="border-b border-stone-200 text-xs tracking-wide text-stone-500 uppercase dark:border-stone-800">
            {head.map((h, i) => (
              <th key={i} className="px-4 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-stone-100 dark:divide-stone-800">{children}</tbody>
      </table>
    </div>
  );
}

export const td = "px-4 py-2 align-top";

export function Revisions({ revisions }: { revisions: Row[] }) {
  if (!revisions.length) return null;
  return (
    <Section title={`History (${revisions.length})`}>
      <ul className="space-y-3 text-sm">
        {revisions.map((r) => (
          <li key={r.id}>
            <details>
              <summary className="cursor-pointer text-stone-600 dark:text-stone-400">
                {day(r.changed_at)} {r.action} of {r.table_name.replace(/s$/, "")} (previous version from {day(r.snapshot.updated_at)})
              </summary>
              <div className="mt-2 space-y-2">
                {Object.entries(r.snapshot)
                  .filter(([k, v]) => k !== "updated_at" && v !== null && v !== "")
                  .map(([k, v]) => (
                    <div key={k}>
                      <div className="text-xs font-medium text-stone-500">{k}</div>
                      <pre className="font-mono text-xs whitespace-pre-wrap">{String(v)}</pre>
                    </div>
                  ))}
              </div>
            </details>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-stone-500">{children}</p>;
}
