# Employee transparency notice (Art. 13/14 GDPR) — template

> **Not legal advice.** This is a template notice an operator adapts and gives to employees before
> or when they start using a `memory-manager` deployment, to satisfy GDPR Art. 13/14's
> transparency obligation. It describes what this codebase actually stores and who can see it -
> verified against the code, the ADRs and [`docs/security/threat-model.md`](../security/threat-model.md),
> not invented. It does not decide whether this notice is complete for your organisation, what
> your lawful basis is, or how you deliver it (intranet page, onboarding email, works agreement
> appendix, ...). See [`README.md`](./README.md) for how to use this set of templates, and have
> the filled-in result reviewed by someone qualified to give legal advice for your jurisdiction
> before giving it to employees.

---

*The section below this line is written for the employee reading it, in plain language. Fill in
every `<...>` with your own organisation's details before publishing it.*

---

## About this notice

`<Organisation name>` uses `memory-manager`, a note-taking and memory tool for Claude, to let you
and your team store things Claude should remember across conversations - decisions, facts,
preferences, project context. This notice explains what personal data it stores about you, why,
who can see it, how long it is kept, and what rights you have over it.

Data controller: `<name and contact details>`.
Data protection contact / DPO: `<name and contact details, if any>`.

## What is stored about you

| What | Example | Why it exists |
|---|---|---|
| **The content you write** | Whatever you or Claude saves to a note on your behalf - this may include personal facts you chose to record, especially in your own personal memory | This is the point of the tool: giving Claude something to remember |
| **Who wrote it** | Your identity is recorded as the author of every note revision you create | Attribution, and so a shared note's history is traceable |
| **Your identity** | Your Microsoft Entra account ID, display name, and whether your account is currently enabled | Signing you in and keeping access in sync with your employment status |
| **Your group membership** | Which Entra groups you belong to, cached for a limited time | Deciding which shared team memory you can read or write |
| **Login credentials, as hashes only** | A one-way cryptographic hash of your login session or access token - never the token or password itself | Recognising you on your next request without storing anything that could be used to impersonate you if the database were ever read |
| **A record of security-relevant actions** | Who did what, when, and whether it succeeded - never the content of what you wrote | Investigating incidents and demonstrating that access controls work |
| **Technical traces (if enabled)** | Which operation ran, how long it took, by which client - never your note content or your credentials | Keeping the service reliable |

What is **not** stored beyond what you write yourself: this tool does not scan your notes for
anything beyond a security check (looking for accidentally pasted secrets, such as API keys), and
it never treats what you write as instructions to follow - your notes are data, read back to
Claude, never commands the system executes.

## Who can see your personal memory

- **Your own personal memory (`me` namespace) is visible only to you**, by default - not to other
  employees, not to your manager, and not to an administrator, even one with the "admin" role.
  Administrators manage team namespaces and access lists; that role by itself gives **no** access
  to anyone's personal memory.
- **The one exception is "break-glass" access**: an administrator can request **temporary,
  read-only** access to your personal memory, with a stated reason. By default this needs a
  **second**, different administrator's approval, and the access automatically expires after
  **one hour**. Every request, approval and individual view is logged. **You are always told**:
  you see a notice on your account page, and a record of it is written into your own memory, so
  you find out even if you never check proactively.
- **Shared memory** (a team, a project, or company-wide space) is visible to the people and groups
  your organisation has given access to that space - the same way a shared document would be.
  Ask `<your administrator / IT contact>` who currently has access to a given shared space if
  you are unsure.
- **An administrator with direct database access** (an IT operations role, not the application's
  own admin role) can, in principle, read anything in the database directly - this is the same
  level of access any database administrator has over any company system, and it is outside what
  the application itself can restrict. `<name who holds this access in your organisation>`.

## How long your data is kept

- **While you are an active employee**, your personal and shared memory is kept for as long as the
  service is in use - there is no automatic time limit on your own notes, because remembering
  things is the point of the tool.
- **If you leave or are deprovisioned**, your personal memory is automatically frozen and then
  permanently deleted **`<30 days by default - confirm your organisation's configured value>`**
  after your account is disabled, without anyone needing to request it.
- **Security and access logs** are kept for `<your organisation's own retention period>`, and
  records of any deletion are kept at least as long as backup copies exist
  (`<your organisation's backup retention, plus a 7-day margin>`), so that a deletion can be
  proven even after a backup restore.
- **Backup copies** of the whole system are kept for `<your organisation's own backup retention -
  default 30 days>` before they themselves are deleted.

## What happens when you leave

