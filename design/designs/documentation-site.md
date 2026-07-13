# Design: Public Documentation Site

**Status:** Accepted — implemented in this PR
**Date recorded:** 2026-07-13
**Applies to:** a new, self-contained documentation site for `team-harness`, living
in this repository and published on a `writeit.ai` subdomain.

This document records the plan and the decisions behind it so a later reader
understands *why* the docs stack looks the way it does.

The shared principle:

> **Ship the docs as a self-hostable static module that lives with the code, authored
> in MDX the native Next.js way, styled to read as part of the WriteIt family, and
> searchable from the keyboard.**

---

## Goals

1. **Presentable, human-first documentation.** Comprehensible to a developer meeting
   `team-harness` for the first time — good navigation, prose, and examples, not just
   an option dump.
2. **Self-hostable module.** A `clone → build → host` static site with no docs SaaS.
   It ships *in this repo* so docs version with the code.
3. **On a `writeit.ai` subdomain, styled like `writeit.ai`.** Reachable at
   `team-harness.writeit.ai` via CNAME and visually in the same colour family as the
   main site, so a link from `writeit.ai` feels continuous.
4. **Authored in MDX, the way Next.js's own docs work** — Markdown files that are
   routes, with the option to drop in components.
5. **Keyboard search (⌘K / Ctrl+K).** A command-palette search over the docs.

---

## Precedent — replicate the `loopy-loop` docs site

The direct precedent is the sibling **`loopy-loop`** documentation site
([`loopy.writeit.ai`](https://loopy.writeit.ai)), which itself replicated orchestra's
native `@next/mdx` pattern and added Pagefind search and the WriteIt palette. That site
is a proven, self-contained module — roughly a dozen files — and satisfies every goal
above. Rather than re-derive the stack, this site is lifted from `loopy-loop/website/`
and re-homed onto team-harness content.

Everything below is therefore inherited from that precedent; the only team-harness-
specific decisions are the content (Decision 6) and the domain.

## Decision 1 — Native `@next/mdx`, `page.mdx`-as-route

Build with **native `@next/mdx`** (each `src/app/docs/**/page.mdx` is a route),
Tailwind v4 + `@tailwindcss/typography`, `remark-gfm` + `rehype-slug` +
`rehype-pretty-code` (Shiki), a hand-maintained navigation array
(`src/lib/docs/navigation.ts`), a docs `layout.tsx` giving a three-column
sidebar / prose / on-this-page shell with prev–next pagination, and
`output: 'export'` for a fully static build. Proven in-house, MDX-as-routes, no new
framework to learn. (Fallback if search wiring stops being worth owning:
[Fumadocs](https://fumadocs.dev/), which bundles search and dark mode.)

## Decision 2 — Live inside this repo, under `website/`

The site lives at `website/` in the `team-harness` repo, not a separate repo or the
private `writeit` monorepo, so docs version alongside the code they describe. The
`website/` app has its own `package.json` / toolchain, independent of the Python
package; it is never published to PyPI, only built into a static site.

## Decision 3 — GitHub Pages at `team-harness.writeit.ai` via CNAME

Publish the static export to **GitHub Pages** from this repo, served at
**`team-harness.writeit.ai`** via CNAME. Pages keeps hosting with the OSS repo, needs
no server, and supports a custom domain. The product-branded subdomain was chosen over
a generic `docs.writeit.ai` so the URL ties to the tool by name.

Mechanics: a GitHub Actions workflow (`.github/workflows/docs-deploy.yml`) builds the
export, runs Pagefind over the output, and deploys with `actions/deploy-pages`.
`output: 'export'` + `trailingSlash: true` give directory-style URLs that resolve to
`index.html`; served at the subdomain root, no `basePath` is needed; a `.nojekyll`
marker keeps Next's `_next/` assets. The custom domain is a **one-time** provisioning
step (Settings → Pages source = GitHub Actions, custom domain = `team-harness.writeit.ai`,
a DNS `CNAME` to `writeitai.github.io`, then Enforce HTTPS) — the committed
`public/CNAME` records intent but does not bind the domain on its own. See
`website/README.md` for the checklist.

## Decision 4 — Keyboard (⌘K) search via Pagefind + `cmdk`

Search via **[Pagefind](https://pagefind.app/)** (a static, self-hostable index built
from the exported HTML as a post-build step) surfaced through a **`cmdk`** command
dialog bound to ⌘K / Ctrl+K. Keeps search inside the self-hostable constraint with no
query-time server.

## Decision 5 — WriteIt palette, open-font substitute, light-first

Style with `writeit.ai`'s brand colours (sand `#f7ebbd`, ink `#222433`, green
`#5ca493`, gold `#ebaa1a`, red `#f34832`) mapped onto shadcn-style CSS variables in
`src/app/globals.css`. Substitute the open **Hanken Grotesk** (via `next/font`) for the
domain-locked proxima-nova so the module stays self-hostable. Ship **light mode first**
(the dark palette is present but not yet wired to a toggle), matching the light-only
`writeit.ai`.

## Decision 6 — Author content from the README and source docs

The content is authored as MDX pages sourced from the existing `README.md`, `CLAUDE.md`
architecture notes, and the design docs. Proposed information architecture (one
`page.mdx` each, order in `navigation.ts`):

| Route | Source material |
|---|---|
| `/docs` — Introduction | README overview, tagline, value prop |
| `/docs/getting-started` | Install, worker prerequisites, `th init`, first run, logs |
| `/docs/concepts` | Coordinator/worker split, request flow, tool registry, templates, context, skills |
| `/docs/configuration` | `config.toml`, resolution order, env vars, prompt files, output, retries |
| `/docs/workers` | Agent-template schema, built-in workers, custom agents, model + reasoning effort |
| `/docs/providers` | `openai_compat` vs `codex`, auth, routing workers through OpenRouter |
| `/docs/skills` | Agent Skills: directories, `SKILL.md`, naming, subdirectories |
| `/docs/context-management` | Context tracking, auto-compaction, `/compact`, `/clear` |
| `/docs/sdk` | `TeamHarness`, constructor parameters, `TeamHarnessResult` |
| `/docs/cli-reference` | `th run`/`repl` flags, REPL commands, editing keys, terminal features |
| `/docs/coordinator-tools` | The coordinator's agent / file-system / shell / task tools |
| `/docs/run-logs` | `run.json`, `todo.json`, `worker_sessions.json`, per-worker logs |
| `/docs/troubleshooting` | Common failure modes, migration notes, trust model |

Docs must track the current API — e.g. the coordinator default model is `gpt-5.6-sol`,
not the older `gpt-5.5` some README examples still show.

---

## Stack summary

`Next.js` (App Router) · `@next/mdx` · `output: 'export'` + `trailingSlash: true` ·
`Tailwind v4` + `@tailwindcss/typography` · `remark-gfm` + `rehype-slug` +
`rehype-pretty-code` (Shiki) · `Pagefind` + `cmdk` for ⌘K search · WriteIt palette with
Hanken Grotesk · deployed to GitHub Pages at `team-harness.writeit.ai`.

## Follow-up (out of scope for this repo's PR)

- After the site is live, add a "Docs" link on `writeit.ai` → `https://team-harness.writeit.ai`
  (a change in the `writeit` repo).
- Add **dark mode** (wire the existing dark tokens to a `next-themes` toggle) once light
  mode ships.
