---
id: 01KC96X9G0A5T1ZNQQDEBCYN0X
title: Deploy runbook
description: "Deploy steps for Leuchtturm: build, migrate, check Kompass, rollback."
type: reference
tags: [ops]
created: 2025-12-12T12:00:00Z
updated: 2025-12-12T13:00:00Z
---
1. Build the container image.
2. Run the database migration.
3. Check [[kompass]] for the deployment status.
4. If the error rate spikes, roll back to the previous image tag.
