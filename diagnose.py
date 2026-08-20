#!/usr/bin/env python3
"""
diagnose.py - Why did a run find nothing?

A run that finds nothing looks identical to a run where there was nothing to
find, and those are completely different problems. This works out which one you
had and says so in a sentence, rather than leaving you to read a failures CSV.

It checks, in order:

  1. Can this machine reach the internet at all?
  2. For each discovery source: does its robots.txt permit us? If not, that is
     the answer and we stop there - we do not fetch a page we have been told
     not to.
  3. If permitted: does a real lookup come back, and can we parse agencies out
     of it?

    python diagnose.py            # run it from a terminal
    (or press Diagnose in the app)
"""

import sys
from urllib.parse import quote_plus, urlparse

import common
import find_agencies

# Somewhere harmless and stable, purely to prove the network works.
REACHABILITY_PROBE = "https://example.com"


def _robots_verdict(fetcher, url):
    """
    Does this host's robots.txt permit us to fetch this path?

    Returns (allowed, note). A host with no robots.txt is allowed by convention.
    """
    host = urlparse(url).netloc
    parser = fetcher.robots_for(url)
    if parser is None:
        return True, f"{host} publishes no robots.txt we could read (so: allowed)"
    try:
        allowed = parser.can_fetch(fetcher.user_agent, url)
    except Exception:
        return True, f"{host} robots.txt could not be interpreted (so: allowed)"
    path = urlparse(url).path or "/"
    if allowed:
        return True, f"{host} robots.txt allows {path}"
    return False, f"{host} robots.txt disallows {path}"


def check_internet(fetcher):
    page = fetcher.get(REACHABILITY_PROBE)
    if page.ok:
        return {"id": "internet", "label": "Internet connection", "ok": True,
                "detail": "Reachable."}
    return {
        "id": "internet", "label": "Internet connection", "ok": False,
        "detail": f"Could not reach {REACHABILITY_PROBE} ({page.failure}). "
                  f"Nothing else can work until this does. If you are on a work "
                  f"network, a proxy or firewall may be in the way.",
        "fix": "Check your connection, or try a different network.",
    }


def check_search(fetcher, engine):
    """Test one search engine end to end, stopping at the first real blocker."""
    template = find_agencies.SEARCH_ENGINES[engine]
    query = '"hvac marketing agency" Phoenix AZ'
    url = template.format(q=quote_plus(query))
    label = f"Web search ({engine})"

    allowed, note = _robots_verdict(fetcher, url)
    if not allowed:
        return {
            "id": f"search:{engine}", "label": label, "ok": False,
            "detail": f"{note}. Search engines forbid automated searching in "
                      f"their robots.txt, and this app honours that, so it never "
                      f"asks. This is why a run finds no agencies.",
            "fix": "Use 'Add agencies from your browser' instead — you run the "
                   "search yourself and hand the results over.",
            "blocker": "robots",
        }

    page = fetcher.get(url)
    if not page.ok:
        return {"id": f"search:{engine}", "label": label, "ok": False,
                "detail": f"{note}, but the lookup failed: {page.failure}.",
                "fix": "Often a temporary block. Try again later, or use "
                       "'Add agencies from your browser'.",
                "blocker": "fetch"}

    if find_agencies._looks_like_a_wall(page.text):
        return {"id": f"search:{engine}", "label": label, "ok": False,
                "detail": f"{note}, but it answered with a bot challenge instead "
                          f"of results.",
                "fix": "Use 'Add agencies from your browser'.",
                "blocker": "challenge"}

    hits = find_agencies._search_result_domains(page.text, engine)
    if not hits:
        return {"id": f"search:{engine}", "label": label, "ok": False,
                "detail": f"{note}, and it answered, but no agency websites could "
                          f"be read out of the page ({len(page.text)} characters). "
                          f"Their page layout has probably changed.",
                "fix": "Use 'Add agencies from your browser' while this is fixed.",
                "blocker": "parse"}

    return {"id": f"search:{engine}", "label": label, "ok": True,
            "detail": f"Working — found {len(hits)} sites for a test search.",
            "sample": [d for d, _ in hits[:5]]}


def check_directory(fetcher, key):
    cfg = find_agencies.DIRECTORIES[key]
    url = cfg["listing"].format(term=quote_plus("hvac"), geo=quote_plus("Phoenix AZ"),
                                geo_slug="phoenix-az")
    label = cfg["name"]

    allowed, note = _robots_verdict(fetcher, url)
    if not allowed:
        return {"id": f"dir:{key}", "label": label, "ok": False,
                "detail": f"{note}. Not used.", "blocker": "robots"}

    page = fetcher.get(url)
    if not page.ok:
        return {"id": f"dir:{key}", "label": label, "ok": False,
                "detail": f"Lookup failed: {page.failure}.", "blocker": "fetch"}
    if find_agencies._looks_like_a_wall(page.text):
        return {"id": f"dir:{key}", "label": label, "ok": False,
                "detail": "Answered with a bot challenge. These directories sit "
                          "behind bot protection and usually refuse.",
                "fix": "Save the listing page from your browser and import it.",
                "blocker": "challenge"}
    profiles = cfg["profile_link"].findall(page.text)
    if not profiles:
        return {"id": f"dir:{key}", "label": label, "ok": False,
                "detail": "Answered, but no agency listings could be read out of "
                          "the page. Their layout has probably changed.",
                "blocker": "parse"}
    return {"id": f"dir:{key}", "label": label, "ok": True,
            "detail": f"Working — {len(profiles)} listings on a test page."}


def run(engines=("duckduckgo", "bing", "google"), directories=("clutch", "upcity"),
        delay=1.0):
    """Everything, in order. Returns a dict the app renders and a plain summary."""
    fetcher = common.Fetcher(delay=delay, verbose=False)
    checks = [check_internet(fetcher)]

    if checks[0]["ok"]:
        for engine in engines:
            checks.append(check_search(fetcher, engine))
        for key in directories:
            checks.append(check_directory(fetcher, key))

    working = [c for c in checks[1:] if c.get("ok")]
    robots_blocked = [c for c in checks[1:] if c.get("blocker") == "robots"]

    if not checks[0]["ok"]:
        verdict = ("This machine can't reach the internet, so nothing can work "
                   "yet. Fix that first.")
    elif working:
        verdict = (f"{len(working)} source(s) working. If a run still finds "
                   f"nothing, it is looking in the wrong places rather than "
                   f"being blocked.")
    elif robots_blocked and len(robots_blocked) >= len(checks) - 1 - 1:
        verdict = ("Every automatic source refuses automated access in its "
                   "robots.txt, and this app honours that. That is why your run "
                   "found nothing — it isn't that there are no agencies. Use "
                   "'Add agencies from your browser' to get going.")
    else:
        verdict = ("No source is currently working — they are blocking us or "
                   "have changed their pages. Use 'Add agencies from your "
                   "browser' to get going.")

    return {"checks": checks, "verdict": verdict,
            "any_working": bool(working),
            "stats": fetcher.stats}


def main():
    print("\nChecking what's working. This takes a few seconds.\n")
    result = run()
    for c in result["checks"]:
        mark = "ok  " if c.get("ok") else "!!  "
        print(f"  {mark}{c['label']}")
        print(f"      {c['detail']}")
        if c.get("fix"):
            print(f"      -> {c['fix']}")
        if c.get("sample"):
            print(f"      e.g. {', '.join(c['sample'])}")
        print()
    print(f"  {result['verdict']}\n")
    return 0 if result["any_working"] else 1


if __name__ == "__main__":
    sys.exit(main())
