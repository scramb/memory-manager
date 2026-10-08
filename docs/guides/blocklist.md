# Operator blocklist

An optional, operator-defined set of content categories that reject a write outright
(`BLOCKLIST_FILE`, #244, owner decision O26). Off by default: with no `BLOCKLIST_FILE`
set, no write is ever checked against a blocklist at all.

## Configuring one

Point `BLOCKLIST_FILE` at a TOML file of `[[category]]` entries, each with a `name` and
`patterns` (Python `re` regexes) and/or `keywords` (matched case-insensitively on a word
boundary - German umlauts and ß count as word characters). See
[`examples/blocklist.example.toml`](../../examples/blocklist.example.toml) for the exact
shape; both German and English entries live in one file, there is no separate
per-language mechanism.

The file is loaded once, eagerly, at process startup (`serve --stdio`/`--http`, and every
CLI command that opens the vault): a category with an invalid regex, a missing `name`, or
neither `patterns` nor `keywords` refuses startup rather than surfacing as a confusing
rejection on whatever note a user happens to write first.

## What gets checked

`memory_write`, `memory_edit` and `memory_supersede` - the same content-producing write
path the secret scan already covers (`vault/secrets.py`), checked right after it, on
both storage backends. `memory_archive` is unaffected: it only moves an existing note to
`_archive/...`, it never introduces new content.

A hit rejects the write with `BlocklistRejected`, naming only the matched category - never
the text that matched it (CLAUDE.md: note content is data, not instructions, and is never
echoed back). The audit log records the same thing: `outcome: "rejected"`, `detail`
holding `{"error": "BlocklistRejected", "category": "<name>"}` and nothing else.

## Not included

Per-namespace blocklists and classifier- or model-based content filtering are explicitly
out of scope for this mechanism (#244) - every category here is a static regex/keyword
list an operator wrote down, not a judgement call a model makes.
