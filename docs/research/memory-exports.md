# Memory export formats: Claude and ChatGPT (research for a memory-manager importer)

Retrieved: 2026-10-06. Neither vendor publishes a schema. Field names below come from
third-party parsers and fixtures built against real exports (cited per claim). "Unverified"
means no real export was opened for this note.

## 1. Claude (claude.ai)

### 1.1 Ways to get memory out

| Path | How | Output |
|---|---|---|
| A. In-app view | Settings > Capabilities > "View and edit your memory", copy manually | Free text |
| B. Prompt | Ask Claude: "Write out your memories of me verbatim, exactly as they appear in your memory." | Free text, written by the model |
| C. Account data export | Settings > Privacy > Export data; a download link arrives by email and expires after 24 h; web and Desktop only, not mobile | ZIP(s) with `memories.json` / `memories/<uuid>.json` |

A and B come from the official "Import and export your memory" article [S1]. C comes from the official
export article [S2]. That article only says the export holds "conversation data and the user data for
your account". It does **not** mention memory [S2]. That memory is part of the export comes from
third-party parsers that read it [S4–S8]. One survey claims an official FAQ says "All memory data is
included in data exports" [S7], but I could not find that sentence on support.claude.com: **unverified**.
Several SEO guides say memory is *not* in the export [S9]. Real-export fixtures contradict them [S5, S6],
so those guides are probably out of date.

Import into Claude (for completeness): Settings > Memory > "Start import", paste the text, "Add to
memory". This is experimental. Claude extracts the facts itself and favours work-related ones [S1].
The prompt it suggests for other providers asks for one entry per line: `[date saved, if available] - memory content` [S1].

### 1.2 Packaging (changed in 2026)

- **Legacy (single zip, observed until about Aug 2026):** `users.json`, `projects.json` (or `projects/`),
  `memories.json`, `conversations.json` [S3, S4].
- **Current (observed 2026-09-01 and 2026-09-22):** the email links to a **manifest JSON** that names one
  zip per category, and each zip can be split into parts `-000`, `-001`, … [S4, S5]:
  `conversations-000.zip` → `conversations.json`; `projects-000.zip` → `projects/<uuid>.json`;
  `memories-000.zip` → `memories/<uuid>.json`; `light_metadata-000.zip` → `users.json`,
  `login_history.json`; plus `design_chats-000.zip` [S5] and `feedback-000.zip` → `reflections/<uuid>.json` [S6].
  Each manifest URL works once and needs the claude.ai session cookie [S4].

### 1.3 Memory JSON structure

The top level is an array with one record in the legacy form [S8]. The current per-account file
`memories/<uuid>.json` is a single object [S5]. Known keys:

| Key | Type | Meaning | Source |
|---|---|---|---|
| `conversations_memory` | string | Global memory as one prose/Markdown block (older "summary-style" memory) | [S5, S8, S10] |
| `project_memories` | object `{project_uuid: text}` | Per-project memory. Keys join with `projects/<uuid>.json` | [S3, S4, S8] |
| `memory_files` | array of `{path, content, updated_at}` | Newer file-based memory, one Markdown file per topic | [S5, S7, S10] |
| `account_uuid` | string | Account ID; matches `users.json` | [S5, S8] |

Fictional minimal example (key names match the real-export fixture [S5]; the values are invented):

```json
{
  "conversations_memory": "**Work context**\nAlex is a backend engineer ...",
  "project_memories": { "0198aaaa-0000-7000-8000-000000000001": "Project uses Postgres 16 ..." },
  "memory_files": [
    { "path": "/profile.md", "content": "- [stated] Prefers metric units\n- [inferred] Works in UTC+1",
      "updated_at": "2026-01-02T03:04:05.000000Z" }
  ],
  "account_uuid": "00000000-0000-4000-8000-0000000000aa"
}
```

