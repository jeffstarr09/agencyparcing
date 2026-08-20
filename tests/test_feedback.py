#!/usr/bin/env python3
"""
test_feedback.py - The learning loop must actually change the parse.

Run:  python tests/test_feedback.py

The whole point of feedback.py is that a verdict you write once changes what the
parser does forever after. These tests hold that contract:

  * every verdict type produces the rule it promises
  * a malformed verdict is rejected with a reason, not silently dropped
  * a suppressed name stays suppressed when another extractor re-derives it
    from the domain (the bug that shipped in the first version of this loop)
  * confidence retuning needs enough samples before it moves
  * learning is idempotent - replaying the same verdicts twice is a no-op
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedback  # noqa: E402

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"\n        {detail}" if detail and not condition else ""))


def fresh():
    import json
    return json.loads(json.dumps(feedback.EMPTY_RULES))


def row(**kw):
    base = {"entity_type": "client", "entity_key": "", "agency_domain": "acme.com",
            "verdict": "", "correct_value": "", "method": "alt_text", "note": ""}
    base.update(kw)
    return base


def test_each_verdict_makes_its_rule():
    print("\nverdicts produce rules")
    rules, report = feedback.learn([
        row(entity_key="Junk Row", verdict="bad"),
        row(entity_key="Wrong Name", verdict="rename", correct_value="Right Name"),
        row(entity_key="No Domain", verdict="wrong_domain", correct_value="real.com"),
        row(entity_key="Good One", verdict="good"),
        row(entity_key="", verdict="missed", correct_value="Never Found Ltd"),
    ], fresh())
    check("5 verdicts applied", report["applied"] == 5, str(report))
    check("bad -> per-agency blocklist",
          "Junk Row" in rules["agency_blocklist"].get("acme.com", []),
          str(rules["agency_blocklist"]))
    check("rename -> name correction",
          rules["name_corrections"].get("Wrong Name") == "Right Name",
          str(rules["name_corrections"]))
    check("wrong_domain -> domain correction",
          rules["domain_corrections"].get("No Domain") == "real.com",
          str(rules["domain_corrections"]))
    check("missed -> recall miss",
          "Never Found Ltd" in rules["recall_misses"].get("acme.com", []),
          str(rules["recall_misses"]))
    stats = rules["method_stats"]["alt_text"]
    check("good and bad both counted", stats["good"] == 1 and stats["bad"] == 3,
          str(stats))


def test_malformed_verdicts_are_reported():
    print("\nmalformed verdicts are rejected with a reason")
    rules, report = feedback.learn([
        row(entity_key="X", verdict="frobnicate"),
        row(entity_key="Y", verdict="rename"),                       # no correct_value
        row(entity_key="Z", verdict="wrong_domain", correct_value="not a domain"),
        row(entity_key="", verdict="bad"),                           # no entity_key
    ], fresh())
    check("nothing applied", report["applied"] == 0, str(report["applied"]))
    check("all four explained", len(report["skipped"]) == 4,
          "\n        ".join(report["skipped"]))
    check("unknown verdict named", any("frobnicate" in s for s in report["skipped"]))
    check("missing correct_value named",
          any("needs a correct_value" in s for s in report["skipped"]))
    check("bad domain named", any("not a domain" in s for s in report["skipped"]))


def test_suppression_survives_a_domain_derived_alias():
    print("\na suppressed name stays suppressed under a domain-derived alias")
    rules, _ = feedback.learn(
        [row(entity_key="Northbeam Media", agency_domain="alpine.com", verdict="bad")],
        fresh())
    r = feedback.Rules(rules)
    check("blocks the written form",
          r.is_junk("Northbeam Media", "", "alpine.com"))
    check("blocks the run-together form the parser derives from the domain",
          r.is_junk("Northbeammedia", "northbeammedia.com", "alpine.com"))
    check("blocks via the domain stem even with an odd label",
          r.is_junk("Northbeam  media", "northbeammedia.com", "alpine.com"))
    check("does not block an unrelated client",
          not r.is_junk("Summit Roofing", "summitroofing.com", "alpine.com"))
    check("scoped to the agency that rejected it",
          not r.is_junk("Northbeam Media", "", "someoneelse.com"))


def test_retune_needs_enough_samples():
    print("\nconfidence retuning waits for enough reviews")
    few = [row(entity_key=f"C{i}", verdict="bad", method="image_filename")
           for i in range(feedback.MIN_SAMPLES_TO_RETUNE - 1)]
    rules, _ = feedback.learn(few, fresh())
    check(f"no override below {feedback.MIN_SAMPLES_TO_RETUNE} samples",
          "image_filename" not in rules["confidence_overrides"],
          str(rules["confidence_overrides"]))

    many = [row(entity_key=f"C{i}", verdict="bad", method="image_filename")
            for i in range(feedback.MIN_SAMPLES_TO_RETUNE + 1)]
    rules, _ = feedback.learn(many, fresh())
    check("a method reviewed as consistently wrong is demoted to low",
          rules["confidence_overrides"].get("image_filename") == "low",
          str(rules["confidence_overrides"]))

    good = [row(entity_key=f"C{i}", verdict="good", method="case_study_title")
            for i in range(feedback.MIN_SAMPLES_TO_RETUNE + 1)]
    rules, _ = feedback.learn(good, fresh())
    check("a method reviewed as consistently right is promoted to high",
          rules["confidence_overrides"].get("case_study_title") == "high",
          str(rules["confidence_overrides"]))


def test_learning_is_idempotent():
    print("\nreplaying the same verdicts changes nothing")
    verdicts = [
        row(entity_key="Junk", verdict="bad"),
        row(entity_key="Old", verdict="rename", correct_value="New"),
    ]
    once, _ = feedback.learn(verdicts, fresh())
    twice, _ = feedback.learn(verdicts, once)
    for field in ("junk_labels", "agency_blocklist", "name_corrections",
                  "domain_corrections", "recall_misses"):
        check(f"{field} unchanged on replay", once[field] == twice[field],
              f"{once[field]} != {twice[field]}")


def test_rules_apply_in_the_parser():
    print("\nparse_clients honours the rules")
    import parse_clients
    rules, _ = feedback.learn([
        row(entity_key="Bad Client", agency_domain="acme.com", verdict="bad"),
        row(entity_key="Typo Co", agency_domain="acme.com", verdict="rename",
            correct_value="Typo Co."),
    ], fresh())
    parse_clients.RULES = feedback.Rules(rules)
    agency = {"agency_name": "Acme", "agency_domain": "acme.com", "vertical": ""}

    check("a suppressed name is not a plausible client",
          not parse_clients.plausible_name("Bad Client", "Acme", "acme.com"))

    merged = parse_clients.merge_clients([
        {"client_name": "Typo Co", "client_domain": "", "source": "s",
         "confidence": "medium", "_method": "alt_text"},
        {"client_name": "Bad Client", "client_domain": "", "source": "s",
         "confidence": "high", "_method": "outbound_link"},
    ], agency)
    names = [m["client_name"] for m in merged]
    check("rename applied in the merge", "Typo Co." in names, str(names))
    check("suppressed row dropped from the merge", "Bad Client" not in names, str(names))
    renamed = next((m for m in merged if m["client_name"] == "Typo Co."), None)
    check("a hand correction is treated as high confidence",
          renamed and renamed["confidence"] == "high",
          str(renamed))
    parse_clients.RULES = feedback.Rules(fresh())


if __name__ == "__main__":
    for fn in (test_each_verdict_makes_its_rule,
               test_malformed_verdicts_are_reported,
               test_suppression_survives_a_domain_derived_alias,
               test_retune_needs_enough_samples,
               test_learning_is_idempotent,
               test_rules_apply_in_the_parser):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
