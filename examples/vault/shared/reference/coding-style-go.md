---
id: 01KGC6WBM04CA3VX7SH027JWQK
title: Go coding style guide
description: "Shared Go style: wrapped errors, table-driven tests, no naked returns."
type: reference
tags: [style, go]
created: 2026-02-01T09:00:00Z
updated: 2026-02-01T10:00:00Z
---
Wrap errors with context using '%w'. Prefer table-driven tests for anything with more than two cases. No naked returns in functions longer than a few lines.
