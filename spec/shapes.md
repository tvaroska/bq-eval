---
type: atom
related:
  - "silver.md"
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

Prompts, answers, system instructions, tool definitions and encoded files carried in messages (documents, images, audio) may arrive by reference. The content sits in storage in the same cloud as the trace, and the step carries its address. Silver follows the address.

An address whose content is missing is counted, like any other missing value.

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
| `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK` | `upload` |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` | the storage path |
| `OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT` | `jsonl` |

No framework setting supplies the user or the business outcome. Those are asked of each use case.

## Not accepted

| Setup | Instead |
|---|---|
| Content only in log records, with no upload | one of the three shapes |
| LangGraph through LangSmith export, Traceloop or OpenInference | the add-on in the table |
| AutoGen, Semantic Kernel | Microsoft Agent Framework |
| CrewAI, LlamaIndex, Claude Agent SDK | convert to the general shape before the data lands |

An application outside the table may rewrite its telemetry into the general shape before it is stored. That rewriting is the application's own and is not part of the core.