Caveats:
- Example paths such as `/profile.md` and `/preferences.md`, plus per-person/per-topic files, are reported
  by one tool vendor [S7]. The section headings in `conversations_memory` ("Work context", "Personal
  context", "Top of mind", "Brief history") come from one blog [S11]. Inline `[stated]` / `[inferred]`
  tags inside the content are reported by one repo note [S12]. **All three are single-source and
  unverified.** The importer must treat `content` as opaque Markdown.
- A key may be absent depending on account age and plan. Accept any subset of the four.
- The export does not link conversations to projects [S4]. This does not matter for importing memory.

## 2. ChatGPT (OpenAI)

### 2.1 Ways to get memory out

| Path | How | Output |
|---|---|---|
| A. Manage memories | Settings > Personalization > Manage memories (legacy "saved memories" list), copy by hand | Plain list [S13, S14] |
| B. Memory summary (since June 2026) | Memory now has a readable, editable "memory summary" that updates itself; users can switch back to legacy saved memories in settings [S15, S16] | Prose. No export button found |
| C. Prompt workaround | Ask the model to list every stored memory in one code block, formatted `[date] - content` (the prompt Anthropic suggests [S1]). Memories sit in a "Model Set Context" system block with dated lines such as `[2025-05-02] The user likes ice cream` [S17] | Text, written by the model and possibly incomplete |
| D. Data export | Settings > Data Controls > Export data; the link arrives by email [S13, S18] | ZIP, see 2.2 |

help.openai.com returns 403 to non-browser clients, so the official export article
(`/articles/7260999`) and the Memory FAQ (`/articles/8590148`) **could not be read directly**. Their
content here is second-hand.

### 2.2 Export ZIP contents

Common files: `conversations.json`, `chat.html`, `user.json`, `message_feedback.json`,
`model_comparisons.json`, `shared_conversations.json`, plus media [S18, S19, S7]. Newer exports split
conversations into `conversations-000.json`, `conversations-001.json`, … and add `user_settings.json` and
`export_manifest.json` [S20]. `conversations.json` is an array of conversations. Each conversation has
`title`, `create_time` / `update_time` (Unix float seconds), `current_node`, and `mapping` (a tree of
nodes `{id, parent, children[], message}`) [S21].

**Are saved memories included as a file?** Most likely no, but **unverified**:
- Several guides say memory entries are not in the export [S13, S14]. A cross-vendor survey concludes
  "not in the export" and lists it as still to be checked against a real export [S7].
- Some parsers handle a `memory.json` / `memories.json` (array of `{id, content, created_at?,
  updated_at?, enabled?}`) "in newer exports" [S10, S22]. I found no real-export fixture or sample that
  shows such a file. Treat it as speculative and parse it only if it is present.

**Traces of memory inside `conversations.json`** (confirmed by many parsers):
- Memory writes appear as assistant messages with `recipient: "bio"`. The text in `content.parts[]` is
  the stored fact. The tool reply is usually "Model set context updated." [S23, S24]. ChatGPT can
  acknowledge a write and still not save it [S23], so a bio call does not prove the memory exists.
- Snapshot of memory injected into a chat: a message with `content.content_type ==
  "model_editable_context"` and the field `content.model_set_context` (string) [S24, S25].
- Custom instructions (not memory): `content_type == "user_editable_context"` with `user_profile` /
  `user_instructions`. Metadata may contain `about_user_message` / `about_model_message` [S20, S24].

Fictional minimal node:

```json
{ "id": "n2", "parent": "n1", "children": [],
  "message": { "author": {"role": "assistant"}, "recipient": "bio", "create_time": 1767225600.0,
               "content": {"content_type": "text", "parts": ["User prefers Python for scripting."]} } }
```

Caveats: the export has no stable memory IDs. Memories deleted in the UI still appear as historical bio
calls. The 2026 memory-summary system may no longer write through `bio` [S23]. The export is not
available for every workspace plan [S21].

## 3. Proposed importer design for memory-manager

### 3.1 Accepted inputs (detected by content, not by file name)

1. **Claude memory JSON**: a `memories/<uuid>.json` file, a legacy `memories.json`, or a zip that contains
   one. Detection: the object (or the first array element) has any of `conversations_memory`,
   `project_memories` or `memory_files`. Ignore every other zip entry, conversations included.
2. **ChatGPT memory JSON** (only if it ever appears): an array of objects with `content|text|memory`.
   Skip entries with `enabled: false`.
3. **ChatGPT export zip / conversations JSON** (opt-in flag `--from-conversations`): extract `bio`
   payloads and the latest `model_set_context` snapshot. Mark them lower confidence, because these are
   historical writes and not the current memory state.
4. **Plain text / Markdown list** (works for every provider and is the main path for ChatGPT): one memory
   per line or per `-`/`*`/`1.` bullet. Strip code fences. Parse an optional leading date as `[YYYY-MM-DD]`,
   `YYYY-MM-DD -`, or `[date] -`, the format Anthropic's prompt produces [S1]. Lines that are blank or
   contain only a heading become tags or context, not notes. The user passes `--source claude|chatgpt`.

The importer reads only these files. It never runs or interprets their content, in line with the
"note content is data" rule.

### 3.2 Mapping to notes

| Input unit | Notes produced | title | type | tags |
|---|---|---|---|---|
| list line / ChatGPT entry / `bio` payload | 1 per entry | first ~8 words, sentence-cased | heuristic (below) | `imported`, provider |
| Claude `memory_files[i]` | 1 per file. If the file is a bullet list, split it into one note per bullet | from `path` stem (`profile` → "Profile") or bullet text | from path or heuristic | path stem; `stated`/`inferred` if those tags are present |
| Claude `conversations_memory` | split on Markdown headings; 1 note per section, or per bullet if the section is a list | heading text | from heading (below) | heading slug |
| Claude `project_memories[uuid]` | 1 note per project | project `name` from `projects/<uuid>.json` if available, else "Project memory <uuid8>" | `project` | `project:<slug>` |

- **description** (≤150 chars): the first sentence of the entry, whitespace collapsed, cut at a word
  boundary at 147 characters plus "…".
- **type** heuristic (deterministic and rule-based, no LLM): headings or paths `profile`, `personal`,
  `about` → `user`. `preferences`, `instructions`, `style` or text such as "prefers / wants / don't" →
  `feedback`. `project`, `work context`, or a project key → `project`. Text containing a URL or tool/doc
  reference → `reference`. Everything else → `fact`. The user can override the type with `--type`.
- **source**: `import:claude` or `import:chatgpt`. Extra provenance goes in frontmatter: original
  date (if parsed) or `updated_at`, `import_origin` (`memory_files:/profile.md`, `bio`,
  `list:line 12`, …), and the import run ID.
- **Body**: the original text verbatim, never rewritten.
- **Path**: `imported/<provider>/<slug>.md`, so imported notes are easy to review or archive in bulk.
- **Secret scan**: each note goes through the normal write path (secret scan, path allowlist,
  audit log). Hits are reported and skipped, never written.

### 3.3 Deduplication

1. **Normalise** the text: Unicode NFKC, casefold, collapse whitespace, strip bullets, leading dates,
   `[stated]`/`[inferred]` tags and trailing punctuation.
2. **Exact key**: `sha256(provider + ":" + normalised_text)`, stored in frontmatter as `import_key`.
   Re-running the same import is a no-op: an existing key is skipped and counted as `unchanged`.
3. **Within one run**: collapse identical normalised texts. This matters because ChatGPT
   `bio`/`model_set_context` repeat across many conversations [S20]. Keep the earliest date.
4. **Against the vault (near-duplicates)**: before writing, run the existing hybrid search with the
   entry text. If the top hit has a very high similarity, report the entry as `possible_duplicate`
   and do not write it automatically. The user can confirm with `--accept-duplicates`. This keeps
   "never overwrite silently": the importer only creates notes and never edits existing ones.
5. **Dry run by default**: `import --dry-run` prints planned notes, types, skips and duplicates.
   `--apply` writes them through the write queue, as one commit per import run.

### 3.4 Gates to raise with the owner before building

The importer adds a CLI surface and new frontmatter keys (`import_key`, `import_origin`), so the note
format changes. Under CLAUDE.md this needs approval or an ADR. The `--from-conversations`
heuristics for ChatGPT should stay opt-in until a real export confirms the shapes above.

### 3.5 What was actually built (#49)

The owner's task contract for #49 rejected the new `import_key`/`import_origin` frontmatter keys from
3.2/3.3 outright - ADR-0005 stays frozen, so no gate was raised and no ADR was written for this. Dedup
without a dedicated key works instead by scanning the vault's working copy once per run for notes whose
`source` already starts with `import:` and comparing a normalised hash of each candidate's text (NFKC,
casefold, strip punctuation, collapse whitespace) against that set and against the rest of the current
run (`importers.core.dedupe_against_vault`). A note's own "Imported from ... on ..." provenance line
sits in the body, after the memory text and a blank line, precisely so that line's date never changes
what the hash is taken over (`importers.core.dedup_hash` only looks at the body's first paragraph) -
re-running the same import, even on a different day, produces zero new notes.

Everything else built simpler than 3.1-3.3 propose:

- `memory_files[]` entries are never split into one note per bullet - one note per file, title from the
  file's own first Markdown heading or else its humanized path stem, as the task contract specified.
  `conversations_memory` and `project_memories` are split into one item per bullet/paragraph via a
  shared parser (`importers.textlist.parse_items`), not "on Markdown headings" - headings are dropped as
  structure, not kept as a title source.
- No rule-based `type` heuristic (profile/preferences/project/URL -> user/feedback/project/reference):
  Claude's `conversations_memory` is always `user`, `project_memories` is always `project`; everything
  else (memory_files, ChatGPT items, the plain-text fallback) takes `--type` (default `user`).
- No vault-wide semantic/hybrid-search near-duplicate pass and no `--accept-duplicates` - the normalised
  hash scan above is the only duplicate check.
- No `imported/<provider>/<slug>.md` path convention - notes land at the normal
  `<namespace>/<type>/<slug>.md` (ADR-0005), namespace given by the required `--namespace` flag.
- The ChatGPT importer extracts only `bio` tool calls under `--from-conversations`, not the
  `model_set_context` snapshot 3.1 also lists - that snapshot is unverified beyond a single parser
  source ([S24], [S25]) and the task contract did not ask for it.

A fix round added zip-bomb guards to `importers.claude._search_zip`, since none of 3.1-3.3 above
considered a hostile export file (`importers.claude.py`, constants `_MAX_ZIP_DEPTH` et al.):

- The outer input file is size-checked (`Path.stat()`, no read) before it is ever opened: over
  100 MB, `collect()` raises `ClaudeFormatError` without touching the file's content.
- Zip nesting stops at depth 2 (the export zip plus at most one inner `memories-*.zip`) - a third
  level raises `ClaudeFormatError` instead of being descended into.
- Only `.json` entries matching the expected memory layout (`memories.json`, `memories/*.json`) and
  inner `.zip` entries matching `memories(-NNN)?.zip` are ever opened; every other entry (conversations,
  projects, media, ...) is skipped by name without a single byte being read.
- Each entry is skipped outright if its declared `file_size` exceeds 20 MB, and rejected with a clear
  `ClaudeFormatError` if `file_size`/`compress_size` implies a ratio over 100:1 once `file_size` is
  already above 1 MB - the classic zip-bomb signature, caught from the header alone.
- Every entry is read through `ZipFile.open(info).read(limit + 1)`, never `ZipFile.read(info)`, so a
  declared size the real stream disagrees with is still bounded; a 100 MB total budget is shared across
  every entry actually decompressed in one `collect()` call. In practice `zipfile` itself already
  CRC-validates a forged `file_size` and raises `BadZipFile` (caught the same way a merely corrupt zip
  is), so this is defense in depth rather than the only line of defense.

## Sources

- [S1] https://support.claude.com/en/articles/12123587 (Import and export your memory from Claude)
- [S2] https://support.claude.com/en/articles/9450526-how-can-i-export-my-claude-data
- [S3] https://github.com/lordjabez/claude-export-viewer/blob/main/CLAUDE.md
- [S4] https://github.com/bbiyani/ClaudeSearch/blob/main/EXPORT_FORMAT.md (measured 2026-08-05 and 2026-09-01)
- [S5] https://github.com/PDP-Connect/data-connectors/tree/main/connectors/anthropic/__fixtures__/split-export (keys match a real export of 2026-09-22)
- [S6] https://github.com/crownleo/ClaudeViewer/blob/main/README.en.md
- [S7] https://github.com/kalinplus/mem-adaptor/blob/main/docs/source-memory-formats.md
- [S8] https://github.com/sabercomo/ClaudeReader/blob/main/DATA_SCHEMA.md
- [S9] https://takeoutday.org/guides/how-to-export-claude-data (returns 403 to fetch; seen in search snippets only)
- [S10] https://github.com/nicolasmota/personal-context/blob/main/docs/guides/import-memories.md
- [S11] https://albertinemeunier.net/Claude_Memories_Conversion/en
- [S12] https://github.com/momoyu-bot/English-card (tools/手册/打捞机翻译版.md)
- [S13] https://aimemory.pro/blog/export-all-chatgpt-data ; https://memx.app/how-to/how-to-export-your-chatgpt-memory/
- [S14] https://www.notis.ai/blog/can-you-export-your-chatgpt-memory/ ; https://www.aichatexport.app/guides/chatgpt-data-export-what-is-included
- [S15] https://letsdatascience.com/news/openai-upgrades-chatgpt-memory-architecture-for-fresher-pers-b26b51d5 (cites the OpenAI post "Dreaming: Better memory for a more helpful ChatGPT", 2026-06-04)
- [S16] https://startupfortune.com/openai-makes-chatgpt-memory-more-active-and-harder-to-ignore/
- [S17] https://github.com/Justin3go/justin3go.com/blob/main/docs/posts/2026/06/04-agent-memory-architecture-guide.md
- [S18] https://github.com/mohamed-chs/convoviz/blob/main/docs/dev/chatgpt-spec.md
- [S19] https://github.com/hang-in/seCall/blob/main/docs/plans/secall-p9-chatgpt.md
- [S20] https://github.com/letta-ai/skills/blob/main/letta/importing-chatgpt-memory/references/chatgpt-export-notes.md
- [S21] https://github.com/trace-cortex/cortex-app/blob/main/docs/DATA_INGESTION_SURVEY.md
- [S22] https://github.com/savestatedev/savestate/blob/main/src/adapters/chatgpt.ts
- [S23] https://github.com/takano32/chatgpt-memory-distiller/blob/main/src/chatgpt_memory_distiller/chat.py
- [S24] https://github.com/moorcheh-ai/memanto/blob/main/examples/migrations/chatgpt-claude-okf/liberate.py
- [S25] https://github.com/daohoangson/chat-dl/blob/main/src/providers/chatgpt/models.ts ; https://github.com/pionxzh/chatgpt-exporter/blob/master/src/api.ts
