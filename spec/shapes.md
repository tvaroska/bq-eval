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

> The forms of telemetry Silver accepts, and how each framework is set up to produce one of them. Silver reads these and nothing else.

## What a shape is

A shape is one family of attribute names in one placement. The same names in a second placement count as a second shape.

Silver accepts at most three shapes. A new shape needs a named use case and a recorded trace, and it has to replace one of the three.

## The three shapes

| Shape | Names | Where the content is |
|---|---|---|
| **ADK's own** | `gcp.vertex.agent.*` | on the step |
| **General** | the OpenTelemetry GenAI conventions | on the step |
| **General, by reference** | the OpenTelemetry GenAI conventions | in storage; the step carries the address |

ADK's own shape is what ADK writes in its older trace format. ADK plans to remove that format, so this shape is expected to be replaced.

## Content by reference

Prompts, answers, system instructions, tool definitions and encoded files carried in messages (documents, images, audio) may arrive by reference. The content sits in storage in the same cloud as the trace, and the step carries its address. Silver follows message addresses, while source documents and media passed by storage URI (such as multi-hundred-page PDFs) stay in storage in place and expose their URIs on `trace_payloads`.

An address whose content is missing is counted, like any other missing value, and the text part is marked as not read whole.

### When content must go by reference

An application where one prompt, answer or tool result can exceed the trace store's per-attribute limit (`65,532` bytes exact measured cap; `65,533+` bytes silently drops the entire span in an `HTTP 200` `partialSuccess` batch rather than truncating, and span `events` enforce the same cap — see [open-questions.md](open-questions.md#tests-on-the-trace-store)) uses the shape that carries content by reference.

### Upload contract

Any writer of content by reference meets this contract:

| Part | Rule |
|---|---|
| Storage & partitioning | Written to a **BigLake-backed** storage prefix partitioned by Hive date/hour (`dt=YYYY-MM-DD/hr=HH/...`), either as micro-batched Parquet/Iceberg (preferred at high volume) or JSON Lines matching the OpenTelemetry helper layout, in the same cloud and CMEK perimeter as the trace. Flat unpartitioned per-call JSONL joined dynamically by `_FILE_NAME` is prohibited at scale because BigQuery scans 100% of files on dynamic joins. |
| Addresses | The step carries the four standard reference attributes: `gen_ai.input.messages_ref`, `gen_ai.output.messages_ref`, `gen_ai.system_instructions_ref`, `gen_ai.tool.definitions_ref`. |
| Risk-tiered delivery | Uploads run **asynchronously** off the interactive request hot path (via a bounded Collector/sidecar queue with explicit drop counters) so storage backpressure never stalls live requests. For Tier-1 regulated state writes (`payload_profile = 'regulated_worm'`), audit enqueue fails closed at the tool/gateway Port before mutation executes; zero-payload ZDR apps (`payload_profile = 'zdr_metadata'`) disable content uploads entirely while emitting execution-time `bq_eval.args_sha256`. |
| Failure & aborted turns | A failed upload leaves the address on the step so Silver counts missing content. Tool execution errors and execution-time `bq_eval.args_sha256` are emitted directly on `execute_tool` spans so aborted turns without a subsequent model call never lose tool errors or hashes. |

BQ Eval ships one reference asynchronous hook/adapter meeting this contract. The official helper's default `upload` hook drops silently without counters or Hive partitioning and does not meet it.

### Upstream Bronze / Collector Adapter Port (Non-Conforming Frameworks)

Silver keeps its in-BigQuery SQL shape adapters capped at three (`ADR-011`), **and** defines an explicit **Upstream Bronze / OTel Collector Adapter Port** for frameworks and gateways outside the table below (such as third-party agent SDKs, custom runtimes, or log-backed gateways). Those adapters normalize telemetry into the General OTel GenAI shape and inject platform-level attributes (`user.id` / attested OBO identity, `outcome.*`, `bq_eval.*`, `gen_ai.response.time_to_first_chunk`, and execution-time `bq_eval.args_sha256`) at SDK, Gateway, or Collector middleware — eliminating per-agent boilerplate. Every supported framework setup requires a **live recorded trace** in the fixture suite before counting as verified.

## Supported setups

Each application produces exactly one shape.

| Framework | Version | Add-on | Setting | Shape |
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

The three upload settings:

| Setting | Value |
|---|---|
| `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` | a hook that meets the upload contract |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` | the storage path |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT` | `jsonl` |

## What no setting supplies

No framework setting supplies attested `user.id` / OBO identity, `outcome.*`, or `bq_eval.*` attributes (verified live on Google ADK 2.14.0: even when `user_id` is passed to `Runner.run_async()`, ADK emits no `user.id` on root spans, omits `gen_ai.response.model`, and omits `gen_ai.response.time_to_first_chunk`). In enterprise deployments, platform SDKs, gateways, or OTel Collector middleware inject these attributes via the Bronze Adapter Port rather than hand-coding them in every application ([attributes.md](attributes.md)).

## Not accepted

| Setup | Instead |
|---|---|
| Prompt/answer content only in log records, with no storage upload | one of the three shapes (narrow OTel `gen_ai.evaluation.result` feedback events are read into `events`) |
| LangGraph through LangSmith export, Traceloop or OpenInference | the add-on in the table |
| AutoGen, Semantic Kernel | Microsoft Agent Framework |
| CrewAI, LlamaIndex, Claude Agent SDK | convert to the general shape before the data lands |

An application outside the table may rewrite its telemetry into the general shape before it is stored. That rewriting is the application's own and is not part of the core.
