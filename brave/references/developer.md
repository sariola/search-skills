# Developer research

Choose the exact ecosystem, package, version, repository, or error before
searching. Prefer maintainer documentation, code, release notes, and advisories
for conclusions. Community posts help discover causes but can target other versions.

| Need | Helpers |
|---|---|
| Documentation | `docs(query)`, `domain(host)` |
| Code or examples | `code_result(query, lang=...)` |
| Community explanations | `qna(query, min_answers=...)`, `forums(query)`, `reddit(query, subreddit=...)` |
| Repositories and issues | `github_repos(query)`, `github_issues(query)` |
| Registry identity | `software(query)`, `pkg_lookup(query)` |
| Exact exception | `error_solution(query, ctx=...)` |
| Trace plus related issues | `stack_trace(query, ctx=...)` |
| Known advisory | `cve_lookup(cve)` |
| Package advisory candidates | `pkg_security(query, context=...)` |
| Releases and migrations | `breaking_change(query)`, `dep_signal(query, library=...)` |
| Combined brief | `dev_digest(query)` |

These helpers compose searches and local ranking/classification. A package
version in a search snippet is not a registry freshness guarantee; `error_solution`
does not test a fix; `pkg_security` does not scan dependencies. Verify affected
and fixed versions, applicability, and advisory status in authoritative sources.
Absence of an indexed CVE or deprecation hit proves neither safety nor support.

For debugging, search the distinctive error text plus the relevant library and
version rather than every stack frame. Inspect a proposed solution's context
before adapting it. Vote counts and top-comment selection rank leads; they do
not establish correctness. `dep_signal` uses text labels and available age data,
so unknown dates can prevent reliable recency filtering.

```python
results = brave.search('site:docs.python.org "TaskGroup" "3.13"', count=5)
issues = brave.github_issues('tokio JoinSet abort_all', count=5)
```

For complete or exact repository inspection, use local source or a repository
API when available. Web search helpers are not exhaustive code indexes.
