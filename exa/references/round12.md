# Round 12 — v0.13 next-layer power (recall coverage + rich convenience layers)

All surfaces below are live-verified against api.exa.ai / api.exa.ai/websets (2026-08-07).

## 1. `webset_recall(webset_id, search_id=None)` — read-side coverage estimate

When a webset search is created with `recall=True`, the API computes a
**total-match coverage estimate** on the completed search: the expected number
of records that *could* match your criteria, a confidence (high|medium|low),
a min/max bounds range, and the agent-authored reasoning that produced the
estimate. This is genuinely new data the API returns (it lives in the
`recall` block of a `websetSearch`), and it lets you judge **how much of the
true market you've captured** before acting on a lead list.

```python
r = exa.webset_recall("webset_01kzdz6evys7w78y7fenc696jb")
r["summary"]          # "Estimated 200 total potential matches (confidence=medium, range 100..300). The estimate ..."
r["expected_total"]   # 200
r["confidence"]       # "medium"
r["bounds"]           # (100, 300)
r["reasoning"]        # "We conducted multiple searches to find companies ... ~3,000 completed Series A in 2024 ..."
r["exists"]           # True
```

Live-verified on a "AI startups in California that raised Series A in 2024"
search: expected total 200, medium confidence, range 100–300, with grounded
reasoning over PitchBook/NVCA/Crunchbase. If the search did not request
`recall=True` (or hasn't completed yet), `exists` is `False` and
`expected_total` is `None` — no fabricated numbers. `search_id` is optional;
omit it to auto-pick the most recent search on the webset.

## 2. `similar_to(url, **kwargs)` — more-like-this alias

Friendly alias over `find_similar`: given one good source page, expand it into
a cluster of peer pages/papers. Accepts every `find_similar` keyword.

```python
peers = exa.similar_to("https://clojure.org/reference/transducers",
                       num_results=8, with_highlights=True)
```

## 3. `agent_chat(query, ..., turns=N)` — multi-turn agentic research loop

Each turn is a full `agent()` run chained via `previous_run_id`, so follow-up
prompts continue the same thread with awareness of the prior turn's sources,
grounding, and conclusion. No manual run-id threading.

```python
out = exa.agent_chat(
    "What are the main strengths of Reitit for Clojure?",
    system_prompt="Be concise. Prefer official docs.",
    turns=2,               # survey, then refine
)
out["run_id"]       # last agent run id
out["text"]         # final answer
out["citations"]    # unique citation list
out["turns"]        # [AgentRun, AgentRun, ...]
```

Live-verified over two turns (survey → refine); turn 2 was a better "tighter
final answer" that clearly reused the prior turn's context.

## 4. `search_to_csv(results, path)` + `SearchResults.to_xlsx(path)`

Lazy-pandas exporters with the same row layout as `to_dataframe()` (title,
url, domain, hostname, published_date, author, score, snippet, summary,
highlights) plus an optional raw `entities` column (`include_entities=True`).
Great for handing search output to spreadsheets/analytics.

```python
exa.search_to_csv(results, "clojure_web_frameworks.csv", include_entities=False)
results.to_xlsx("clojure_web_frameworks.xlsx")
```

`to_xlsx` requires the optional `openpyxl` writer (imported lazily).

---
All four surfaces live-verified; `__all__` is now 96 entries. Backward compat
is fully preserved — no existing signature or return changed.
