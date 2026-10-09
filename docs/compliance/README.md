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

Both documents describe the architecture as it stands in this repository (default `git` storage
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
4. Neither template substitutes for a Data Protection Impact Assessment, a transparency notice to
   data subjects, or a deletion/retention concept tailored to your organisation's roles - those
   are tracked separately (issues #275, #276) and are explicitly out of scope here.

## Not included

- Deletion concept, roles and permissions beyond what `toms.md` cites as existing controls (#275).
- DPIA, transparency notice, a Germany-specific section (#276).
- Legal review of either template.
