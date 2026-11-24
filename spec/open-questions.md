---
type: atom
related:
  - "layers.md"
  - "silver.md"
tags: [bq-eval]
---

# Open Questions

> What is undecided. Settled decisions live in [silver.md](silver.md) and the design notes; this note tracks only what is still open, with the current leaning where one exists.

## Column lists

The objects and their rules are fixed in [silver.md](silver.md). The columns of each object are not. They come with the schema files.

## Settle time and rebuild window

How long a trace must be quiet before it counts as settled, and how many days each run rebuilds. Both are settings, not design. They need a measurement of how late spans arrive.

## Reading across clouds

Per-trace rows, per-outcome rows and model-call usage may be read from the hub. How the hub reads them, and whether cross-cloud query limits allow it at real volume, is not verified. Model-call usage is one row per call, so it is the larger of the two.

## Evaluation where text may not leave

A judge needs the prompt and the answer, and text does not leave its cloud. Evaluation of a trace therefore has to run in the cloud that holds its text. How an add-on does that is open.

## A trace split across clouds

The owning cloud judges the trace on the spans it can see. Errors, tool calls and tokens in the other cloud are not part of that judgement. Whether an add-on should treat such a trace as incomplete is open.

## Sessions

A session is not a Silver object. `traces` carries the session ID and the turn number. Whether multi-turn conversations need an object of their own is open.

## Call-level rows without text

Decision needed. Whether the scheduled job that builds `traces` also stores one row per model call and per tool call, holding everything except text: model, tokens, durations, tool name, status. Add-ons would then read stored rows and step attributes would be parsed once, not on every query. The leaning is yes.

## Copies of text

Decision needed. Whether Silver ever stores a copy of text or encoded files. The leaning is never: content stays where it landed and is read only for the traces someone judges.

## A step that fits no shape

Decision needed. What Silver does with a step it cannot read as one of the three [shapes](shapes.md). The leaning is to keep it, flag it and count it.

## Size of content on a step

The trace store's limit on the size of one step attribute is not known. Two of the three shapes put whole prompts and tool results on the step. A test with one very large prompt settles how many setups have to move to content by reference.

## Proof of each setup

The versions and settings in [shapes.md](shapes.md) come from reading each package, not from running it. Google ADK 2.13 was not read at all. The leaning is that a setup counts as supported only when a recorded trace from it is among the fixed inputs.

## Limits of content by reference

- Tool steps are not covered. Their arguments and results are inside the stored messages of the model call that follows. Whether a row per tool call reads them from there is open.
- An upload can be skipped while its address is still written. How large the share of such addresses may get before a trace counts as having no text is open.
- Storage fills with many small files under one path. How an external table performs over them is not measured.
- Only setups built on the official OpenTelemetry helper library can write by reference. Whether the others get a small application-side uploader is open; the leaning is not before the size test shows text being cut.


## What the use cases need that Silver does not define

Of the 27 required checks in the [use-case notes](use-cases.md), Silver as specified in [silver.md](silver.md) feeds 6 in full, 10 in part and 11 not at all. That count is a reading of the notes against the specification, not a measurement. The questions below are what stands between the two.

### Who may read call-level data

Only the cost add-on may read `spans`, and tool arguments and results are in no published object. Every use case needs something at this level: the user passed to a tool call (1), whether the ticketing tool ran on a rejected email (2), time to first output and personal data in tool traffic (3), which model handled a document first (4), which calls wrote and with which key (5). Whether Silver publishes one object per model call without text and one per tool call with its arguments, results and a hash of the arguments, readable by every add-on, is open. The leaning is yes, and that it replaces the cost add-on's access to `spans`.

### Events between the start and the close of an outcome

`outcomes` holds what the closing step reported. Use case 2 needs the time from the inbound email to the acknowledgement, the rejection before a ticket, the escalation and the time spent waiting for the customer. Use case 5 needs the park for approval, the approval and the resume. Use case 4 needs to know whether a document went to user review. Whether Silver publishes such events is open.

### Signals that arrive after a trace is settled

A thumbs up or down (3), a user's correction of an extraction (4) and a reviewer's sign-off (5) arrive minutes or days later, and a settled row should not change. Whether these share one object with the events above is open. So is the line to the add-ons; the leaning is that signals that are part of the running workflow are Silver, and labels produced by the evaluation process are not. An approval has to come from the system that grants it: an agent's own statement that a person approved is a claim and counts for nothing.

### The tool-call limit

The limit of 8 tool calls is the same for every application and changes the overall pass. A coding session (3), an orchestrator with specialists (2) and a long document read in parts (4) pass 8 without looping. Whether the limit becomes a value per application, or a loop is redefined as the same tool called again with the same arguments, is open.

### Freshness

Silver states no delay. Use case 1 asks for a warning within one minute when the user is missing, and use case 2 has a one-minute target. Whether such alerts read `spans` over the last minutes, or are raised where telemetry is collected and stay outside Silver, is open.

### Outcomes that outlive the rebuild window

Use case 2 runs to 24 hours and beyond, and use case 5 pauses for days. Which day an outcome's row belongs to, how it is rebuilt once its first trace has left the window, and that an outcome with no closing step still gets a row, are not stated.

### Sources that are not telemetry

Batch results that produce no span per document (4), the approval system's own records (5) and the mail and ticket system's own events (2). Whether each enters as spans written by the application or through an adapter of its own is open.

### What prompt, context and answer mean

`trace_payloads` has three parts per trace, and telemetry carries text on many steps. Which steps fill each part is not written down. In use case 2 the last output of a trace can be a question back to the customer, not an answer.

### A tool list as reference data

Which tools write to a system of record and which need approval first (5), which return personal data (1) and which create a ticket (2). None of this is in the telemetry, and asking an application to label its own tools runs against the rule on what an application says about itself. Whether a third reference table holds it is open.

### What every application has to add

The OpenTelemetry GenAI conventions carry no user, no business outcome, no turn number, no user feedback, no serving tier for Gemini, no mark for test traffic, no human approval, no model confidence and no idempotency key. Which of these are required of every application, and under which names, is open.

### Smaller items

- `traces` and `outcomes` are described as small. Use case 4 is one outcome per document at very large volume, and nothing is measured.
- `spans` and `trace_payloads` are views. When the trace store expires data, the evidence behind a decision in use case 5 is gone.
- A user on a per-trace row crosses to the hub with the row. "No text" does not cover it.
- Values only an application knows, such as page count or model confidence, have no defined way into Silver.
