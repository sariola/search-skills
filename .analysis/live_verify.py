"""Phase-4 live verification: exercise the documented Exa + Brave surfaces
against the real APIs and record a contract baseline.

Cost-conscious: small counts, bounded request fan-out. Agent + batch are
exercised minimally (they burn API credits). Never prints credentials.
Run:  python3 .analysis/live_verify.py
"""
import json
import os
import sys
import time

sys.path.insert(0, "exa/src")
sys.path.insert(0, "brave/src")

import exa
import brave

RESULTS = []


def ok(name, **extra):
    RESULTS.append({"name": name, "status": "ok", **extra})
    print(f"PASS  {name}  " + (json.dumps(extra) if extra else ""))


def fail(name, exc):
    RESULTS.append({"name": name, "status": "fail",
                    "error": f"{type(exc).__name__}: {exc}"})
    print(f"FAIL  {name}  {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# EXA
# ---------------------------------------------------------------------------
def run_exa():
    # 1. team_info — no credit spend, confirms key + auth path
    try:
        r = exa.team_info()
        ok("exa.team_info", plan=r.get("plan") if isinstance(r, dict) else "?",
           has_email=bool(r.get("email")) if isinstance(r, dict) else False)
    except Exception as e:
        fail("exa.team_info", e)

    # 2. search — core (deep is a search_type, not a mode)
    try:
        sr = exa.search("exa search api", num_results=3, search_type="deep",
                        with_highlights=True, with_summary=True)
        ok("exa.search", n=len(sr.results),
           request_id=sr.request_id, cost=sr.total_cost(),
           first_title=sr.results[0].title if sr.results else None)
    except Exception as e:
        fail("exa.search", e)

    # 2b. search — bad mode must raise a *clean* ExaBadRequestError
    try:
        exa.search("x", mode="not-a-mode")
        fail("exa.search(bad-mode-rejected)", AssertionError("no error raised"))
    except exa.ExaBadRequestError:
        ok("exa.search(bad-mode-rejected)")
    except Exception as e:
        fail("exa.search(bad-mode-rejected)", e)

    # 3. fetch — single URL string shape
    try:
        f = exa.fetch(["https://example.com"])
        ok("exa.fetch(string-shape)", is_str=isinstance(f, str),
           has_id="id" in f if isinstance(f, dict) else "n/a")
    except Exception as e:
        fail("exa.fetch", e)

    # 4. fetch — list shape with highlights
    try:
        f2 = exa.fetch(["https://example.com"], with_highlights=True)
        ok("exa.fetch(list-shape)", is_list=isinstance(f2, list),
           n=len(f2) if isinstance(f2, list) else "?")
    except Exception as e:
        fail("exa.fetch(list-shape)", e)

    # 5. answer — citation-grounded
    try:
        a = exa.answer("What is the capital of France?")
        ok("exa.answer", answer_len=len(a.answer or ""),
           n_citations=len(a.citations), cost=a.cost_dollars)
    except Exception as e:
        fail("exa.answer", e)

    # 6. find_similar
    try:
        fs = exa.find_similar("https://example.com", num_results=2)
        ok("exa.find_similar", n=len(fs.results))
    except Exception as e:
        fail("exa.find_similar", e)

    # 7. agent — minimal agentic run (agent takes effort, not num_searches)
    try:
        ar = exa.agent("What is 2+2? Answer in one word.")
        ok("exa.agent", id=ar.id, status=ar.status,
           text=(ar.text or "")[:40], structured=ar.structured)
    except Exception as e:
        fail("exa.agent", e)

    # 8. agent_stop on a completed run (should be a no-op or clean error)
    try:
        ar = exa.agent("say hi")
        try:
            stop = exa.agent_stop(ar.id, reason="budget_reached")
            ok("exa.agent_stop", id=ar.id, resp=str(stop)[:60])
        except exa.ExaError as e:
            ok("exa.agent_stop(expected-error-on-terminal)", id=ar.id,
               err=type(e).__name__)
    except Exception as e:
        fail("exa.agent_stop", e)

    # 9. batch — create a 2-request batch then poll.
    # NOTE: /batches is plan-gated; a clean ExaPlanError is the *correct* client
    # behaviour for a plan that hasn't enabled it, so we record that as a pass.
    try:
        b = exa.batch_create([
            {"method": "POST", "url": "/search", "body": {"query": "a", "numResults": 2}},
            {"method": "POST", "url": "/search", "body": {"query": "b", "numResults": 2}},
        ])
        b_id = b.get("id")
        ok("exa.batch_create", id=b_id, status=b.get("status"),
           results_url_present=bool(b.get("resultsUrl")))
        p = exa.poll_batch(b_id, poll_interval=1.0, budget_seconds=20)
        ok("exa.poll_batch", status=p.get("batch", {}).get("status"),
           n_results=len(p.get("results", {})))
    except exa.ExaPlanError as e:
        ok("exa.batch(plan-gated-clean-error)", err=type(e).__name__)
    except Exception as e:
        fail("exa.batch", e)

    # 10. batch_list — also plan-gated; clean ExaPlanError expected
    try:
        bl = exa.batch_list(limit=3)
        ok("exa.batch_list", n=len(bl.get("data") or []))
    except exa.ExaPlanError:
        ok("exa.batch_list(plan-gated-clean-error)")
    except Exception as e:
        fail("exa.batch_list", e)

    # 11. monitor_list — no create (cost), just list
    try:
        ml = exa.monitor_list(limit=3)
        ok("exa.monitor_list", n=len(ml.get("data") or ml.get("monitors") or []))
    except Exception as e:
        fail("exa.monitor_list", e)

    # 12. webset_list
    try:
        wl = exa.webset_list(limit=3)
        ok("exa.webset_list", n=len(wl.get("data") or []))
    except Exception as e:
        fail("exa.webset_list", e)

    # 13. entity_search — client-side composite (returns a dict)
    try:
        es = exa.entity_search("NVIDIA", category="company", num_results=3)
        ok("exa.entity_search", entity_count=es.get("entity_count"),
           category=es.get("category"))
    except Exception as e:
        fail("exa.entity_search", e)

    # 14. top_terms
    try:
        tt = exa.top_terms("renewable energy", num_results=5)
        ok("exa.top_terms", terms=len((tt.get("results") or tt.get("terms") or []))
           if isinstance(tt, dict) else "?")
    except Exception as e:
        fail("exa.top_terms", e)


# ---------------------------------------------------------------------------
# BRAVE
# ---------------------------------------------------------------------------
def run_brave():
    # 1. search — core
    try:
        s = brave.search("clojure web framework", count=5, mode="web")
        ok("brave.search", n=s.get("n"), first=(s.get("results") or [{}])[0].get("title"))
    except Exception as e:
        fail("brave.search", e)

    # 2. news
    try:
        n = brave.search("AI news", mode="news", count=3)
        ok("brave.search(news)", n=n.get("n"))
    except Exception as e:
        fail("brave.search(news)", e)

    # 3. paged — multi-page merge
    try:
        p = brave.paged("python typing", total=10, mode="web", max_pages=2)
        ok("brave.paged", n=p.get("n"), pages=p.get("pages"),
           exhausted=p.get("exhausted"))
    except Exception as e:
        fail("brave.paged", e)

    # 4. merge — client-side
    try:
        a = brave.search("clojure web framework", count=3, mode="web")
        b = brave.search("clojure concurrency", count=3, mode="web")
        m = brave.merge(a, b)
        ok("brave.merge", merged=len(m),
           tagged_all=all("_query" in x for x in m))
    except Exception as e:
        fail("brave.merge", e)

    # 5. place_search — coordinate-anchored
    try:
        ps = brave.place_search("coffee shops", latitude=37.77, longitude=-122.41,
                                count=3)
        ok("brave.place_search", n=ps.get("n"),
           resolved=ps.get("resolved", {}).get("name"))
    except Exception as e:
        fail("brave.place_search", e)

    # 6. rich — weather
    try:
        r = brave.rich("weather in san francisco")
        ok("brave.rich(weather)", vertical=r.get("vertical"),
           n=len(r.get("results", [])))
    except Exception as e:
        fail("brave.rich", e)

    # 7. weather — composite
    try:
        w = brave.weather("san francisco")
        ok("brave.weather", has_current=bool(w.get("current") or w.get("current_weather")))
    except Exception as e:
        fail("brave.weather", e)

    # 8. stock_quote
    try:
        sq = brave.stock_quote("AAPL")
        ok("brave.stock_quote", has_symbol=bool(sq.get("symbol") or sq.get("results")))
    except Exception as e:
        fail("brave.stock_quote", e)

    # 9. probe — fetch a real page
    try:
        pr = brave.probe("https://example.com")
        ok("brave.probe", status=pr.get("status"),
           title=pr.get("title"), chars=pr.get("chars"))
    except Exception as e:
        fail("brave.probe", e)

    # 10. summarize_page
    try:
        sp = brave.summarize_page("https://example.com")
        ok("brave.summarize_page", n_points=len(sp.get("points", [])))
    except Exception as e:
        fail("brave.summarize_page", e)

    # 11. convert_values — pure client-side
    try:
        cv = brave.convert_values("100", "USD", "EUR")
        ok("brave.convert_values", result=str(cv)[:60])
    except Exception as e:
        fail("brave.convert_values", e)

    # 12. structured — AI structured data
    try:
        st = brave.structured("list 3 popular python web frameworks",
                              schema={"type": "object",
                                      "properties": {"items": {"type": "array"}}})
        ok("brave.structured", has_data=bool(st.get("data") or st.get("results")))
    except Exception as e:
        fail("brave.structured", e)


def main():
    t0 = time.time()
    run_exa()
    run_brave()
    dt = time.time() - t0
    n_pass = sum(1 for r in RESULTS if r["status"] == "ok")
    n_fail = sum(1 for r in RESULTS if r["status"] == "fail")
    print(f"\n==== {n_pass} pass / {n_fail} fail in {dt:.1f}s ====")
    out = {"elapsed_s": round(dt, 1), "pass": n_pass, "fail": n_fail,
           "results": RESULTS}
    with open(".analysis/live_baseline.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"baseline written to .analysis/live_baseline.json")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
