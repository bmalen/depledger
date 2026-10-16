import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, NavLink, Outlet, useNavigate, useSearchParams } from "react-router";
import { RouterProvider } from "react-router/dom";
import { Dashboard } from "./pages/dashboard";
import { ActionsPage, DocPage, ImagesPage, PatternsPage, SearchPage } from "./pages/misc";
import { PackagePage, PackagesPage } from "./pages/packages";
import { NotePage, RepoPage, ReposPage } from "./pages/repos";
import { PrPage, ReviewPage, ReviewsPage } from "./pages/reviews";

const NAV = [
  ["/", "Dashboard"],
  ["/packages", "Packages"],
  ["/repos", "Repos"],
  ["/reviews", "Reviews"],
  ["/patterns", "Traps & patterns"],
  ["/images", "Images"],
  ["/actions", "Actions"],
] as const;

function SearchBox() {
  const [params] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const navigate = useNavigate();
  return (
    <form
      className="w-full sm:w-72"
      onSubmit={(e) => {
        e.preventDefault();
        if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`);
      }}
    >
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search notes, docs, reviews, digests"
        className="w-full rounded-md border border-stone-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20 dark:border-stone-700 dark:bg-stone-900"
      />
    </form>
  );
}

function Layout() {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-stone-200 bg-stone-50/90 backdrop-blur dark:border-stone-800 dark:bg-stone-950/90">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <NavLink to="/" className="font-semibold tracking-tight">
            Dependency ledger
          </NavLink>
          <nav className="flex flex-1 flex-wrap gap-x-4 gap-y-1 text-sm">
            {NAV.map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  isActive ? "font-medium text-teal-700 dark:text-teal-400" : "text-stone-600 hover:text-stone-900 dark:text-stone-400 dark:hover:text-stone-100"
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
          <SearchBox />
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Dashboard /> },
      { path: "/packages", element: <PackagesPage /> },
      { path: "/packages/:id", element: <PackagePage /> },
      { path: "/docs/:slug", element: <DocPage /> },
      { path: "/repos", element: <ReposPage /> },
      { path: "/repos/:owner/:name", element: <RepoPage /> },
      { path: "/repos/:owner/:name/notes/:ref", element: <NotePage /> },
      { path: "/repos/:owner/:name/prs/:number", element: <PrPage /> },
      { path: "/reviews", element: <ReviewsPage /> },
      { path: "/reviews/:id", element: <ReviewPage /> },
      { path: "/patterns", element: <PatternsPage /> },
      { path: "/images", element: <ImagesPage /> },
      { path: "/actions", element: <ActionsPage /> },
      { path: "/search", element: <SearchPage /> },
    ],
  },
]);

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: true } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
