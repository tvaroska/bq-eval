---
type: root
down:
  - "layers.md"
tags: [bq-eval]
---

# BQ Eval

> Agents fail quietly: the request succeeds and the answer is wrong, slow or expensive. BQ Eval reads the traces agents already emit, where they already land, and turns them into a small set of tables that evaluation, monitoring and cost can be built on.

**Why it exists.** Teams that run agents already collect traces. What they lack is a shared, checked view of those traces that does not require a new pipeline, a new store, or code in every agent.

**How it works.** Three layers. Bronze is the telemetry, left in place. Silver is the core: SQL in BigQuery that normalizes every source into the same objects and runs rule checks on every trace. Gold is a set of add-ons that read Silver. See [layers](layers.md).

**What is in these notes.** The requirements are in [spec/](../spec/layers.md). These notes describe how the design meets them. Status is kept out of both; it belongs in the [roadmap](../docs/roadmap.md). The SQL that ships today is described in [architecture and SQL](../docs/architecture-and-sql.md).
