# Small local model (7-8B via Ollama): does the filter mode help?

Measured: 2026-10-11 (local run) · Feeds: [#151](https://github.com/scramb/memory-manager/issues/151) · Work package: WP-44 · Feature: F-02

F-02's own claim: "with a small local model (7-8B via Ollama) the filter mode (#150)
demonstrably gives usable answers." This note records the first measurement of that
claim, against the fictional `examples/vault` and
[`tests/e2e/clients/small_model_questions.yaml`](../../tests/e2e/clients/small_model_questions.yaml)'s
24 questions, run with the filter on and with it off.

Threshold (#151's own Definition of Done): filter-on hit rate >= 0.7, and strictly
better than filter-off. Owner decision 2026-10-11 (via harness): report honestly - if
a run misses either threshold, the numbers are kept, the workflow stays red, and this
is reported, never tuned after the fact (not the valves, not the question set, not the
prompt).

## Hardware and software profile

- 8 vCPU (QEMU Virtual CPU version 2.5+, 1 thread/core), 47 GiB RAM - the same shared
  build/dev VM `docs/benchmarks/baseline.md` describes. **No AVX/AVX2** (`/proc/cpuinfo`
  carries neither flag): Ollama's CPU backend falls back to its slowest generic kernels,
  which is the main reason every latency number below is measured in tens of seconds to
  low minutes, not the handful of seconds a modern laptop CPU (or any GPU) would need.
- Podman 5.4.2, `podman compose` via `podman-compose` 1.3.0.
- Ollama `0.40.2` (`deploy/openwebui/docker-compose.yml`'s own pinned tag), Open WebUI
  `0.11.4` (`Makefile`'s own `OPENWEBUI_VERSION`).
- Model: `qwen2.5:7b-instruct-q4_K_M` (Apache-2.0, Qwen2.5 model family), manifest digest
  `sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`, retrieved
  2026-10-11 from Ollama's own library. The local run's own `ollama list` reported id
  `845dbda0ea48`, a prefix of that digest - checked live by the test itself
  (`_verify_model_digest`), not only asserted here.
- Stack: `deploy/openwebui/docker-compose.yml` plus
  [`deploy/openwebui/examples-vault-overlay.yml`](../../deploy/openwebui/examples-vault-overlay.yml)
  (seeds the vault "remote" with a verbatim copy of `examples/vault` instead of the base
  file's single welcome note, raises Open WebUI's own HTTP client timeout, and turns on
  its Ollama API) plus the `ollama` compose profile.
- Filter valves: the *shipped defaults* - `max_notes=5`, `token_budget=400`,
  `outlet_enabled=false` - never tuned for this measurement.

## Method

One admin user (Open WebUI's first signup), the memory-manager filter
(`integrations/openwebui/filter_memory.py`) installed and made global, built-in memory
disabled, one personal token (`memory-manager token create --kind personal --owner ...
--role Memory.User`, scope `memory:read` only, no `--namespace` - the overlay's own
`LOGIN_NAMESPACES=*` is what lets this one token read every namespace
`examples/vault` uses) set as that admin's own `UserValves.token`.

Per question: one direct `memory_search` call (the filter's own query/limit) as a
diagnostic - "fact in context" - whether the filter's own search would even surface the
fact among its top `max_notes` hits, independent of what the model then does with it;
then one real chat turn with the filter on (`temperature=0`, `seed=42`,
`max_tokens=220`); then, once every question has run once this way, `Filter.Valves.
enabled=false` and the same 24 questions again, filter off. A fact counts as present in
an answer by a case-insensitive, umlaut-folded substring match against any of its
`expected_any` variants - checked once, before any of this runs, that no such variant is
itself a substring of its own question.

Reproduce:

```
MM_E2E_OPENWEBUI_SMALL_MODEL=1 uv run pytest tests/e2e/clients/test_openwebui_small_model.py -v
```

Results are written to `tests/e2e/clients/.small-model-results/results.json` and
`results.md` (gitignored).

## Results - local run, 2026-10-11

**Local measurement only** - the CI run's own numbers (`.github/workflows/
nightly-openwebui.yml`) are added below once that workflow has run on this PR.

- **on rate: 0.833 (20/24)**
- **off rate: 0.042 (1/24)**
- mean generation rate (filter on): ~2.36 Ollama output tokens/s

Both thresholds are met by a wide margin: 0.833 >= 0.7, and 0.833 is far above 0.042.

| id | lang | source | fact in context | on hit | off hit | on latency (s) | off latency (s) |
|---|---|---|---|---|---|---|---|
| q01 | de | `personal/fact/birthday.md` | yes | yes | no | 128.4 | 35.0 |
| q02 | de | `personal/fact/lives-in-hamburg.md` | yes | yes | no | 84.9 | 20.7 |
| q03 | de | `personal/fact/owns-a-bike.md` | yes | yes | no | 96.1 | 24.1 |
| q04 | en | `personal/fact/speaks-german-and-english.md` | yes | yes | no | 84.5 | 46.3 |
| q05 | de | `work/fact/manager-is-tobias.md` | yes | yes | no | 98.0 | 44.3 |
| q06 | de | `work/fact/office-location.md` | yes | yes | no | 82.8 | 39.5 |
| q07 | en | `work/fact/tech-stack.md` | yes | yes | no | 86.5 | 33.8 |
| q08 | de | `shared/fact/company-founded.md` | yes | yes | no | 96.9 | 44.8 |
| q09 | de | `shared/fact/employee-count.md` | yes | no | no | 96.6 | 55.2 |
| q10 | en | `shared/fact/product-names.md` | yes | yes | no | 71.4 | 37.9 |
| q11 | en | `shared/fact/product-names.md` | yes | yes | no | 62.1 | 35.0 |
| q12 | de | `personal/reference/favorite-coffee-shop.md` | yes | yes | no | 94.0 | 29.5 |
| q13 | de | `personal/reference/cat-keks.md` | yes | yes | no | 86.2 | 30.8 |
| q14 | en | `personal/reference/favorite-book-list.md` | no | no | no | 95.8 | 56.3 |
| q15 | en | `personal/user/communication-style.md` | yes | yes | no | 63.7 | 33.4 |
| q16 | de | `personal/user/dietary-notes.md` | yes | yes | no | 97.9 | 26.8 |
| q17 | de | `personal/user/timezone-and-hours.md` | no | no | no | 102.2 | 129.7 |
| q18 | de | `work/user/standup-time.md` | yes | yes | no | 90.4 | 37.2 |
| q19 | en | `work/user/slack-notification-prefs.md` | yes | yes | no | 90.0 | 29.5 |
| q20 | en | `work/reference/team-roster.md` | yes | yes | no | 89.0 | 42.3 |
| q21 | en | `work/reference/oncall-rotation.md` | yes | yes | yes | 79.1 | 52.6 |
| q22 | en | `shared/user/team-working-agreement.md` | yes | no | no | 85.4 | 90.5 |
| q23 | de | `personal/user/editor-preference.md` | yes | yes | no | 76.6 | 42.4 |
| q24 | en | `shared/reference/api-auth-guide.md` | yes | yes | no | 102.0 | 229.5 |

### CI run

Filled in by the harness once `nightly-openwebui.yml` has run on the PR that introduces
this file (its own artifact upload carries the same `results.json`/`results.md` shape).

## Limitations

- **One run, one host, no repeated trials.** Same caveat `baseline.md` already states
  for its own single-host measurement; `temperature=0`/a fixed `seed` make a rerun on
  the *same* hardware and model build close to reproducible, but say nothing about
  variance across hardware or across an upstream model update.
- **Substring scoring has both false negatives and a rare false positive.** q09's "on"
  answer ("ungefähr 40 Mitarbeiter") and q17's ("9-13 und 20-22 Uhr") are factually
  correct but score as misses because the frozen `expected_any` list did not anticipate
  those exact paraphrases ("40 Mitarbeitende" vs "40 Mitarbeiter"; "9 bis 13" vs "9-13") -
  the real on-rate is very likely a little higher than 0.833. q21's "off" answer scores as
  a hit only because its generic, unsourced boilerplate happens to mention "Monday" in
  passing ("Monday to Friday") - not a real recall of the on-call note. Per the owner
  decision above, the frozen question set is not touched to fix either direction now
  that it has been scored once; a future question-set revision could note this as a
  known class of scoring noise.
- **No AVX/AVX2 on the measuring host.** Every latency/throughput number here (~2.3-2.7
  output tokens/s, ~65-230s per real chat turn) reflects this specific VM's own,
  deliberately slow CPU inference path - not a statement about what this model needs on
  typical consumer hardware (any machine with AVX2, or a GPU, would be substantially
  faster).
- **One model, one quantization, one question set.** Neither a model/quantization
  comparison nor a version matrix (#156) is in scope here.
- **Tool mode and embeddings are out of scope for this measurement** (`Nicht dabei`):
  this is the filter (`inlet`/`outlet`, lexical FTS search) only.
