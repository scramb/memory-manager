Date: 2026-10-10 · Branch: main · Last commit: d1f5c49 docs(docs): mark F-01 done with the v0.2.0 release (#300)

## Done

- F-01 (Enterprise Scale, M7–M11, WP-16 … WP-34) is done. Every M8–M11 box in `docs/TASKS.md` is ticked and every WP header carries its merged PR.
- v0.2.0 is released (tag `v0.2.0`, release-please PR #211). Both `cosign verify` commands from `docs/releasing.md` succeed:
  - image `ghcr.io/scramb/memory-manager@sha256:8a6af1f3bd72a4456b70afcef1ca401121b516b714aaf1002d218196bd05729c` (amd64 + arm64)
  - chart `ghcr.io/scramb/charts/memory-manager@sha256:706e5478f20435cda8d05272246cb6f6ce69843d99812aa7b4379f3df6a1e875`
  - Anonymous image pull and `helm pull` work.
- F-01's latency targets at 1M notes / 5M chunks are **not met**. Owner decision 2026-10-10: document the outcome and finish F-01; the follow-up is #297. The numbers and the measured cause are in `docs/benchmarks/target-size.md`.
- Owner-accepted deviations, recorded in `docs/PLAN.md` and F-01:
  - The target-size run used single-node kind instead of an operator cluster (O24).
  - In postgres mode, retention defaults on, with `PERSONAL_RETENTION_DAYS`=30 (ADR-0008).
- Last session's open item #89 (uv.lock bump) is closed.

## In progress

- Nothing. Main is clean, with no extra worktrees or local branches.
- Release-please already keeps the next standing release PR open (#301 "release 0.2.1"); leave it until there is something to release.

## Failed / dead ends

- `mm_frequent_lexemes` on the partitioned `chunks` table read the parent's stats. Autovacuum never analyzes a partitioned parent, so frequent terms went undetected, and the fulltext leg outranked the correct vector hit in RRF. Fixed in #293 by migration `postgres/0024`. Never trust parent-level `pg_stats` on a partitioned table.
- Unfiltered vector searches had no indexable predicate. The RLS `exists` policy gives the planner none, so every request computed an exact distance for every chunk in `chunks_user`. Fixed in #296 by filtering `c.namespace` on `mm_readable_ns()` when an identity is set.
- `loadtest/load.py` with `Executor.map` had no backpressure. At 1M notes the host OOM-killed the loader at about 39 GiB RSS. It now uses a bounded window (`_windowed`), and the Job has a memory limit.
- On an existing cluster, `scripts/loadtest-cluster.sh` uninstalls the release on exit, which destroyed a 2-hour dataset once. Set `KEEP_RELEASE=1` on every run except the last; see `docs/benchmarks/cluster-loadtest.md`.
- `scripts/check-deploy-placeholders.sh` treats dotted names such as `memory.current` as hostnames. Keep such names out of the manifests under `loadtest/k8s/` and `deploy/`, comments included.
- Enterprise `token create` needs `--expires-days`, and an owner who already has a `users` row. Only an Entra login writes that row; `oidc` and `password` logins do not.
- Subagent poll loops built on `pgrep -f "<pattern>"` match their own command line and never end. Use `run_in_background` and wait for the completion notification instead.
- Chained shell commands (`cmd && commit; gh issue close`) closed an issue even though the commit had failed. Keep the commit and the issue close in one `&&` chain.
- Still valid from earlier sessions:
  - Storing a private key via `$(cat key)` strips the trailing newline, and OpenSSH then fails with `error in libcrypto`.
  - Tags and PRs that release-please creates with GITHUB_TOKEN trigger no other workflows, so it dispatches `release.yml` and `validate.yml` itself.
  - A closed release PR keeps its `autorelease: pending` label; remove it before regenerating.
  - A CDN in front of the server blocks the `Python-urllib` user agent.

## Next step

- #297: hybrid search should meet F-01's latency targets at 1M notes / 5M chunks.
  - Start by profiling one hybrid search per leg with `EXPLAIN (ANALYZE, BUFFERS)` (`search.py` `_vector_search_legs`).
  - Then write an ADR-0016 addendum.
  - Verify with a rerun of `scripts/loadtest-cluster.sh` at `LOADTEST_NOTES=1000000` for both shared-state modes.
  - #297 has no work package or milestone yet; plan it with `feature-planning` before starting.
- F-02 (M12–M17) can start: O32 decided F-01 first, then F-02.
- Rolling 0.2.0 out to the owner's deployment is a tag bump in the operator overlay, which lives outside this repo. It is only done on the owner's request.

## Open questions

- None blocking. #297 needs a work package before work starts.
