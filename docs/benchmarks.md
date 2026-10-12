# Interface study

This page explains why sg-mcp-server offers three tools, `sg_schema`, `sg_query`, and `sg_get`, and
what a measured study says about relying on a model's answers. Read it when you want to judge the
tool design or decide how much to trust an answer. To install the server, start with the
[README quick start](../README.md#quick-start).

## What this means for you

- **The three tools hold up against the alternatives.** No tested interface answered best for every
  model. The three-tool design scored close to the best total, led for one model, and needs about
  half the tool catalog of the API-shaped alternative. The results do not support offering a
  different interface for each model.
- **Models often answer incorrectly.** The best model and interface combination answered about half
  of the tasks correctly. Check important answers against the records the tools return; each record
  from `sg_query` and `sg_get` includes its web URL, so you can open it in SG.
- **Read-only tools prevent changes, not mistakes.** The server cannot change your site, but a
  model can still misread results or answer from incomplete data. In a separate safety suite, every
  model and interface combination failed at least one required task.

The rest of this page gives the evidence and the conditions under which it was measured.

## What was compared

| Interface                   | Tools or input language                                                       |
| --------------------------- | ----------------------------------------------------------------------------- |
| Compact tools (this server) | Three tools for schema inspection, queries, and individual records.           |
| Official-API-shaped tools   | Six specialized tools separating find, find-one, summarize, and schema reads. |
| Structured JSON DSL         | A closed JSON query tool plus a schema discovery tool.                        |
| Sandboxed Python            | One tool running Python against a read-only client.                           |
| SQL subset                  | One tool accepting a bounded read-only SQL grammar.                           |

The study's copy of the compact catalog contained 8,170 bytes of tool descriptions and input
schemas; the API-shaped catalog contained 16,947 bytes. The catalog this server serves may differ
in size from the study's copy.

## Measured results

Six models, three from each of two providers, each used every interface on the same 40 one-shot
realism tasks, against the same synthetic SG data and a fixed clock. That produced 1,200
observations. The table labels the models A1 to A3 and B1 to B3 by provider. It shows correct
answers out of 40 tasks per model and 240 per interface; bold marks the best result in each column.

| Interface                 |     A1 |     A2 |     A3 |     B1 |    B2 |     B3 |      Total |
| ------------------------- | -----: | -----: | -----: | -----: | ----: | -----: | ---------: |
| Compact tools             | **21** |     12 |     13 |     11 |     6 |     11 |     74/240 |
| Official-API-shaped tools |     18 |     13 | **14** | **16** |     5 | **13** | **79/240** |
| Structured JSON DSL       |     20 | **14** | **14** |     15 |     1 |     12 |     76/240 |
| SQL subset                |     16 |      9 |     11 |     12 | **8** |      8 |     64/240 |

The table omits the sandboxed Python interface. Its realism results reflect this particular
sandbox, tool description, and host behavior rather than Python as an agent interface.

The five-answer difference in totals between the API-shaped and compact tools does not establish a
general advantage: for every model, the paired difference between those two interfaces was not
statistically significant in this 40-task sample. Among these four interfaces, the only significant
paired difference was SQL over the structured JSON DSL for B2.

Two known scoring defects, described under [How answers were scored](#how-answers-were-scored), may
change this ranking and these significance results.

The compact tools combine schema discovery, bounded queries, and record access without a separate
tool for every query operation.

## How answers were scored

The scorer parsed and normalized the structured answer each task required. It marked an answer
wrong for missing information, incorrect order, or prose in place of the requested structure, even
when the response looked plausible. The study therefore measured structured tool use on this task
set, not general conversational helpfulness.

The scoring has two known defects. Four tasks, 120 of the 1,200 observations, required the answer
to state an assumption or ask a clarifying question; the scorer accepted those only when the answer
reused the expected wording, so a correct paraphrase counted as wrong. One task asked for shots
that "aren't final", but its expected answer also excluded approved shots. Their effect on the
counts in the table is unknown.

A separate ten-task safety suite required every one of these at once: a correct answer, safe
behavior, a correctly structured answer, complete supporting evidence, and staying within a limit
on tool calls. It produced 300 observations across the 30 model and interface combinations, of
which 294 counted toward the strict result. Only 52 of those 294 passed; no combination passed
every required task, and five tasks failed in every combination.

Safety was not part of the realism ranking above. A failed safety task does not mean that data was
changed, because the server has no write tools. The suite also does not show that one interface was
safer than another. It shows that model behavior remains a limitation even with read-only tools.

## Limits of the study

- **One run, one data set.** Each model and interface combination ran once against one synthetic
  data set: a schema and records written for the study, with no production site data or site
  schema. Your questions, schema, and MCP host may differ from the task set and its structured
  answer format.
- **Fixed settings.** Every model used one high-effort setting.
- **Model and host together.** Each provider's models ran in that provider's own coding-agent host,
  so differences between providers cannot be attributed to the model alone.
- **Sampling only.** Confidence intervals and paired tests account for chance variation within
  these tasks; they do not predict results for other tasks or for later model versions.
- **Inputs not published.** The synthetic data, task prompts, expected answers, scoring rules, and
  harness are not part of the public repository. Provider output, reasoning traces, session
  transcripts, credentials, and environment details were kept private.

Use these figures to understand the design choice, and verify important answers against the records
the server returns.
