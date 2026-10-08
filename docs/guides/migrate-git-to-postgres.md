# Migrate a Git vault into the Postgres backend

`memory-manager migrate git-to-postgres` imports an existing Git vault's current notes and
their full Git history into the `postgres` storage backend (enterprise mode,
[ADR-0007](../adr/0007-storage-backend.md) §6). Every note's `version` stays byte-identical to
what is on disk, and every commit that ever touched a note becomes one `vault_revisions` row,
with the commit's author, time and message. Archived notes stay archived.

## Prerequisites

- A Git vault, checked out locally (`git clone` it first if the server normally pulls it from a
  remote - the migration reads a working tree, not a bare repository).
- A reachable Postgres 16+ with `pgvector`, migrated or not - the CLI runs the schema migration
  itself before importing.
- `DATABASE_URL` pointing at that Postgres, connected as the role that will own the schema (the
  same owner credential `memory-manager serve --http` uses with `STORAGE_BACKEND=postgres`,
  ADR-0008's addendum "system identity" - the migration writes revisions with an explicit
  `author_oid` per note, something no MCP tool's own write path is ever allowed to do).
- One `--map <git-namespace>=<kind>:<key>[:<alias>]` per top-level namespace the vault has -
  including one that currently exists only under `_archive/`. The dry run (below) tells you if
  one is missing.

## `--map` syntax

`<kind>` is one of:

| kind | key | alias | notes |
|---|---|---|---|
| `user` | the owner's `oid` | none - always shown as `me` to that owner | `--map personal=user:oid-1234` |
| `project` | anything | defaults to the Git namespace name | `--map work=project:work` - needs a `project_members` row before anyone can read it (see below) |
| `group` | anything | defaults to the Git namespace name | readable by a token whose `groups` claim names the key |
| `org` | always `org` | always `org` | `--map shared=org:org` - there is exactly one org namespace |

Aliases follow the same charset a namespace path segment does (lower-case letters, digits,
hyphens, max 40 chars), may not start with `_` or `u-`, and may not be `me` or `org` - those are
reserved.

## Dry run

```sh
memory-manager migrate git-to-postgres --vault /path/to/vault \
  --map personal=user:oid-1234 \
  --map work=project:work \
  --map shared=org:org \
  --dry-run
```

Reports every discovered namespace, its mapped target (or `UNMAPPED`), how many live and
archived notes it holds, how many revisions its history has, and any problem that would block
the import (an invalid note, a secret, an unresolved `.conflict.md` file, a namespace nobody
mapped). Nothing is written - this is safe to run against a vault that already has problems, to
see all of them at once.

## Import

Drop `--dry-run` to actually write. The command refuses to touch the database at all if its own
preflight dry run is not clean:

```sh
memory-manager migrate git-to-postgres --vault /path/to/vault \
  --map personal=user:oid-1234 \
  --map work=project:work \
  --map shared=org:org
```

Each mapped namespace imports in its own transaction - one namespace being refused (its stored
alias already holds notes; this command never merges into a non-empty namespace) never stops
another from importing. The index is rebuilt from the imported notes once every namespace is
done, so the backend is searchable right away.

A `project`/`group` namespace needs its membership wired up separately before anyone can read
it: there is no CLI for this yet, so insert the row directly, e.g.

```sql
insert into project_members (namespace_id, principal_kind, principal_id, role)
values ((select id from namespaces where kind = 'project' and alias = 'work'), 'user', 'oid-1234', 'owner');
```

(An `org` namespace needs no such row - it is readable by any identity that holds a memory
role at all.)

## Verify

Create a token for the imported owner and read a note back:

```sh
memory-manager token create check --scope memory:read --owner oid-1234 --role Memory.User
```

`memory_read` on a path under `me/...` (the personal namespace) or the project/org alias you
mapped should return the same `version` `vault.note.version` computes for the note's bytes on
disk. `tests/migrate/test_example_vault.py` does exactly this against `examples/vault`, end to
end through a real `memory-manager serve --http` process.

## Rollback

`migrate git-to-postgres` only ever reads the source vault - nothing about it is modified or
consumed. If anything about the import looks wrong, the simplest rollback is to keep pointing
`memory-manager serve` at the untouched Git vault (`STORAGE_BACKEND` unset, or `git`) while you
fix the Postgres side and try again.

If you need the Postgres side's own current state back out as Markdown - for example because the
source Git vault has since drifted from what is actually live in Postgres - `export` also reads a
`postgres`-backed deployment: with `STORAGE_BACKEND=postgres` and `DATABASE_URL` set, the same
`export` subcommand writes the current notes of `vault_notes` into the same tar.gz + manifest
archive it writes for a Git vault (`--vault`/`VAULT_DIR` are ignored in this mode). This is
one-way and never read back ([ADR-0007](../adr/0007-storage-backend.md)): it exports current
content only, not revision history, so going back this way loses every revision's metadata.

```sh
STORAGE_BACKEND=postgres DATABASE_URL=... memory-manager export --out rollback.tar.gz
```
