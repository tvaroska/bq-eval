---
type: atom
related:
  - "silver.md"
  - "attributes.md"
  - "open-questions.md"
  - "../design/silver.md"
tags: [bq-eval, silver, telemetry]
---

# Telemetry shapes

> The forms of telemetry Silver accepts, and how each framework or gateway is set up to produce one of them. Silver reads these and nothing else.

## What a shape is

A shape is one family of attribute names in one placement. The same names in a second placement count as a second shape.

Silver accepts at most three shapes in BigQuery SQL. A new shape needs a named use case and a recorded trace, and it has to replace one of the three.

## The three shapes

| Shape | Names | Where the content is |
|---|---|---|
| **ADK's own** | `gcp.vertex.agent.*` | on the step |
| **General** | the OpenTelemetry GenAI conventions | on the step |
| **General, by reference** | the OpenTelemetry GenAI conventions | in storage; the step carries the address |

ADK's own shape is what ADK writes in its older trace format. ADK plans to remove that format, so this shape is expected to be replaced.

## Content is required

Every shape carries content, on the step or by reference. A model or tool call that arrives without content is kept: it stays in the text-free objects (`model_calls`, `tool_calls`, `traces`) with a content-missing fact, it has no text row, evaluation does not score it, and cost and monitoring count it. Silver defines no zero-payload profile (`ADR-036`).

The shape says how content is read. The kind of a span is read from its operation name, so spans that carry no content of their own (agent and workflow spans, embedding calls) are still placed, and spans with no GenAI operation (HTTP, database) are ordinary spans: kept, counted, not flagged.

## Content by reference

Prompts, answers, system instructions, tool definitions, and encoded files carried in messages (documents, images, audio) may arrive by reference. The content sits in storage in the same cloud, location, and CMEK perimeter as the trace, and the step carries its address. Silver follows message addresses, while source documents and media passed by storage URI (such as multi-hundred-page PDFs) stay in storage in place and expose their URIs on `trace_payloads`.

An address whose content is missing is counted, like any other missing value, and the text part is marked as not read whole.

### When content must go by reference

An application where one prompt, answer, or tool result can exceed the trace store's per-attribute limit (`65,532` bytes exact measured cap; `65,533+` bytes silently drops the entire span in an `HTTP 200` `partialSuccess` batch rather than truncating, and span `events` enforce the same cap — see [open-questions.md](open-questions.md#tests-on-the-trace-store)) uses the shape that carries content by reference.

### Upload contract

Any writer of content by reference meets this contract:

| Part | Rule |
|---|---|
| Storage & partitioning | Written to a **BigLake-backed** storage prefix partitioned by Hive date/hour (`dt=YYYY-MM-DD/hr=HH/...`), in the same cloud, BigQuery location, and CMEK perimeter as the trace. **Micro-batched Parquet/Iceberg is the canonical production format** at scale; JSON Lines (`jsonl`) matching the OpenTelemetry helper layout is permitted strictly for low-volume dev/prototype setups. Flat unpartitioned per-call JSONL joined dynamically by `_FILE_NAME` is prohibited at scale because BigQuery scans 100% of files on dynamic joins. |
| Addresses | The step carries the four standard reference attributes: `gen_ai.input.messages_ref`, `gen_ai.output.messages_ref`, `gen_ai.system_instructions_ref`, `gen_ai.tool.definitions_ref`. |
| Risk-tiered delivery | Uploads run **asynchronously** off the interactive request hot path (via a bounded Collector/sidecar queue with explicit drop counters) so storage backpressure never stalls live requests. For Tier-1 regulated state writes (`payload_profile = 'regulated_worm'`), audit enqueue fails closed at the tool/gateway Port before mutation executes. |
| Failure & aborted turns | A failed upload leaves the address on the step so Silver counts missing content. Tool execution errors and execution-time `bq_eval.args_sha256` are emitted directly on `execute_tool` spans so aborted turns without a subsequent model call never lose tool errors or hashes. |

BQ Eval ships one reference asynchronous hook/adapter meeting this contract. The official helper's default `upload` hook drops silently without counters or Hive partitioning and does not meet it.

### Gateway Inline-Artifact & Guardrail Port (`UC3`, `UC4`, `UC11`)

Interactive multi-megabyte inline uploads (for example, `100 MB` attachments arriving directly at a central LLM gateway rather than pre-staged in object storage) **must never be buffered inside an in-process SDK telemetry hook**. Instead, the gateway implements a **Gateway Inline-Artifact & Guardrail Port**:
- Streams raw attachments directly to storage via pre-signed private-connectivity URLs (emitting `media_uris`, `bq_eval.document.bytes`, `bq_eval.document.page_count`, and `bq_eval.attachment_sha256` on the span/request) when `payload_profile` permits storage.
- Emits structured inline DLP / safety verdicts (`guardrail.intervention` with `control_id`, `layer`, `action`) into a landing place only platform controls can write, so they count as platform-verified (`ADR-034`). Traffic whose content is stored nowhere is kept in the text-free objects and is not scored (`ADR-036`).

