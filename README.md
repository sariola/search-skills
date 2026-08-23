# search-skills

Agent-friendly Python clients for two independent web-search APIs: **Exa**
(neural/semantic search, answers, WebSets) and **Brave Search** (web/news/image/
video/local, rich structured results). Built as agent skills — every function
returns readable Markdown or tidy dicts, so an LLM agent can call them directly.

They are designed to be used together as two independent discovery paths, with
load-bearing claims verified against the returned primary sources.

## Layout

```
exa/                      # Exa Search skill (v0.20.0)
  SKILL.md                # full agent-facing capability doc
  pyproject.toml
  src/exa/__init__.py     # the whole client (httpx only)
  references/             # per-round dev notes: usage, websets, API details
brave/                    # Brave Search skill (v0.20.0)
  SKILL.md                # full agent-facing capability doc
  pyproject.toml
  src/brave/__init__.py   # the whole client (httpx only)
  references/             # per-round dev notes: api.md + round-by-round findings
```

## Install

Python ≥ 3.10, single dependency (`httpx`).

```sh
uv venv && source .venv/bin/activate
uv pip install -e ./exa -e ./brave
# or: pip install -e ./exa -e ./brave
```

## Keys

Both clients read their key from the environment (with dotenv fallbacks):

- `EXA_API_KEY` — from <https://api.exa.ai>
- `BRAVE_API_KEY` — from <https://brave.com/search/api/>

## Usage

```python
import exa, brave

# Exa: neural search with grounded summaries
res = exa.search("branch-native databases", num_results=5, with_summary=True)
for r in res.results:
    print(r.title, r.url, r.summary)

# Exa: citation-grounded answer
print(exa.answer("What is Firecracker's memory-snapshot restore model?"))

# Brave: structured web search
b = brave.search("firecracker microvm", count=4)
for tr in b["top_results"]:
    print(tr["item"]["title"], tr["item"]["url"])
```

Each package's `SKILL.md` is the authoritative surface map — dozens of modes
per engine (news/video/image/local/rich-schema/POI/research digests on Brave;
answers, agents, monitors, WebSets, exporters on Exa), all documented with
live-verified request/response notes in `references/`.

## Note on console scripts

Each `pyproject.toml` declares a `[project.scripts]` entry pointing at a
shared `rlm.skill:cli` launcher that is **not** part of this repo. Installing
the packages works and the modules import fine; only the bare `exa` / `brave`
shell commands are unavailable. Import the modules instead.
