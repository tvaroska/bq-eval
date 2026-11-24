---
type: atom
up: "layers.md"
tags: [bq-eval, silver, clouds]
---

# More than one cloud

> Data stays in the cloud where it landed, and Silver is computed next to it. This note says what that means for the objects and for what may cross between clouds. Contract: [spec/silver.md](../spec/silver.md#clouds).

## One Silver per cloud

Every cloud that holds telemetry gets the same five objects, produced by the same SQL. The reference tables are small and are copied into each cloud. The hub is the cloud where the add-ons run.

## What may cross

| Data | May be read from the hub |
|---|---|
| `traces` rows | yes |
| `outcomes` rows | yes |
| Model, serving tier and token counts of model-call spans | yes, for cost only |
| Other span data | no |
| Prompt, context and answer text | no |

With per-trace rows from every cloud, the hub sees every trace by its ID. With model-call usage from every cloud, one price list in the hub can price all of them.

## A trace in two clouds

A trace can start in one cloud and call a model in another. Each cloud sees only its own spans.

- The cloud that holds the root span owns the trace and publishes its row from the spans it can see.
- The spans in the other cloud form a separate row, flagged as a fragment.
- The two are never merged.

Usage is not counted twice, because the fragment carries its own. The owning row describes only the part its cloud can see; errors and tool calls in the other cloud are not in its checks.

## Text

Text does not leave its cloud. Anything that needs the prompt or the answer, such as a judge, has to run where the text is. See [open questions](../spec/open-questions.md#evaluation-where-text-may-not-leave).
