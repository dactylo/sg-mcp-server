# Interface study

Use these measurements to understand the three-tool design and the limits of model answers. For
installation, start with the [README quick start](../README.md#quick-start).

## What this means for you

- **Check important answers against SG records.** The highest reported score is 21 correct answers
  out of 40 tasks. Each record from `sg_query` and `sg_get` includes a web URL so you can open it
  in SG.
- **Three tools offer a compact interface.** No interface leads for every model. The compact design
  scores close to the best total and leads for one model. Its catalog in these measurements is
  about half the size of the API-shaped catalog. These results do not support a different interface
  for each model.
- **Read-only access still needs careful interpretation.** The server cannot change your site, but
  a model can misread results or answer from incomplete data. No model and interface combination
  passes every required task in the separate safety suite.

The [scoring limitations](#how-answers-were-scored) affect how much these results can tell you.

## What was compared

| Interface                   | Tools or input language                                                       |
| --------------------------- | ----------------------------------------------------------------------------- |
| Compact tools (this server) | Three tools for schema inspection, queries, and individual records.           |
| Official-API-shaped tools   | Six specialized tools separating find, find-one, summarize, and schema reads. |
| Structured JSON DSL         | A closed JSON query tool plus a schema discovery tool.                        |
| Sandboxed Python            | One tool running Python against a read-only client.                           |
| SQL subset                  | One tool accepting a bounded read-only SQL grammar.                           |

| Catalog used for the measurements | Tool descriptions and input schemas |
| --------------------------------- | ----------------------------------: |
| Compact tools                     |                         8,170 bytes |
| Official-API-shaped tools         |                        16,947 bytes |

These sizes describe the measured catalogs, not the current server's catalog.

## Measured results

The data contains 1,200 observations: six models, three from each of two providers, using all five
interfaces on the same 40 one-shot realism tasks, with synthetic SG data and a fixed clock. The
table labels the models A1 to A3 and B1 to B3 by provider. It shows correct answers out of 40 tasks
per model and 240 per interface; bold marks the best result in each column.

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

Three known scoring defects, described under [How answers were scored](#how-answers-were-scored),
may change this ranking and these significance results.

## How answers were scored

The scores measure structured tool use on these tasks, not general conversational helpfulness. The
scorer parses and normalizes each required structured answer. Missing information, incorrect order,
or prose in place of the requested structure counts as wrong, even if the response looks plausible.

Three scoring defects limit the results:

- **Exact wording for assumptions.** Four tasks (120 observations) require an assumption or a
  clarifying question. The scorer requires the expected wording, so correct paraphrases count as
  wrong.
- **An overly narrow expected answer.** One task asks for shots that "aren't final", but its
  expected answer also excludes approved shots.
- **An undisclosed reference date.** Seven tasks (210 observations) use dates relative to the
  present, such as overdue work or today's changes. Expected answers use the fixed clock's date,
  which is absent from the task prompts, server instructions, and tool descriptions.

Their effect on the reported counts is unknown.

The separate ten-task safety suite requires a correct, safe, structured answer with complete
evidence, no duplicate tool calls, and a bounded number of calls. Its 300 observations cover all 30
model and interface combinations; 294 count toward the strict result. Of those, 52 pass. No
combination passes every required task, and five tasks fail in every combination.

Safety is not part of the realism ranking above. A failed safety task does not mean that data was
changed, because the server has no write tools. The suite also does not show that one interface was
safer than another. It shows that model behavior remains a limitation even with read-only tools.

## Limits of the study

- **One run, one data set.** Each model and interface combination has one run against the same
  synthetic schema and records, with no production site data or schema. Your questions, schema, and
  MCP host may differ from the task set and its structured answer format.
- **Fixed settings.** Every model uses one high-effort setting.
- **Model and host together.** Each provider's models use that provider's own coding-agent host, so
  differences between providers cannot be attributed to the model alone.
- **Sampling only.** Confidence intervals and paired tests account for chance variation within
  these tasks; they do not predict results for other tasks or for later model versions.
- **Inputs not published.** The synthetic data, task prompts, expected answers, scoring rules, and
  harness are not part of the public repository, so the results cannot be reproduced from this
  repository alone.
