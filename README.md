# search-skills

Reusable agent skills and Python clients for Exa and Brave Search. Each skill
starts with task selection and evidence handling; focused references cover
advanced operations. The clients can be used independently or as complementary
search paths.

## Install and use

Python 3.10+ and `uv`:

```bash
uv venv
uv pip install --python .venv/bin/python -e ./exa -e ./brave
```

Configure `EXA_API_KEY` and `BRAVE_API_KEY` (or `BRAVE_SEARCH_API_KEY`) in your
environment. The clients also support existing Prime dotenv/key-store fallbacks.

```python
import exa
import brave

papers = exa.search("incremental view maintenance", mode="paper",
                    num_results=5, with_highlights=True)
print(papers.to_agent())

pages = brave.search('site:docs.python.org "TaskGroup"', count=5)
for page in pages.get("results") or []:
    print(page.get("title"), page.get("url"), page.get("snippet"))
```

Use `exa.run(...)` / `brave.run(...)` for readable text. No external harness or
CLI launcher is required. The functions accept awaitable results for harness
compatibility but still perform blocking I/O; use threads for event-loop use.

To install as agent skills, copy or link each complete skill directory into your
agent's global skills directory. Keep `src`, `pyproject.toml`, and `references`
with `SKILL.md`; names alone are not dependencies or installed packages.

## Skill guides

- [Exa](exa/SKILL.md): semantic discovery, content, papers, code, and entities.
  References cover [search](exa/references/usage.md),
  [developer research](exa/references/developer.md),
  [hosted jobs](exa/references/jobs.md), and [Websets](exa/references/websets.md).
- [Brave](brave/SKILL.md): web, news, media, local, and exact-term discovery.
  References cover [the client](brave/references/api.md),
  [developer research](brave/references/developer.md), and
  [verticals](brave/references/verticals.md).

These are custom clients, not the vendors' official SDKs. Python signatures come
from the bundled source; current endpoint support and entitlement come from the
provider. Convenience helpers can make multiple requests and return heuristics
or generated summaries. Verify substantive claims against original sources.

## Validation

```bash
uv run --with ./exa --with ./brave python -m unittest discover -s tests
```

The tests use local fixtures and do not need keys or make paid API calls.

## License

[MIT](LICENSE) — Copyright (c) 2026 karolus.
