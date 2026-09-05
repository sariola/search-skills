# Developer research

Start from the exact package, repository, version, symbol, or error. Prefer
maintainer docs and source at the relevant revision. Search results and extracted
snippets are leads; verify compatibility before proposing code changes.

| Task | Helper | Interpretation |
|---|---|---|
| Repository identity and README | `github_repo("owner/repo")` | Combines search with GitHub API/README fetches; confirm redirects and default branch |
| Code, issues, PRs, commits | `code_search(query, language=...)` | Classifies indexed GitHub URLs; language filtering is heuristic |
| Package usage examples | `find_used_by(package)` | Searches imports/notebooks; not a complete reverse-dependency index |
| Hugging Face models | `hf_models(query)` | Combines search, metadata, and model cards; gated cards can be absent |
| Hugging Face discussions | `hf_discussions(query)` | Searches community pages and forums |
| Cited coding answer | `dev_help(query)` | Generated synthesis; read the cited code/docs |
| Combined research report | `dev_report(query, repo=..., days=...)` | Multiple searches, answer generation, and optional repository/news retrieval |
| Release discovery | `pkg_releases(package)` | Search-derived candidates; verify registry or release tags |
| Deprecation candidates | `deprecations(query)` / `is_deprecated(package)` | Search/classification signals, not definitive package status |
| Example fragments | `snippet(query, lang=...)` | Extracted code fences, not tested solutions |
| Read API documentation | `api_reference(url)` | Fetches bounded page content and optional summary |
| Linting advice | `code_lint_tips(query)` | Generated advice, not a linter execution |

Examples:

```python
hits = exa.search("TaskGroup cancellation Python 3.13",
                  include_domains=["docs.python.org"], with_highlights=True)
code = exa.code_search("tokio JoinSet abort_all", language="rust", num_results=5)
```

For debugging, separate the exception text from unrelated stack frames and
include environment/version context. Open the source or issue resolution before
recommending a fix. For release/security decisions, establish affected and fixed
versions from maintainer advisories; a missing search hit does not prove safety.
Do not execute downloaded snippets as part of search alone.
