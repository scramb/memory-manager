# Compliance templates

> **Not legal advice.** These templates are a starting point for an operator's own GDPR
> assessment of a `memory-manager` deployment. They describe what this codebase actually does
> - verified against the code, the ADRs and the threat model, not invented - but filling them in
> correctly, deciding whether they are complete for a given deployment, and any legal conclusion
> drawn from them is the operator's own responsibility. Have them reviewed by someone qualified
> to give legal advice for your jurisdiction and your deployment before relying on them.

## What is here

| Template | Covers |
|---|---|
| [`data-flow.md`](./data-flow.md) | Categories of personal data this server processes, the purpose of each, who/what receives them, a data-flow diagram, and an Art. 30 "record of processing activities" skeleton. |
| [`toms.md`](./toms.md) | Art. 32 technical and organisational measures (TOMs), mapped one-to-one to a concrete control and its config key or code path, plus the Art. 25 data-protection-by-default defaults this server ships with. |
| [`deletion-concept.md`](./deletion-concept.md) | How and when personal data is erased (Art. 17): the hard-delete/pseudonymization/audit-redaction scope per data category, the backup horizon, the `git`-backend limitation (no erasure of history), and the restore runbook for replaying `erasure_log` via `ERASURE_LOG_REPLAY_FILE`. |
| [`roles-and-permissions.md`](./roles-and-permissions.md) | Who may read, write, curate or administer which memory: the role × namespace-kind matrix, how roles are assigned and removed, the break-glass procedure and its audit trail, and operator access to the database. |
| [`dpia-template.md`](./dpia-template.md) | An Art. 35 Data Protection Impact Assessment: systematic description, necessity/proportionality, a risk table pre-filled from the threat model, the measures from `toms.md`, residual risk and sign-off fields. |
| [`transparency-notice.md`](./transparency-notice.md) | An Art. 13/14 notice addressed to employees, in plain language: what is stored, why, who can see it (including break-glass), how long it is kept, and their export/delete rights on `/account`. |
| [`germany.md`](./germany.md) | Optional: works-council co-determination under §87(1) no. 6 BetrVG (technical facilities suitable for monitoring performance or behaviour) and a works-agreement checklist. |

Every document describes the architecture as it stands in this repository (default `git` storage
backend, and the enterprise profile: `STORAGE_BACKEND=postgres` with Entra ID login, ADR-0006
through ADR-0009). Everything that depends on an *operator's own* choice - which fields apply to
their organisation, their retention policy, their sub-processors, their own data-protection
contact - is marked `<...>` for the operator to fill in. A placeholder left unfilled is not this
server's omission; it is the part only the operator can answer.

## How to use these templates

1. Read [`docs/security/threat-model.md`](../security/threat-model.md) first (STRIDE per trust
   boundary, with a mitigation or an accepted residual risk for every row) - these templates
   build on it rather than re-deriving the same facts. Where a control below needs more detail
   than one line gives, the threat model's matching flow is the deeper source.
2. Fill in every `<...>` placeholder in `data-flow.md` with your own controller details, your own
   sub-processors (which embedding provider, if any; your own SIEM/log-collector target; your
   backup storage location and provider) and your own retention decisions.
3. Use `toms.md` as evidence for Art. 32 "appropriate technical and organisational measures" -
   each row names the actual control and where it lives in this codebase, so a reviewer can check
   it against the running deployment rather than trusting the paragraph alone.
4. `deletion-concept.md` and `roles-and-permissions.md` build on `toms.md`'s own citations the
   same way: fill in the backup-retention value, the operator holding direct database access,
   and your own Git-backend erasure-request policy if you run that backend.
5. `dpia-template.md` and `transparency-notice.md` build on the first four the same way: the DPIA
   risk table re-frames the threat model's own findings by risk to the data subject, and the
   transparency notice turns `data-flow.md`'s categories and `roles-and-permissions.md`'s
   break-glass procedure into plain language for employees. `germany.md` is optional and layers on
   top of both if your organisation has a works council.

## Not included

- Translations of any of the seven templates.
- Legal review of any of the seven templates, or a determination of whether a DPIA is legally
  required for your specific deployment.
