# depledger

A shared ledger for reviewing dependency-update PRs (Renovate, Dependabot) across many repositories.

Reviewing a dependency bump is mostly remembering: what changed upstream between two versions, which
command proves whether a repo is exposed, which image digests were already probed, and how the same
bump went in the other repos you maintain. depledger keeps all of that in one SQLite database with a
CLI to read and write it and a small web UI to browse it.

It was built for an AI coding agent (Claude Code) doing the reviews, so the CLI prints compact text
sized for a model's context and takes one fact per write. It works just as well by hand.

## What it records

- **Packages**: one row per package (or glob such as `@tiptap/*`) with the highest version analysed,
  a status (`routine`, `CHECK` or `TRAP`) and a one-line gist, optionally backed by a markdown
  analysis doc and the exposure checks (shell commands) that decide whether a repo is affected.
- **Traps and patterns**: symptom-keyed warnings about tooling rather than one package, printed on
  every triage.
- **Repo notes**: standing facts about one repository. Untagged notes print on every triage of that
  repo; tagged notes print only when a tag matches the PR (a bucket such as `frontend` or
  `docker-image`, a package name or glob, the branch, or a merge signal such as `lock_ok=no`).
- **PRs, bumps and reviews**: every triaged PR with what it moves, and every verdict, so the next
  review of the same package anywhere shows how it went before.
- **Image digests and action SHAs** already verified, so they are not probed twice.

Every change to prose keeps the previous version in a revisions table, and everything is searchable
with SQLite FTS5 (trigram, so package names and digest prefixes match as substrings).

## Requirements

- Python 3.9 or newer (standard library only)
- git and an authenticated [GitHub CLI](https://cli.github.com/) (`gh`)
- Docker with buildx, optional, for the image digest checks
- [Bun](https://bun.sh/), optional, for the web UI

## Install

```
git clone https://github.com/bmalen/depledger.git
ln -s "$PWD/depledger/bin/depledger" ~/.local/bin/depledger   # or anywhere on your PATH
(cd depledger/ui && bun install)                               # only for the web UI
```

Data lives in `$DEPLEDGER_HOME`, default `${XDG_DATA_HOME:-~/.local/share}/depledger`: the database
(`depledger.db`), an `export/` folder that every write refreshes (one JSON Lines file per table, so
the ledger can be backed up and diffed in git) and the UI log. Set `DEPLEDGER_DB` to use a different
database file; its export then goes beside it.

To back the ledger up, put the data directory under git with a remote (a private repository: the
ledger holds notes about your own projects), then run `depledger backup` now and then. It refreshes
`export/`, commits only that folder and pushes. The database file itself does not need committing.

## Usage

Run commands from inside a checkout of the repo the PR belongs to. Repo-scoped commands read the
repo from the `origin` remote (or `--repo owner/name`) and refuse to write anywhere else.

```
depledger triage 123 --out /tmp/pr-123          # fetch, parse, look everything up, propose a tier
depledger checks --from /tmp/pr-123 --checkout   # run the local checks triage planned, on the PR head
depledger review save owner/repo#123 --verdict safe --tier 1 < entry.md

depledger show package @tiptap/core             # the package across every repo, with its analysis
depledger search prosemirror-view               # substring search over every doc, note and review
depledger show repo                             # this repo's notes and recent reviews

depledger package set npm axios --analysed 1.20.0 --status CHECK --gist "..."
depledger package doc npm axios --append < analysis.md
depledger package exposure npm axios < check.sh
depledger note add "Base branch is staging" --always
depledger note add "Redis runs on phpredis" --when "predis/predis"
depledger probe --from /tmp/pr-123             # probe and bank every image digest triage found
depledger image edit ba6e394f6acc --append-notes "dev compose only"
depledger image add redis:alpine@ba6e394f6acc --fact version=8.10.2 --fact os="Alpine 3.23"
depledger action add actions/checkout v6.1.0 <sha> --type commit

depledger ui                                    # start the web UI (http://127.0.0.1:7420)
```

`depledger --help` and `depledger <command> --help` list everything.

### Triage

`depledger triage` makes one pass over a PR and prints sections for the PR and CI state, every
manifest bump with its ledger status, exposure checks and history in this repo and elsewhere,
lock-only movers, GitHub Actions pins (resolved through the API, both tag hops), container images
(head of tag, per-arch manifests, whether CI runs the new digest), lock completeness, the branch
against its base, the local environment, a proposed risk tier with the checks it needs, the standing
traps, the matching repo notes, and the command to save the verdict. It is tuned for projects using
npm or bun and composer, with Laravel Sail detected when present.

It also prints what CI actually ran on the PR head (each GitHub Actions job with its longest steps
and anything failed or skipped), and the other open PRs that change the same files, each merged
against the base with this PR applied so that a conflict this PR causes is told apart from one the
other PR already has. An analysed version that does not cover the bump (a newer patch, or an older
major line) is marked on the package line.

### Local checks

When a PR needs local checks, triage writes `checks.json` beside its other output and prints the
command. `depledger checks --from <dir> --checkout` checks out the PR head (refusing a dirty tree or
local-only commits), then runs the frozen install for the repo's package manager (bun, pnpm, npm or
yarn) and `composer install`, then build, typecheck and each command of the lint script with
`--fix` stripped side by side, then the tests, inside Laravel Sail when the repo has it and on the
host otherwise. Each step prints PASS or FAIL with its evidence: the lock left untouched, the files
and errors eslint counted, the test summary. `--list` prints the plan, `--only` runs chosen steps,
and `--here` runs on whatever is checked out for a baseline.

### Image probes

Triage writes every image change as full old and new refs to `probes.json` in its `--out` dir.
`depledger probe --from <dir>` skips each pair whose new digest is already on record, runs the
rest in parallel (`docker run` with a POSIX `sh` script, amd64 first), and prints one block per
image: a class (`IDENTICAL`, `REBUILD ONLY`, `PACKAGE REFRESH`, `VERSION MOVE`), the runtime and
Postgres extension versions that moved, and the PHP module and dpkg/apk/rpm package deltas. Both
digests are banked with the same fact keys every time, the new one with the delta as its notes.
Dumps are cached by digest under `<data dir>/probes` (`DEPLEDGER_PROBE_DIR`), so a digest is pulled
once per machine. `depledger probe <old-ref> <new-ref>` does one pair, `--no-bank` probes digests
on record too and writes nothing, and `scripts/probe-image.sh` is a print-only wrapper around it.

## Web UI

`depledger ui` starts a read-only Bun server on `127.0.0.1:7420` (change it with `DEPLEDGER_UI_PORT`).
It shows open dependency PRs across every registered repo next to their verdicts, packages with
their history across repos, repo notes, reviews, traps and patterns, digests, action pins and search.

## Tests

```
python3 -m unittest discover tests
```

## License

MIT, see [LICENSE](LICENSE).
