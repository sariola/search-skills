# Round 18 — Developer-community surfaces (v0.18) — live-verified

This round ships **first-class developer/software-engineer surfaces** on top of
the existing Exa primitives (`search`/`fetch`/`answer`/`news_roundup`/...) plus
the permitted raw github.com / huggingface.co HTTP. Every function returns clean
structured objects and was verified live against api.exa.ai with the query noted.

`__all__` went 110 → **117** (7 new names, all backward compatible, nothing reused).

---

## 1. `github_repo(url_or_repo, ...)`

Client-side GitHub repo profile. Resolves a URL or `owner/repo`, confirms via
`search(mode='github')`, then pulls structured metadata + README head from the
GitHub REST API (`/repos/{o}/{r}`, `follow_redirects=True`) and raw README.

Verified on `github.com/ollama/ollama`:
- owner=`ollama`, repo=`ollama`, description="Get up and running with Kimi-K2.6...",
  primary_language=`Go`, stars=`177984`, forks=`17293`,
  license=`MIT`, topics=[...14...], readme_head=700 chars.
Verified on `github.com/ggerganov/llama.cpp` (renamed → `ggml-org/llama.cpp`):
- follow_redirects resolved the alias; owner=`ggml-org`, repo=`llama.cpp`,
  primary_language=`C++`, stars=`122973`, forks=`21416`, license=`MIT`,
  default_branch=`master`, readme_head populated.

## 2. `code_search(query, *, language=None, num_results)`

Surfaces the GitHub-mode hits as clean `code` / `pull_requests` / `commits` /
`issues` / `repos` dicts with snippets. Optional `language` filters client-side
by file extension.

Verified on `"llama.cpp vulkan replace llama_vulkan.move"` (num_results=6):
returned 6 hits classified code/PR/commit, including
`ggml/src/ggml-vulkan/ggml-vulkan.cpp` (code) and
`ggml : add Vulkan backend (#2059)` (commit).

## 3. `hf_models(query, ...)`

HuggingFace **model** search: site-scoped `search(include_domains=['huggingface.co'])` +
raw HF `/api/models/{id}` JSON (downloads/likes/task/license) + raw model-card
README for ``card_markdown``.

Verified on `"llama 3.1 8b"` → top
`meta-llama/Llama-3.1-8B-Instruct`: author=`meta-llama`, task=`text-generation`,
library=`transformers`, license=`llama3.1`, downloads=`7666987`, likes=`6544`,
params=`8030261248`, tags=[transformers, safetensors, llama, ...].
Gated model (`gated=manual`) → `card_markdown` empty (401), all metadata intact;
public model (`unsloth/Meta-Llama-3.1-8B-Instruct`) → card_markdown populated.

## 4. `hf_discussions(query, ...)`

HuggingFace community **discussion threads**: HF-scoped + `discuss.huggingface.co`-scoped
search, classified `model-community` / `huggingface-forum`, with 220-char excerpts.

Verified on `"Llama 3.1 8B"`: returned 12 threads covering both communities
(e.g. "Running LLaMA 3.1 8B Model Downloaded from Meta — missing configuration file").

## 5. `dev_help(query)`

Citation-grounded dev **answer** over the code corpus + code-snippet extraction +
enumerated steps + per-source citations.

Verified on `"how to fix UnicodeDecodeError utf-8 gbk"`: 852-char answer,
3 code fences (incl. `with open("f.txt","r", encoding="gbk")`), 8 cited sources
with title/url/author/date.

## 6. `find_used_by(package, ...)`

Find repos / notebooks / `.py` / `.pyt` pages that **import** a package: runs
github-mode `import <pkg>` + notebook-oriented queries + a `*.py/*.ipynb` web
pass, de-dupes by URL, excludes the package's own repo (repo basename == package
or guessed owner).

Verified py-level signal on `ollama` (6 results): `pdichone/ollama-fundamentals`,
`RamiKrispin/ollama-poc` (ollama-poc.ipynb), `krisograbek/ollama-chatbot-st`,
`brevdev/notebooks` (llama3-to-ollama.ipynb), `microsoft/Phi-3CookBook`.
Verified on `httpx` (8 results): `rcmckee/webscraping-with-selectolax-and-httpx`,
`jupyter/notebook` (pins httpx), `fastapi/fastapi` (issue), python-httpx.org.

## 7. `dev_report(query, ...)`

One-call **dev-light README**: web `search` + `github_repo` block + `answer`
citations + `news_roundup` fresh sweep, returned as a markdown **string**
(`return_meta=True` wraps the pieces).

Verified on `"ollama python server usage"` (repo=`ollama/ollama`, days=3):
3410-char markdown with Summary + cited sources (Real Python, PyPI, DeepWiki), a
GitHub: ollama/ollama block (primary=Go stars=177984), Top results, and a
Fresh dev news list.

---

Auto-checked: `import exa` loads with `__version__="0.18.0"`, `__all__` length
117 (no dupes), and all prior entry points unchanged.