### Upstream Bronze / Collector Adapter Port (Custom Gateways, Coding SDKs & Non-Conforming Frameworks)

Silver keeps its in-BigQuery SQL shape adapters capped at three (`ADR-011`), **and** defines an explicit **Upstream Bronze / OTel Collector Adapter Port** for custom central gateways, coding-agent SDKs, ephemeral dev containers, and non-conforming frameworks outside the table below:
- **Custom Proxy Bronze Contract:** Central LLM gateways (in addition to `LiteLLM`) emit `requests` records via structured server/gateway logs (`_AllLogs` `inference_request` on GCP, S3 Parquet/Iceberg proxy logs on AWS) carrying mandatory `x-client-request-id` + `(trace_id, span_id)`, caller `trace_owner_app_id`, `upstream_endpoint_location`, `state_mode`, `thought_signature_present`, traffic type, and OBO claims; these count as verified because of where the gateway's records land, not because of a flag (`ADR-034`).
- **Coding-SDK & Container Collector Adapter:** Normalizes coding agents (including `Claude Agent SDK` and IDE/CLI agents running in ephemeral dev containers) into the General OTel GenAI shape while auto-injecting `user.id`, `gen_ai.response.id`, `gen_ai.response.time_to_first_chunk`, `workspace_session_id`, container compute (`sandbox.cpu_sec`, `sandbox.mem_gb_sec`), `outcome.*`, and execution-time `bq_eval.args_sha256` with zero per-agent code ceremony.

Every supported framework or gateway setup requires a **live recorded trace** in the fixture suite before counting as verified.

## Supported setups

Each application produces exactly one shape.

| Framework / Gateway | Version | Add-on | Setting | Shape |
|---|---|---|---|---|
| Google ADK, older trace format | 2.12 or earlier | none | none | ADK's own |
| Google ADK, older trace format | 2.14 or later | none | `ADK_TELEMETRY_SCHEMA_VERSION_OPT_IN=1` | ADK's own |
| Google ADK | 2.14 or later | none | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` | General |
| Google ADK | 2.14 or later | `opentelemetry-instrumentation-google-genai` 1.3b0 | the three upload settings below | General, by reference |
| LangGraph, LangChain | `langchain` 0.3.22 or later | `opentelemetry-instrumentation-genai-langchain` 1.3b0 | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` | General |
| LangGraph, LangChain | `langchain` 0.3.22 or later | `opentelemetry-instrumentation-genai-langchain` 1.3b0 | the three upload settings below | General, by reference |
| OpenAI Agents SDK | 0.3.3 or later | `opentelemetry-instrumentation-openai-agents-v2` 0.1.0 | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` | General |
| Pydantic AI | 2.73 or later | none | `Agent.instrument_all()` | General |
| Strands | 1.66 or later | none | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,gen_ai_span_attributes_only` | General |
| Microsoft Agent Framework | 1.26 or later | none | `ENABLE_SENSITIVE_DATA=true` | General |
| LiteLLM gateway | 1.110 or later | none | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` and `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY` | General |
| Custom central LLM gateway / Coding SDKs | any | Bronze / OTel Collector Adapter Port | Gateway `requests` log sink + Collector OTel normalizer | General / General, by reference |

The three upload settings:

| Setting | Value |
|---|---|
| `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` | a hook that meets the upload contract |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` | the Hive-partitioned BigLake storage path |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT` | `parquet` (canonical production format via async BigLake Collector/hook) or `jsonl` (dev/low-volume OTel helper only) |

## What no setting supplies

No framework setting supplies `user.id`, structured OBO claims, `outcome.*`, or `bq_eval.*` attributes (verified live on Google ADK 2.14.0: even when `user_id` is passed to `Runner.run_async()`, ADK emits no `user.id` on root spans, omits `gen_ai.response.model`, omits `gen_ai.response.id`, and omits `gen_ai.response.time_to_first_chunk`). In enterprise deployments, platform SDKs, gateways, or OTel Collector middleware inject these attributes via the Bronze Adapter Port rather than hand-coding them in every application ([attributes.md](attributes.md)).

## Not accepted directly in BigQuery Silver SQL

| Setup | Route instead |
|---|---|
| No content at all (capture off or zero retention) | kept in the text-free objects with a content-missing fact; no text rows, not scored by evaluation (`ADR-036`) |
| Prompt/answer content only in log records, with no storage upload | one of the three shapes (narrow OTel `gen_ai.evaluation.result` feedback events and custom-proxy metadata-only `requests` logs are read through `events` / `requests`) |
| LangGraph through LangSmith export, Traceloop, or OpenInference | the add-on in the table |
| AutoGen, Semantic Kernel | Microsoft Agent Framework |
| CrewAI, LlamaIndex, Claude Agent SDK, custom runtimes, custom log-backed gateways | normalize into the General OTel GenAI shape (and `requests` Bronze contract) through the **Upstream Bronze / OTel Collector Adapter Port** above (with a live recorded trace fixture) |

Normalization for setups outside the three in-BigQuery SQL shapes lives at the Upstream Bronze / OTel Collector Adapter Port before landing in Bronze rather than adding a fourth SQL adapter in Silver.
