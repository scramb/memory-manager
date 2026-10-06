# ADR-0005 — Note format: one Markdown file per note with canonical YAML frontmatter

Status: Accepted · Date: 2026-10-06
Relates to: vault component; #6, #7, #8, #9, #10, #19, #24

## Context

Notes are the source of truth (PLAN → Data model). Parser, validation, path safety, link extraction, the write queue (`if_version`) and the index all depend on one exact definition of the format. The PLAN gives the fields and the 16 KB size cap. Six points were still open: slug charset, namespace charset, timestamp precision, whether `updated` changes on archive, the `_archive/` layout and the canonical key order. The owner accepted the format as drafted in the PLAN on 2026-10-06 (O5) and chose the `type` set `user`, `feedback`, `project`, `reference`, `fact`.

## Options

### A — Canonical, fully specified frontmatter (key order, quoting, list style, timestamps fixed)
Pro: the byte-stable round trip makes `version` meaningful, and human diffs stay minimal · Con: the serializer is our own code instead of `yaml.dump`.

### B — Any valid YAML, normalised only on write
Pro: humans can use any YAML style · Con: the first write by Claude rewrites the whole frontmatter of a human-edited note, creating noisy diffs, and `version` changes without any change in content.

### C — TOML frontmatter (`+++`)
Pro: less ambiguous than YAML · Con: Obsidian and most Markdown tools expect YAML.

## Decision

**A**, accepted by the owner on 2026-10-06. The parser accepts any valid YAML a human writes (safe loader); everything the server writes is canonical.

### Path

`<namespace>/<type>/<slug>.md`, relative to the vault root, `/` as separator.

- `namespace`: `^[a-z0-9][a-z0-9-]{0,39}$`. Names starting with `_` are reserved (`_archive`).
- `type`: one of `user`, `feedback`, `project`, `reference`, `fact`. The directory must equal the frontmatter `type`.
- `slug`: `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 80 characters. Unique within `namespace/type`.
- Archive: `_archive/<namespace>/<type>/<slug>.md`. It is readable but never a write target, except through `memory_archive`. Archiving moves the file in one commit.

### File

UTF-8 without BOM, LF line endings, ending with exactly one `\n`. The file is `---\n`, then the frontmatter, then `---\n`, then the Markdown body. Maximum size is **16,384 bytes** for the whole file.

### Frontmatter fields (canonical order)

| Key | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | ULID (26 chars, Crockford base32, upper case); never changes, survives moves |
| `title` | string | yes | 1–120 chars, single line |
| `description` | string | yes | 1–150 chars, single line; used for recall in `memory_index` |
| `type` | string | yes | enum above |
| `tags` | list of strings | no | each `^[a-z0-9][a-z0-9-]{0,39}$`, at most 20, no duplicates |
| `aliases` | list of strings | no | each 1–80 chars, single line, at most 20 |
| `created` | timestamp | yes | RFC 3339 UTC, second precision, `Z` suffix (`2026-10-06T14:03:00Z`) |
| `updated` | timestamp | yes | same format; `>= created`; set on every server write **including archive** |
| `valid_from` | date | no | `YYYY-MM-DD` |
| `valid_to` | date | no | `YYYY-MM-DD`, `>= valid_from` |
| `supersedes` | list of strings | no | ULIDs of the notes this one replaces |
| `source` | string | no | 1–200 chars, e.g. `claude.ai`, `claude-code`, a URL |

Unknown keys are rejected, so that typos cannot silently drop data.

### Canonical serialization

- Keys in the order of the table above; optional keys without a value, and empty lists, are omitted.
- Strings are plain YAML scalars when that round-trips unchanged. Otherwise they are double-quoted with JSON-style escapes.
- Lists are written in flow style: `tags: [a, b]`.
- Timestamps and dates are written unquoted in the formats above.
- The body is kept byte for byte.

### Version

`version` = lower-case hex SHA-256 of the file bytes. It is the `if_version` token of the write path.

### Links

`[[slug]]` and `[[slug|label]]` in the body. Links inside code spans and code blocks are ignored.

### Supersede

A superseding note lists the old `id` in `supersedes`. The old note gets `valid_to` set and stays in place; it is not deleted.

Checked against the guardrails:
- Few dependencies: only `pyyaml` (safe loader) for parsing, plus the stdlib.
- OSS first: yes.
- Container: n/a.
- Technology pool: n/a (format decision inside ADR-0001).

## Consequences

- `vault/note.py` needs its own canonical writer (~100 lines) instead of `yaml.dump`.
- A human edit in non-canonical YAML gets normalised by the next server write to that file. Notes the server does not touch are never rewritten.
- The `type` set is small on purpose: it decides the directory layout, so changing it later means moving files.

## Reversibility

Expensive once real vaults exist. Changing paths or types means a migration that moves files in the users' vault repos. Adding optional fields is cheap.