When your account is disabled (detected automatically from the company directory, usually within
minutes), you lose access immediately. Your personal memory is not deleted immediately - it is
kept for a limited grace period (see above) in case this was a mistake, then permanently deleted.
Anything you contributed to a **shared** team space is **not** deleted when you leave, because the
team owns that content - only your name on it is replaced with "erased" so it is no longer
attributable to you personally.

## Your rights

- **See what you have stored:** everything in your personal memory is visible to you at any time
  by asking Claude, or by using the "export" function on `<your organisation's account page URL>`,
  which gives you a downloadable copy of your personal memory.
- **Delete your own memory:** the same account page has a "delete my memory" option, which
  permanently and irreversibly deletes your personal memory and, if your account is also removed,
  your identity record. This is a genuine, permanent deletion, not an archive - it cannot be
  undone by `<your organisation>` once confirmed.
- **Correct your own memory:** since you (and Claude, acting on your behalf) write your own
  personal memory, correcting it is as simple as writing an updated note - there is no separate
  correction process needed for your own content.
- **Ask about shared content:** if a note in a shared space contains personal data about you that
  you believe is incorrect or should be removed, contact `<your administrator / data protection
  contact>` - shared content is not something you can delete yourself, since others may depend on
  it.
- **Complain:** you have the right to raise a concern with `<your organisation's data protection
  contact>` and, if unresolved, with `<your supervisory authority>`.
- `<Add: if your organisation has a works council, point employees to docs/compliance/germany.md`
  or your own equivalent, and to any works agreement covering this tool>`.

## Questions

Contact `<your organisation's data protection contact>` with any question about this notice or
about your own data.

---

*End of the employee-facing section. The remainder of this file is for the operator filling it
in, not for employees.*

---

## Operator notes (not part of the notice itself)

- This notice is written to be handed to employees directly, in plain language, deliberately
  avoiding GDPR article numbers and code references in the employee-facing section above - they
  are listed here for the operator filling in the template, to make it checkable against the
  source:
  - "The content you write" / "Who wrote it": [`data-flow.md`](./data-flow.md) "Categories of
    personal data processed" (Note content, Authorship).
  - "Your identity" / "Your group membership": `data-flow.md` (Identity, Group membership cache);
    enterprise only.
  - "Login credentials, as hashes only": `data-flow.md` (Credentials); CLAUDE.md "token hashes
    only".
  - "A record of security-relevant actions": `data-flow.md` (Audit metadata); `audit.py`'s own
    docstring, "never note content".
  - "Technical traces": `data-flow.md` (Observability); off unless `OTEL_EXPORTER_OTLP_ENDPOINT`
    is set.
  - "Break-glass" paragraph: [`roles-and-permissions.md`](./roles-and-permissions.md)
    "Break-glass: an admin reading a user's personal namespace" (request, four-eyes approval by
    default, 1-hour expiry, read-only viewer never reachable from MCP, audited, user notified via
    banner + `reference` note) - implemented on `main` (WP-26); see that document and the threat
    model's Flow 8 "I" row for the residual risk on how the approver count reaches SQL.
  - "If you leave or are deprovisioned": [`deletion-concept.md`](./deletion-concept.md)
    "Erasure scope" and "Deprovisioning via Graph delta sync" (`PERSONAL_RETENTION_DAYS`, default
    30 days).
  - "Anything you contributed to a shared team space is not deleted": `deletion-concept.md`
    "Erasure scope" row "Authorship... Pseudonymization".
  - "Security and access logs... records of any deletion": `deletion-concept.md` "Backup horizon"
    (retention + 7 days).
  - "export" / "delete my memory": ADR-0008 Decision, "Self-service" (`/account` "export my
    memory", "delete my memory"); `deletion-concept.md` Scope.
  - "An administrator with direct database access": `roles-and-permissions.md` "Operator access
    to the database".
- Fill in every `<...>` before publishing. In particular: your organisation name, your data
  protection contact, your configured `PERSONAL_RETENTION_DAYS` and backup retention values (see
  [`toms.md`](./toms.md) and `deletion-concept.md` for where these are configured), your account
  page's actual URL, and - if applicable - a pointer to [`germany.md`](./germany.md) or your own
  jurisdiction's equivalent.
- If your deployment runs the default `git` storage backend instead of `postgres`, several
  paragraphs above do not apply: there is no break-glass, no self-service export/delete, and no
  automatic deprovisioning-triggered deletion (see `deletion-concept.md` "Git-backend
  limitation"). Rewrite this notice for that case rather than leaving inapplicable paragraphs in.

## Not included

- A Data Protection Impact Assessment - see [`dpia-template.md`](./dpia-template.md).
- A Germany-specific works council / co-determination section - see [`germany.md`](./germany.md).
- Legal review of this template, or a determination of how this notice must be delivered under
  your jurisdiction's own Art. 13/14 timing and form requirements.
