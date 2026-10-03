# AGENTS.md — project instructions

> **Seed.** The `my-ai` personal layer wrote this file because this repo had
> none. It is now **the repo's** file: edit it freely, and no `my-ai` update will
> ever overwrite it (`_skip_if_exists`). Every agent tool reads it through the
> `CLAUDE.md` symlink.

## What this project is

_One paragraph: what it does, who uses it, what it is not._

## Working here

```bash
devenv shell                     # enter the pinned environment
repoman-sync                     # verify toolchain + install agent skills
```

_Add the build / test / lint commands, and the gate that must be green before a
PR._

## Where things live

_The two or three directories a newcomer actually needs. Deeper detail belongs in
`docs/`, not here._

## The standing configuration

- **Run everything inside the `devenv` shell** — it pins Python and wires the `*man`
  toolchain (copyroom, gitman, testee, docman, repoman). Never invoke bare
  `uv`/`python`/`pytest`/`git`/`copier`.
- **The lifecycle is RepoMan's.** Scaffold/update → change → verify → save →
  docs. For the order and the routing, start at the `repoman` skill; for domain
  detail open the per-tool skills.
- **Exit codes are an API:** `0` ok · `1` finding · `2` infra/config · `3` usage.
- **`.agents/` and `.claude/`** are machine-local links maintained by the central
  Devman link plane.

Keep this file for what is true of *this* project only.

```bash
copyroom layer list              # which template layers manage this repo
copyroom agent-files check       # conformance report
```
