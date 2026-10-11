# Note format reference

> **Stable since 1.0.** Nothing on this page carries any compatibility guarantee before
> `1.0.0` ships - every `0.x` release may still change it without notice. See
> [../compatibility.md](../compatibility.md) (ADR-0017) for what "Stable" means from
> `1.0.0` on.

The note format, its path layout and the `export` output, as decided in
[ADR-0005](../adr/0005-note-format.md). Hand-written, but drift-checked by
`tests/reference_docs.py` (`make lint` / `make reference-docs`) against the literal
constants in
[`../../src/memory_manager/vault/validate.py`](../../src/memory_manager/vault/validate.py),
[`../../src/memory_manager/vault/note.py`](../../src/memory_manager/vault/note.py),
[`../../src/memory_manager/vault/paths.py`](../../src/memory_manager/vault/paths.py) and
[`../../src/memory_manager/exporter.py`](../../src/memory_manager/exporter.py): every
limit, regex, note type, frontmatter field and manifest field named below must still
match those modules' own values, or the check fails naming which one changed.

## Path

`<namespace>/<type>/<slug>.md`, relative to the vault root, `/` as separator.

- `namespace`: matches `^[a-z0-9][a-z0-9-]{0,39}$`, at most 40 characters total,
  reserved when it starts with `_` (`_archive`).
- `type`: one of `user`, `feedback`, `project`, `reference`, `fact`. The directory must
  equal the frontmatter `type`.
- `slug`: matches `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 80 characters.
- The whole path (before the `_archive/` prefix, if any) is at most 200 characters.
- Archive: `_archive/<namespace>/<type>/<slug>.md`. Readable, never a write target
  except through `memory_archive`/`memory_supersede`/`memory_promote`.

## File

UTF-8 without a BOM, LF line endings, ending with exactly one `\n`. The file is
`---\n`, then the YAML frontmatter, then `---\n`, then the Markdown body. Maximum size
is **16384 bytes** for the whole file.

## Frontmatter fields

| Key | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | ULID (26 chars, Crockford base32); never changes, survives moves |
| `title` | string | yes | 1-120 chars, single line |
| `description` | string | yes | 1-150 chars, single line; shown in `memory_index` |
| `type` | string | yes | one of `user`, `feedback`, `project`, `reference`, `fact` |
| `tags` | list of strings | no | each matches `^[a-z0-9][a-z0-9-]{0,39}$`, at most 20, no duplicates |
| `aliases` | list of strings | no | each 1-80 chars, single line, at most 20 |
| `created` | timestamp | yes | RFC 3339 UTC, second precision, `Z` suffix |
| `updated` | timestamp | yes | same format; `>= created`; set on every server write, including archive |
| `valid_from` | date | no | `YYYY-MM-DD` |
| `valid_to` | date | no | `YYYY-MM-DD`, `>= valid_from` |
| `supersedes` | list of strings | no | ULIDs of the notes this one replaces |
| `source` | string | no | 1-200 chars, e.g. `claude.ai`, `claude-code`, a URL |

Unknown frontmatter keys are rejected (`vault/note.py`'s `NoteFormatError`), so a typo
never silently drops a field.

## Canonical serialization

- Keys in the order of the table above; optional keys without a value, and empty
  lists, are omitted.
- Strings are plain YAML scalars when that round-trips unchanged, otherwise
  double-quoted with JSON-style escapes.
- Lists are written in flow style: `tags: [a, b]`.
- Timestamps and dates are written unquoted in the formats above.
- The body is kept byte for byte.

## Version

`version` (the write path's `if_version` token) is the lower-case hex SHA-256 of the
file's bytes.

## Links

`[[slug]]` and `[[slug|label]]` in the body. Links inside code spans and code blocks
are ignored.

## Conflict files

When a push is rejected and the rebase conflicts, the server writes
`<namespace>/<type>/<slug>.conflict.md` next to the note. It is not a note: no
frontmatter, ignored by the indexer, never writable by a client.

## Supersede

A superseding note lists the old note's `id` in its own `supersedes`. The old note
gets `valid_to` set and stays in place - it is never deleted.

## `export` output

`memory-manager export` (and the `postgres`-backend equivalent) produces a `tar.gz`
archive: a `manifest.json` at its root plus every note under `vault/<path>`, byte for
byte as it sits in the vault - no lock-in, readable without `memory-manager`.

`manifest.json` fields:

| Key | Type | Purpose |
|---|---|---|
| `format` | string | archive format identifier (`memory-manager-export`) |
| `format_version` | integer | archive format version |
| `exported_at` | string | export timestamp |
| `vault_head` | string or null | the git commit the export was taken at (`git` backend), or null (`postgres` backend, which has no git `HEAD`) |
| `note_count` | integer | number of notes in the `notes` list below |
| `notes` | list of objects | one entry per exported note, fields below |

Each `notes` entry:

| Key | Type | Purpose |
|---|---|---|
| `path` | string | the note's vault-relative path |
| `id` | string | the note's ULID |
| `title` | string | the note's frontmatter `title` |
| `type` | string | the note's frontmatter `type` |
| `namespace` | string | the note's namespace |
| `archived` | boolean | whether the note was under `_archive/` |
| `sha256` | string | the note file's own version hash |
| `bytes` | integer | the note file's size in bytes |
