---
name: exa
description: Search the web with Exa for semantic discovery, research papers, code, companies, people, and page content. Use for Exa searches, cited research answers, or explicitly requested Websets and monitoring through the bundled Python client. Requires EXA_API_KEY.
---

# Exa search

Find relevant sources, retrieve the evidence needed to answer the question, and
return findings with source links. Use the bundled `exa` Python package; its
interface differs from Exa's official SDK and from MCP tools with similar names.

## Start here

Resolve this skill's directory and run Python with its local package:

```bash
uv run --with /absolute/path/to/exa python - <<'PYTHON'
import exa
results = exa.search("papers on incremental materialized view maintenance",
                     mode="paper", num_results=5, with_highlights=True)
print(results.to_agent())
PYTHON
```

Supply `EXA_API_KEY` through the environment. Existing dotenv/key-store fallbacks
are described in [usage.md](references/usage.md). Do not print credentials.
Normal Python uses `exa.run(query)`, not `exa(query)`; the latter requires an
external harness binding. Functions accept `await` on their returned values,
but perform synchronous I/O before returning. Use a worker thread when calling
from an event loop; `await` alone does not provide concurrency.

## Choose the smallest useful operation

| Need | Operation |
|---|---|
| Ranked pages or papers | `search(query, ...)` → `SearchResults` |
| Compact evidence for a model | `brief(query, ...)` or `results.to_agent()` |
| Readable search results | `run(query, ...)` |
| Read a known source | `fetch(urls=[url], include_meta=True, max_characters=6000)` |
| Broaden a strong seed | `find_similar(url, exclude_source_domain=True, ...)` |
| Search several distinct facets | `deep_research([q1, q2], num_results=5)` |
| Provider-generated answer with citations | `answer(question)` → `Answer` |
| Multi-step hosted research | `agent(question, ...)`; read [jobs.md](references/jobs.md) |
| Persistent entity collection | `webset_*`; read [websets.md](references/websets.md) |

Start with `search_type="auto"` and a small result count. Use a precise natural
language description for semantic discovery; preserve exact symbols, names, and
versions for code or identity questions. Expand queries or use heavier research
only when a concrete evidence gap remains. Fan-out helpers make multiple paid
requests; `num_results` is per query, not a total budget.

## Search, verify, answer

1. Identify the question's scope: entity, time interval, geography, and required
   evidence. Add domain or publication-date filters when they express that scope.
2. Choose a category through `mode`: `paper`, `code`/`github`, `company`,
   `financial`, `people`, `personal-site`, `publication`, `news`, or `auto`.
   `lead` and `websets` search modes are company-page searches; they do not
   create persistent Websets. For mode constraints, read [usage.md](references/usage.md).
3. Request highlights to triage, then fetch the strongest primary sources.
   Inspect per-URL errors and truncation. An empty excerpt is not proof that
   the page lacks the answer. A generated summary is not a quotation.
4. Resolve important disagreements against the source material. A second engine
   can reveal missing sources, but two engines returning the same page are not
   independent corroboration. Stop when the requested claims are supported or
   the remaining gap is clear; report uncertainty rather than padding the list.
5. Link claims to the actual supporting pages. Preserve dates, versions, units,
   and distinctions between reported facts, provider synthesis, and your inference.

For papers, verify authors, publication status, and findings in the paper. For
financial reports, verify entity, period, currency, and reported versus estimated
figures. For people and companies, disambiguate identity before merging records;
entity profiles can be incomplete or stale.

## Read only the relevant reference

- [usage.md](references/usage.md): filters, content shapes, entities, exports,
  metadata, and error handling.
- [developer.md](references/developer.md): repositories, code, packages, debugging,
  releases, and Hugging Face research.
- [jobs.md](references/jobs.md): agent runs, streaming, and search monitors.
- [websets.md](references/websets.md): collections, qualification, enrichment,
  imports, webhooks, and scheduled refresh.

These references describe the bundled client. For an unfamiliar helper, inspect
its signature and implementation in [the module](src/exa/__init__.py) before use.
Do not infer current API support or plan entitlement from a historical success.
