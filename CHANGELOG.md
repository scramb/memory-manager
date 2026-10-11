# Changelog

## [0.2.1](https://github.com/scramb/memory-manager/compare/v0.2.0...v0.2.1) (2026-10-11)


### Features

* **auth:** add personal tokens bounded by their owner and self-service on /account ([#308](https://github.com/scramb/memory-manager/issues/308)) ([4018330](https://github.com/scramb/memory-manager/commit/40183303412c1398f702b45e58238731d92593b8)), closes [#134](https://github.com/scramb/memory-manager/issues/134) [#135](https://github.com/scramb/memory-manager/issues/135)
* **cli:** add connect and doctor --client for Claude Code and claude.ai ([#312](https://github.com/scramb/memory-manager/issues/312)) ([f735762](https://github.com/scramb/memory-manager/commit/f73576228bc20b011a2f7e97558118e053997733)), closes [#137](https://github.com/scramb/memory-manager/issues/137) [#138](https://github.com/scramb/memory-manager/issues/138)
* **mcp:** add client compatibility profiles, tool annotations and compat lint ([#309](https://github.com/scramb/memory-manager/issues/309)) ([5ab3ecb](https://github.com/scramb/memory-manager/commit/5ab3ecb63b7c742bc86344ea206c888930bca5f9)), closes [#130](https://github.com/scramb/memory-manager/issues/130) [#131](https://github.com/scramb/memory-manager/issues/131) [#132](https://github.com/scramb/memory-manager/issues/132) [#133](https://github.com/scramb/memory-manager/issues/133) [#305](https://github.com/scramb/memory-manager/issues/305) [#306](https://github.com/scramb/memory-manager/issues/306)
* **mcp:** generate the usage rules and client instruction files from docs/memory-guide.md ([#307](https://github.com/scramb/memory-manager/issues/307)) ([e858fd6](https://github.com/scramb/memory-manager/commit/e858fd63472d6bf2379a720e584eddf8015712e6)), closes [#127](https://github.com/scramb/memory-manager/issues/127) [#128](https://github.com/scramb/memory-manager/issues/128)


### Documentation

* **docs:** add the client docs skeleton with the approved support matrix ([#304](https://github.com/scramb/memory-manager/issues/304)) ([a59ba3a](https://github.com/scramb/memory-manager/commit/a59ba3a2185fb0276b9258050ebff29edb6d1e13)), closes [#129](https://github.com/scramb/memory-manager/issues/129)
* **docs:** bring the README up to the v0.2.0 state ([#303](https://github.com/scramb/memory-manager/issues/303)) ([e31e049](https://github.com/scramb/memory-manager/commit/e31e0492ef668f3319cabd023bd634cb62aff9cf))
* **docs:** hand off the session after F-01 and v0.2.0 ([#302](https://github.com/scramb/memory-manager/issues/302)) ([843f34f](https://github.com/scramb/memory-manager/commit/843f34f419948611a925692d26e73c9985a705f8)), closes [#280](https://github.com/scramb/memory-manager/issues/280)
* **docs:** mark F-01 done with the v0.2.0 release ([#300](https://github.com/scramb/memory-manager/issues/300)) ([d1f5c49](https://github.com/scramb/memory-manager/commit/d1f5c49be6a7814e92e9b7baa8072fdd7959ef7a)), closes [#280](https://github.com/scramb/memory-manager/issues/280)

## [0.2.0](https://github.com/scramb/memory-manager/compare/v0.1.4...v0.2.0) (2026-10-10)


### Features

* **auth:** add quotas, an operator blocklist and SIEM audit export ([#286](https://github.com/scramb/memory-manager/issues/286)) ([4a0c62a](https://github.com/scramb/memory-manager/commit/4a0c62a4af37da524805a6b35cb9796eb63879c0))
* **auth:** deprovision Entra users through a Graph delta sync and revoke their credentials ([#290](https://github.com/scramb/memory-manager/issues/290)) ([57b600a](https://github.com/scramb/memory-manager/commit/57b600aefd621ec788887932133378895ec4a33a))
* **auth:** sign users in through Entra ID with roles, groups and refresh re-checks ([#284](https://github.com/scramb/memory-manager/issues/284)) ([70d0987](https://github.com/scramb/memory-manager/commit/70d0987431c39893cebb786932159c5b04147caf))
* **cli:** migrate a Git vault to Postgres and export it back ([#285](https://github.com/scramb/memory-manager/issues/285)) ([ae2f335](https://github.com/scramb/memory-manager/commit/ae2f33504b150be22c5ef6b9fcea4c0c4827339c))
* **deploy:** add the Entra OpenTofu module, a Flux enterprise example and a kind E2E ([#288](https://github.com/scramb/memory-manager/issues/288)) ([528d0f8](https://github.com/scramb/memory-manager/commit/528d0f8e58f217b2763075ffdf0659e195d8f6c7))
* **deploy:** add the Helm enterprise profile for the postgres backend ([#282](https://github.com/scramb/memory-manager/issues/282)) ([09e6ace](https://github.com/scramb/memory-manager/commit/09e6ace336ec42b96c6ac9a56ddb06fa0b025ad3))
* **index:** add the worker, a jobs outbox and the ADR-0016 vector layout ([#287](https://github.com/scramb/memory-manager/issues/287)) ([aeebea8](https://github.com/scramb/memory-manager/commit/aeebea8f2543a1b2c9df0b13d2aabb763bcff871))
* **mcp:** add memory_promote and the /account self-service page ([#289](https://github.com/scramb/memory-manager/issues/289)) ([67a69dd](https://github.com/scramb/memory-manager/commit/67a69dd4c9508ace104950c6fbec157a32431423))
* **observability:** trace requests into worker jobs and ship metrics, dashboards and alerts ([#292](https://github.com/scramb/memory-manager/issues/292)) ([219b9cd](https://github.com/scramb/memory-manager/commit/219b9cd03e7d38f4bef38a06b06777056515d356))
* **vault:** add erasure, retention, the admin area and break-glass access ([#294](https://github.com/scramb/memory-manager/issues/294)) ([2e65a5e](https://github.com/scramb/memory-manager/commit/2e65a5e733cbd8f05e1d308e665ba1956bc38c7e))


### Documentation

* cut F-01 M8–M11 into issues and record the owner decisions ([#281](https://github.com/scramb/memory-manager/issues/281)) ([1c9079a](https://github.com/scramb/memory-manager/commit/1c9079aede47505feef6b7b1fc89641a3133dd81)), closes [#125](https://github.com/scramb/memory-manager/issues/125)
* **docs:** add the 0.2 upgrade path and the README enterprise section ([#299](https://github.com/scramb/memory-manager/issues/299)) ([d04051a](https://github.com/scramb/memory-manager/commit/d04051a4d1b5557cd4c8edced244e17615b85da1))
* **docs:** add the threat model, pentest checklist and compliance pack ([#295](https://github.com/scramb/memory-manager/issues/295)) ([d1904d4](https://github.com/scramb/memory-manager/commit/d1904d4dd27f5429ce0972cf0d2de6482f6ef216))
* plan F-02 Client Integrations and accept its decisions ([#210](https://github.com/scramb/memory-manager/issues/210)) ([dd3ed9c](https://github.com/scramb/memory-manager/commit/dd3ed9c9c13ab9742dd2fb387b3574e4038c320c)), closes [#126](https://github.com/scramb/memory-manager/issues/126) [#159](https://github.com/scramb/memory-manager/issues/159) [#190](https://github.com/scramb/memory-manager/issues/190) [#196](https://github.com/scramb/memory-manager/issues/196) [#205](https://github.com/scramb/memory-manager/issues/205)

## [0.1.4](https://github.com/scramb/memory-manager/compare/v0.1.3...v0.1.4) (2026-10-08)


### Features

* **auth:** share rate limits and login state across replicas, drain on SIGTERM ([#113](https://github.com/scramb/memory-manager/issues/113)) ([9d2237e](https://github.com/scramb/memory-manager/commit/9d2237e8007452edb8a3cb1d37452eb0e93cb63b)), closes [#103](https://github.com/scramb/memory-manager/issues/103) [#104](https://github.com/scramb/memory-manager/issues/104) [#105](https://github.com/scramb/memory-manager/issues/105)
* **index:** enforce namespaces with RLS and an application-side matrix ([#121](https://github.com/scramb/memory-manager/issues/121)) ([52483ad](https://github.com/scramb/memory-manager/commit/52483addb350082c20341eac0b621c8cae1dae59)), closes [#100](https://github.com/scramb/memory-manager/issues/100) [#101](https://github.com/scramb/memory-manager/issues/101) [#102](https://github.com/scramb/memory-manager/issues/102) [#115](https://github.com/scramb/memory-manager/issues/115) [#116](https://github.com/scramb/memory-manager/issues/116) [#118](https://github.com/scramb/memory-manager/issues/118) [#119](https://github.com/scramb/memory-manager/issues/119)
* **vault:** add the Postgres storage backend for enterprise mode ([#114](https://github.com/scramb/memory-manager/issues/114)) ([25e8bf7](https://github.com/scramb/memory-manager/commit/25e8bf70e48449f98ac0081de661cd01ac56ea97)), closes [#96](https://github.com/scramb/memory-manager/issues/96) [#97](https://github.com/scramb/memory-manager/issues/97) [#98](https://github.com/scramb/memory-manager/issues/98) [#99](https://github.com/scramb/memory-manager/issues/99) [#112](https://github.com/scramb/memory-manager/issues/112)


### Documentation

* accept enterprise ADRs and plan F-01 Enterprise Scale ([#110](https://github.com/scramb/memory-manager/issues/110)) ([62cf611](https://github.com/scramb/memory-manager/commit/62cf6117fdc83420a6bd86fc1382095ede4b0bef)), closes [#93](https://github.com/scramb/memory-manager/issues/93)
* keep RLS variant R2 ([#140](https://github.com/scramb/memory-manager/issues/140)) ([f2b6e05](https://github.com/scramb/memory-manager/commit/f2b6e0551e5bf64d10ad4915aa469fcba9d3d172)), closes [#109](https://github.com/scramb/memory-manager/issues/109)
* record the state after 0.1.3 in TASKS and the handoff ([#90](https://github.com/scramb/memory-manager/issues/90)) ([30cc66a](https://github.com/scramb/memory-manager/commit/30cc66a5c185ababc10b31036f89d8ad1cf2bb9e)), closes [#89](https://github.com/scramb/memory-manager/issues/89)

## [0.1.3](https://github.com/scramb/memory-manager/compare/v0.1.2...v0.1.3) (2026-10-07)


### Documentation

* guide connecting claude.ai and Claude Code to a deployed server ([#86](https://github.com/scramb/memory-manager/issues/86)) ([a1dc957](https://github.com/scramb/memory-manager/commit/a1dc957584fa5ed36fe913761c791409609d5828)), closes [#40](https://github.com/scramb/memory-manager/issues/40) [#22](https://github.com/scramb/memory-manager/issues/22) [#47](https://github.com/scramb/memory-manager/issues/47)

## [0.1.2](https://github.com/scramb/memory-manager/compare/v0.1.1...v0.1.2) (2026-10-07)


### Bug Fixes

* **auth:** fall back across resolved addresses when fetching CIMD documents ([#83](https://github.com/scramb/memory-manager/issues/83)) ([bd3729d](https://github.com/scramb/memory-manager/commit/bd3729db61408b29042a11c1a1bbc443ac5e396d)), closes [#82](https://github.com/scramb/memory-manager/issues/82)

## [0.1.1](https://github.com/scramb/memory-manager/compare/v0.1.0...v0.1.1) (2026-10-07)


### Bug Fixes

* CIMD clients without scope and deploy keys without trailing newline ([#81](https://github.com/scramb/memory-manager/issues/81)) ([773e6ae](https://github.com/scramb/memory-manager/commit/773e6aefd426d9d4c6ddb31b323666ba4999a8f4)), closes [#80](https://github.com/scramb/memory-manager/issues/80) [#77](https://github.com/scramb/memory-manager/issues/77)


### Documentation

* record the v0.1.0 release state in TASKS and the handoff ([#78](https://github.com/scramb/memory-manager/issues/78)) ([6a9f507](https://github.com/scramb/memory-manager/commit/6a9f50713a92e6bd53c22a027af6bfc6b3703144)), closes [#40](https://github.com/scramb/memory-manager/issues/40)

## 0.1.0 (2026-10-07)


### Features

* **auth:** embedded OAuth server with OIDC/password login, CIMD, limits and audit (WP-11) ([#71](https://github.com/scramb/memory-manager/issues/71)) ([a3e0015](https://github.com/scramb/memory-manager/commit/a3e0015c7211a7b95351266cb51c1eab5d222299)), closes [#35](https://github.com/scramb/memory-manager/issues/35) [#36](https://github.com/scramb/memory-manager/issues/36) [#37](https://github.com/scramb/memory-manager/issues/37) [#38](https://github.com/scramb/memory-manager/issues/38) [#39](https://github.com/scramb/memory-manager/issues/39) [#40](https://github.com/scramb/memory-manager/issues/40)
* **cli:** import from Markdown, Claude and ChatGPT; export the vault (WP-14) ([#65](https://github.com/scramb/memory-manager/issues/65)) ([5e09513](https://github.com/scramb/memory-manager/commit/5e095131fea34c92a0adc7cf4d8c203c114b4003)), closes [#48](https://github.com/scramb/memory-manager/issues/48) [#49](https://github.com/scramb/memory-manager/issues/49) [#50](https://github.com/scramb/memory-manager/issues/50)
* **deploy:** Kustomize base, Helm chart and Flux example (WP-13) ([#70](https://github.com/scramb/memory-manager/issues/70)) ([5060559](https://github.com/scramb/memory-manager/commit/5060559d1be2e3bba5413dd1ddacbbc802fc3cc0)), closes [#44](https://github.com/scramb/memory-manager/issues/44) [#45](https://github.com/scramb/memory-manager/issues/45) [#46](https://github.com/scramb/memory-manager/issues/46) [#47](https://github.com/scramb/memory-manager/issues/47)
* **index:** derived Postgres index with chunks and embeddings (WP-07) ([#60](https://github.com/scramb/memory-manager/issues/60)) ([33a5242](https://github.com/scramb/memory-manager/commit/33a52424916d82bfcbf6cb5de9bf9d23acec774c)), closes [#24](https://github.com/scramb/memory-manager/issues/24) [#25](https://github.com/scramb/memory-manager/issues/25) [#26](https://github.com/scramb/memory-manager/issues/26) [#27](https://github.com/scramb/memory-manager/issues/27)
* **mcp:** memory tools over stdio with instructions and search (WP-05) ([#66](https://github.com/scramb/memory-manager/issues/66)) ([bec7fee](https://github.com/scramb/memory-manager/commit/bec7fee222f7b278c724ad241e92ecae2c616cc7)), closes [#17](https://github.com/scramb/memory-manager/issues/17) [#18](https://github.com/scramb/memory-manager/issues/18) [#19](https://github.com/scramb/memory-manager/issues/19) [#20](https://github.com/scramb/memory-manager/issues/20) [#21](https://github.com/scramb/memory-manager/issues/21) [#30](https://github.com/scramb/memory-manager/issues/30)
* **mcp:** Streamable HTTP with static scoped tokens (WP-10) ([#68](https://github.com/scramb/memory-manager/issues/68)) ([ab2b169](https://github.com/scramb/memory-manager/commit/ab2b169d39c64344396869715d3c08c57af7eff3)), closes [#33](https://github.com/scramb/memory-manager/issues/33) [#34](https://github.com/scramb/memory-manager/issues/34)
* **queue:** serialized write queue with conflict handling (WP-04) ([#62](https://github.com/scramb/memory-manager/issues/62)) ([048fd27](https://github.com/scramb/memory-manager/commit/048fd2704d0cb8d262d56aebffb4d162b82d2696)), closes [#14](https://github.com/scramb/memory-manager/issues/14) [#15](https://github.com/scramb/memory-manager/issues/15) [#16](https://github.com/scramb/memory-manager/issues/16)
* **search:** hybrid full-text and vector search with RRF (WP-08) ([#61](https://github.com/scramb/memory-manager/issues/61)) ([d273262](https://github.com/scramb/memory-manager/commit/d27326203998f7f99afc1d3b62c189f82e857ff8)), closes [#28](https://github.com/scramb/memory-manager/issues/28) [#29](https://github.com/scramb/memory-manager/issues/29)
* security review, signed release pipeline and v0.1.0 README (WP-15) ([#72](https://github.com/scramb/memory-manager/issues/72)) ([523b941](https://github.com/scramb/memory-manager/commit/523b941ae497448f5c585c2109a1dbcee062f086)), closes [#51](https://github.com/scramb/memory-manager/issues/51) [#52](https://github.com/scramb/memory-manager/issues/52) [#53](https://github.com/scramb/memory-manager/issues/53)
* **vault:** git-backed vault with sync and secret scanning (WP-03) ([#59](https://github.com/scramb/memory-manager/issues/59)) ([8afe567](https://github.com/scramb/memory-manager/commit/8afe56767270d2fb3f1b9613064b94fe90eb3f87)), closes [#11](https://github.com/scramb/memory-manager/issues/11) [#12](https://github.com/scramb/memory-manager/issues/12) [#13](https://github.com/scramb/memory-manager/issues/13)
* **vault:** note model with parser, validation, path safety and links (WP-02) ([#58](https://github.com/scramb/memory-manager/issues/58)) ([e025197](https://github.com/scramb/memory-manager/commit/e02519738f29ca51e4bd20d7df1350559a455c8f)), closes [#6](https://github.com/scramb/memory-manager/issues/6) [#7](https://github.com/scramb/memory-manager/issues/7) [#8](https://github.com/scramb/memory-manager/issues/8) [#9](https://github.com/scramb/memory-manager/issues/9) [#10](https://github.com/scramb/memory-manager/issues/10)


### Documentation

* add planning skeleton and accepted ADRs 0001-0004 ([4999d27](https://github.com/scramb/memory-manager/commit/4999d27302e2d8ad023a2c3644810b3863fa7c5d))
* Claude Code skill, CLAUDE.md snippet and setup guide (WP-06) ([#67](https://github.com/scramb/memory-manager/issues/67)) ([ff27dda](https://github.com/scramb/memory-manager/commit/ff27ddab18362cc4d2ecacf9a45408d2a2e7c691)), closes [#22](https://github.com/scramb/memory-manager/issues/22) [#23](https://github.com/scramb/memory-manager/issues/23)
* mirror GitHub issues [#1](https://github.com/scramb/memory-manager/issues/1)-[#53](https://github.com/scramb/memory-manager/issues/53) and record the deployment decision ([09f3322](https://github.com/scramb/memory-manager/commit/09f3322ea49f4a557c71ed0f1ba341349dbef899)), closes [#3](https://github.com/scramb/memory-manager/issues/3)
* **research:** drop operator-specific names from the bring reference ([b070d67](https://github.com/scramb/memory-manager/commit/b070d6717493ec94d6c43afdbeff9ee56b366b71))
* **research:** record MCP spec, connector, SDK and Hydra findings ([2dcdd70](https://github.com/scramb/memory-manager/commit/2dcdd70e7620116aefa354805b932ae0330432f2))
