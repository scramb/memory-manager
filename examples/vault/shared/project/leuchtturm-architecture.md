---
id: 01KEPETVG02MBP6WDXMX68ZZJG
title: Leuchtturm architecture
description: Overview of Leuchtturm's components and data flow.
type: project
tags: [project, leuchtturm, architecture]
created: 2026-01-11T12:00:00Z
updated: 2026-01-11T13:00:00Z
---
## Overview

Leuchtturm is [[nordlicht-gmbh]]'s internal analytics platform, see [[kompass]] for the companion ops dashboard.

## Components

An ingestion service, a Go API, and a Postgres/pgvector store.

## Data flow

Events are ingested, stored, then surfaced through the API that [[leuchtturm-mobile-app]] and the web UI both call.
