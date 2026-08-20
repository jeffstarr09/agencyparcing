#!/usr/bin/env python3
"""
test_extractors.py - The parse must not regress.

Run:  python tests/test_extractors.py

parse_clients.py --selftest prints what the extractors found so you can eyeball
it. This asserts it, so CI catches the case where a tweak to one extractor
quietly costs you clients in another. The expectations below were reviewed by
hand against the fixtures; when you deliberately change extraction behaviour,
update them in the same commit and say why.

Precision matters more than recall here: a client we miss is a gap you can see
in the coverage number, but a wrong client is a row you act on and waste a call
on. So MUST_NOT_FIND is as important as MUST_FIND.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedback  # noqa: E402
import parse_clients  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CASES = [
    {
        "file": "alpine_clients.html",
        "url": "https://alpinedigital.com/clients",
        "agency": {"agency_name": "Alpine Digital", "agency_domain": "alpinedigital.com",
                   "vertical": "home_services"},
        "must_find": {
            # alt text, the most reliable source
            "Summit Roofing": {"confidence": "high"},
            "Desert Air HVAC": {"confidence": "high"},
            "Verde Pest Control": {"confidence": "high"},
            # alt text plus a live outbound link: name and domain both
            "Stone Creek Landscaping": {"confidence": "high",
                                        "domain": "stonecreeklandscaping.com"},
            "Mesa Garage Doors": {"confidence": "high", "domain": "mesagaragedoors.com"},
            # linked from a testimonial rather than the logo wall
            "Block Renovation": {"confidence": "high", "domain": "blockrenovation.com"},
            # no alt text at all - filename is the only source
            "Rio Verde Garage Doors": {"confidence": "medium"},
            # alt text was the useless string "client logo"
            "Bright Smile Dental": {"confidence": "medium"},
        },
        "must_not_find": [
            "Alpine Digital",   # the agency's own header logo
            "Some Web Shop",    # "Site by ..." credit link
            "Divider", "Quote Icon",   # decorative theme assets
            "Facebook", "Instagram", "LinkedIn", "TikTok",  # footer social icons
            "WP Engine",        # hosting credit
        ],
        "min_clients": 8,
    },
    {
        "file": "northbeam_work.html",
        "url": "https://northbeammedia.com/our-work",
        "agency": {"agency_name": "Northbeam Media", "agency_domain": "northbeammedia.com",
                   "vertical": "local_health"},
        "must_find": {
            # written-out name from the case-study title, domain from the link
            "Lakeside Dermatology": {"confidence": "high", "domain": "lakesidederm.com"},
            "Harbor Point Med Spa": {"confidence": "high",
                                     "domain": "harborpointmedspa.com"},
            "Foothill Solar": {"confidence": "high", "domain": "foothillsolar.net"},
            # anchor text was the CTA "Shop TrueNorth"; the title has the full name
            "TrueNorth Outfitters": {"confidence": "high",
                                     "domain": "truenorthoutfitters.com"},
            # title only, no link anywhere - correctly low and domainless
            "Olive Branch Orthodontics": {"confidence": "low", "domain": ""},
        },
        "must_not_find": [
            "Northbeam Media",          # itself
            "Google Partner", "Google Badge", "Meta Badge",  # credential badges
            "Meta Business Partner", "Clutch", "Clutch Reviews", "Trustpilot",
        ],
        "min_clients": 5,
    },
]

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"\n        {detail}" if detail and not condition else ""))


def parse(case):
    path = os.path.join(FIXTURES, case["file"])
    with open(path, encoding="utf-8") as f:
        html = f.read()
    raw = []
    for _, fn in parse_clients.EXTRACTORS:
        raw.extend(fn(html, case["url"], case["agency"]))
    return parse_clients.merge_clients(raw, case["agency"])


def test_fixtures():
    for case in CASES:
        print(f"\n{case['file']}")
        merged = parse(case)
        by_name = {c["client_name"]: c for c in merged}
        found = set(by_name)

        check(f"at least {case['min_clients']} clients",
              len(merged) >= case["min_clients"],
              f"got {len(merged)}: {sorted(found)}")

        for name, want in case["must_find"].items():
            if name not in by_name:
                check(f"finds {name!r}", False, f"got {sorted(found)}")
                continue
            got = by_name[name]
            ok = got["confidence"] == want["confidence"]
            detail = f"confidence {got['confidence']!r}, wanted {want['confidence']!r}"
            if "domain" in want:
                ok = ok and got["client_domain"] == want["domain"]
                detail += f"; domain {got['client_domain']!r}, wanted {want['domain']!r}"
            check(f"finds {name!r} at {want['confidence']}"
                  + (f" with {want['domain'] or 'no domain'}" if "domain" in want else ""),
                  ok, detail)

        for name in case["must_not_find"]:
            check(f"does not record {name!r}", name not in found,
                  f"{name!r} was recorded as a client")


def test_js_page_is_never_a_zero():
    print("\njs_portfolio.html")
    case = {"file": "js_portfolio.html", "url": "https://crestlinegrowth.com/portfolio",
            "agency": {"agency_name": "Crestline Growth",
                       "agency_domain": "crestlinegrowth.com", "vertical": "dtc"}}
    merged = parse(case)
    check("extracts nothing from a JS-rendered page", len(merged) == 0,
          f"got {[c['client_name'] for c in merged]}")

    with open(os.path.join(FIXTURES, case["file"]), encoding="utf-8") as f:
        html = f.read()
    is_js, markers = parse_clients.looks_javascript_rendered(html)
    check("detects it as JS-rendered, so the agency is flagged for review "
          "instead of recorded with zero clients", is_js, f"markers={markers}")


def test_static_page_with_no_clients_is_not_flagged_as_js():
    print("\nfalse-positive guard on the JS detector")
    plain = ("<!doctype html><html><head><title>About</title></head><body>"
             "<h1>About us</h1>" + "<p>We are a small team of paid media buyers "
             "working with home service brands across the southwest. " * 12 +
             "</body></html>")
    is_js, markers = parse_clients.looks_javascript_rendered(plain)
    check("a plain text-heavy page is not called JS-rendered", not is_js,
          f"markers={markers}")


def test_name_from_filename():
    print("\nfilename parsing")
    cases = [
        ("/wp-content/uploads/2024/01/summit-roofing-logo-300x120.png", "Summit Roofing"),
        ("/uploads/acme-plumbing-logo@2x.png", "Acme Plumbing"),
        ("/media/2023/verde-pest-control-logo-bw.webp", "Verde Pest Control"),
        ("/img/a3f9c2e18b7d4e5f6a7b8c9d.png", ""),          # hashed CMS filename
        ("/assets/logo.svg", ""),                            # nothing but noise
    ]
    for src, want in cases:
        got = parse_clients.name_from_filename(src)
        check(f"{os.path.basename(src)} -> {want or '(nothing)'!r}", got == want,
              f"got {got!r}")


if __name__ == "__main__":
    # Learned rules are user state; a CI run must test the shipped behaviour.
    parse_clients.RULES = feedback.Rules(
        __import__("json").loads(__import__("json").dumps(feedback.EMPTY_RULES)))
    for fn in (test_fixtures, test_js_page_is_never_a_zero,
               test_static_page_with_no_clients_is_not_flagged_as_js,
               test_name_from_filename):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
