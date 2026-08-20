#!/usr/bin/env python3
"""
parse_clients.py - Pull an agency's client list off its own site.

    python parse_clients.py alpinedigital.com --show          # spot-check one
    python parse_clients.py agencies.csv -o clients.csv --sheet
    python parse_clients.py alpinedigital.com --dry-run       # parse, write nothing
    python parse_clients.py --selftest                        # run the fixtures

Four extraction methods run on every candidate page and their results are merged.
Confidence is set by method, because the methods are not equally trustworthy:

  alt_text          high    <img alt="Summit Roofing"> on a logo wall. The single
                            most reliable source, when the agency bothered to fill
                            it in.
  outbound_link     high    A link leaving the agency's domain from a client or
                            work page. Also the only method that yields a domain
                            rather than a name, which is what pixel_check needs.
  image_filename    medium  /uploads/2024/acme-plumbing-logo.png -> Acme Plumbing.
                            Right most of the time, and wrong in ways you can see.
  case_study_title  low     "How We Grew Acme Plumbing 300%" -> Acme Plumbing.
                            Pattern matching on prose. Review every one of these.

Recall is the honest limit here. Logo walls are images, many are lazy-loaded, and
some are carousels a static fetch never sees. Expect 50-60% on a static fetch.
When a page is clearly JavaScript-rendered and yields nothing, the agency is
marked needs_manual_review rather than recorded with zero clients - a false zero
is worse than an admitted gap, because a false zero silently removes a live
prospect from the list.

Domains are resolved from evidence, never invented. A client whose domain cannot
be established from an outbound link on the page keeps a blank domain, and
--verify-guess will only fill it in after fetching the guessed site and finding
the brand name on it.
"""

import argparse
import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import common
import feedback

# Rules learned from your verdicts in the Feedback tab. Loaded once at import so
# every extractor sees the same set; see feedback.py for how they get here.
RULES = feedback.Rules()

# --------------------------------------------------------------------------
# Where client lists live
# --------------------------------------------------------------------------

CANONICAL_PATHS = [
    "/clients", "/our-clients", "/work", "/our-work", "/case-studies",
    "/case-study", "/portfolio", "/results", "/who-we-work-with", "/projects",
    "/success-stories", "/customers", "/partners", "/testimonials",
]

CLIENT_URL_WORDS = [
    "client", "work", "case-stud", "casestud", "portfolio", "result",
    "who-we-work", "project", "success-stor", "customer", "testimonial",
    "our-brands", "brands",
]

MAX_PAGES = 12  # per agency

# --------------------------------------------------------------------------
# Exclusion lists
#
# These are the difference between a client list and a pile of footer noise.
# Anything here is never a client, however it was found.
# --------------------------------------------------------------------------

BOILERPLATE_DOMAINS = {
    # social
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
    "snapchat.com", "reddit.com", "threads.net", "vimeo.com", "tumblr.com",
    "whatsapp.com", "telegram.org", "medium.com", "substack.com", "yelp.com",
    "nextdoor.com", "glassdoor.com", "indeed.com", "angi.com", "angieslist.com",
    "houzz.com", "thumbtack.com", "bbb.org", "trustpilot.com", "g2.com",
    "capterra.com", "crunchbase.com", "producthunt.com",
    # directories and badges
    "clutch.co", "sortlist.com", "designrush.com", "upcity.com",
    "agencyspotter.com", "goodfirms.co", "themanifest.com", "expertise.com",
    "agencyvista.com", "partners.tiktok.com", "business.tiktok.com",
    # platforms, CDNs, analytics, hosting, CMS
    "google.com", "googleapis.com", "gstatic.com", "googletagmanager.com",
    "google-analytics.com", "googleadservices.com", "doubleclick.net",
    "youtube-nocookie.com", "recaptcha.net", "cloudflare.com", "cloudfront.net",
    "jsdelivr.net", "unpkg.com", "bootstrapcdn.com", "fontawesome.com",
    "typekit.net", "fonts.net", "amazonaws.com", "akamaized.net", "imgix.net",
    "wp.com", "wordpress.com", "wordpress.org", "wpengine.com", "kinsta.com",
    "siteground.com", "godaddy.com", "wix.com", "squarespace.com", "webflow.com",
    "webflow.io", "shopify.com", "myshopify.com", "hubspot.com", "hubspotusercontent.com",
    "mailchimp.com", "klaviyo.com", "activecampaign.com", "calendly.com",
    "hotjar.com", "segment.com", "intercom.com", "drift.com", "zendesk.com",
    "typeform.com", "airtable.com", "notion.so", "github.com", "gitlab.com",
    "apple.com", "microsoft.com", "adobe.com", "salesforce.com", "zoom.us",
    "vercel.app", "netlify.app", "herokuapp.com", "gravatar.com", "w3.org",
    "schema.org", "creativecommons.org", "adobe.io", "stripe.com", "paypal.com",
    "semrush.com", "ahrefs.com", "moz.com", "similarweb.com", "builtwith.com",
}

# Alt text and anchor text that is chrome, not a brand.
JUNK_LABELS = {
    "", "logo", "logos", "client logo", "client logos", "clients", "client",
    "image", "img", "photo", "picture", "icon", "banner", "background", "bg",
    "arrow", "star", "stars", "quote", "divider", "spacer", "placeholder",
    "thumbnail", "thumb", "avatar", "profile", "header", "footer", "menu",
    "search", "close", "play", "pause", "next", "previous", "prev", "read more",
    "learn more", "view case study", "case study", "see the work", "view work",
    "our work", "portfolio", "home", "about", "about us", "contact", "contact us",
    "services", "blog", "news", "careers", "privacy policy", "terms",
    "facebook", "instagram", "twitter", "linkedin", "youtube", "tiktok",
    "pinterest", "snapchat", "google", "google partner", "meta business partner",
    "clutch", "clutch reviews", "trustpilot", "certified", "badge", "award",
    "sitemap", "find us", "shop now", "get a quote", "free audit", "testimonial",
    "testimonials", "team", "our team", "results", "case studies",
}

# Tokens stripped out of an image filename before it becomes a name.
FILENAME_NOISE = {
    "logo", "logos", "logotype", "wordmark", "icon", "brand", "branding",
    "client", "clients", "customer", "partner", "color", "colour", "colored",
    "bw", "b-w", "grey", "gray", "greyscale", "grayscale", "white", "black",
    "dark", "light", "mono", "monochrome", "transparent", "trans", "alpha",
    "final", "new", "old", "copy", "v1", "v2", "v3", "small", "med", "medium",
    "large", "sm", "md", "lg", "xl", "thumb", "thumbnail", "hover", "active",
    "web", "webp", "png", "jpg", "jpeg", "svg", "gif", "retina", "hd", "min",
    "cropped", "resized", "scaled", "edit", "edited", "featured", "img", "image",
    "site", "header", "footer", "square", "horizontal", "vertical", "stacked",
    "primary", "secondary", "alt", "default", "placeholder",
}

# Filenames that are a credential, an award, or a review platform - never a client.
BADGE_FILENAME_WORDS = (
    "badge", "award", "certified", "certification", "accredit", "partner-badge",
    "google-partner", "meta-business", "premier", "review", "rating", "seal",
    "trusted", "verified", "member", "association", "bbb", "inc5000", "top-",
)

# Directories whose images are theme furniture, never client logos.
THEME_PATH_WORDS = ("/themes/", "/theme/", "/plugins/", "/icons/", "/icon/",
                    "/flags/", "/emoji/", "/fonts/", "/sprite", "/ui/", "/svg/icons")

# Paths that do hold client logos on the CMSes agencies actually use.
LOGO_PATH_WORDS = ("/uploads/", "/wp-content/", "/media/", "/assets/", "/images/",
                   "/img/", "/logos/", "/logo/", "/clients/", "/client/",
                   "/brands/", "/files/", "/storage/", "/static/")

# Markers that a page builds its content in the browser. When one of these is
# present and nothing was extracted, the answer is "we could not see it", not zero.
JS_MARKERS = (
    "enable javascript", "please enable js", "__next_data__", "data-reactroot",
    "id=\"root\"", "id='root'", "id=\"app\"", "id='app'", "ng-app", "v-app",
    "data-v-app", "webpack", ".bundle.js", "chunk.js", "swiper-container",
    "owl-carousel", "slick-slider", "elementor-loop", "wp-block-query",
    "data-elementor-type", "vue.js", "react-dom", "gatsby", "nuxt",
)

# "Site by <a>", "Powered by <a>", "Hosted on <a>" - a vendor credit, not a client.
_CREDIT_LINK_RE = re.compile(
    r"(?:site|website|web\s*design|design(?:ed)?|develop(?:ed)?|built|build|made|"
    r"crafted|powered|hosted|template)\s+(?:by|on|with|using)\b[^<]{0,40}"
    r"(?:<[^a][^>]*>\s*){0,3}<a\b[^>]*?href\s*=\s*[\"\']([^\"\']+)",
    re.I | re.S)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z0-9_:\-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))')
_A_TAG_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.I | re.S)
_BG_IMG_RE = re.compile(r'background(?:-image)?\s*:\s*url\(\s*["\']?([^"\')]+)', re.I)
_HEADING_RE = re.compile(r"<(h1|h2|title)[^>]*>(.*?)</\1>", re.I | re.S)
_DIM_SUFFIX_RE = re.compile(r"[-_]\d{2,4}x\d{2,4}$|[-_@]\d+x$|[-_]\d{3,4}$")


# --------------------------------------------------------------------------
# Name hygiene
# --------------------------------------------------------------------------

def _strip_tags(s):
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


def clean_label(raw):
    """Normalise a human-written label. Returns "" when it isn't a brand name."""
    s = _strip_tags(raw)
    s = (s.replace("&amp;", "&").replace("&#38;", "&").replace("&#39;", "'")
          .replace("&rsquo;", "'").replace("&quot;", '"').replace("&nbsp;", " "))
    s = re.sub(r"\s+", " ", s).strip(" \t\n\r-–—|·•:,")
    # "Summit Roofing logo" / "logo of Summit Roofing" / "Summit Roofing - client"
    s = re.sub(r"\b(?:company\s+)?logos?\b", " ", s, flags=re.I)
    s = re.sub(r"^\s*(?:logo of|image of|photo of|client[:\-]?)\s*", "", s, flags=re.I)
    s = re.sub(r"\s*[-–—|·•]\s*(?:client|customer|partner|case study)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" \t\n\r-–—|·•:,.")
    return s


def plausible_name(name, agency_name="", agency_domain=""):
    """
    Is this a brand name? Rejects chrome, sentences, URLs, and the agency itself.
    Deliberately strict: a wrong client name costs more to unpick later than a
    missed one costs to find.
    """
    if not name:
        return False
    low = name.lower().strip()
    if low in JUNK_LABELS:
        return False
    # Anything you marked `bad` in the Feedback tab, globally or for this agency.
    if RULES.is_junk(name, agency_domain=agency_domain):
        return False
    if len(name) < 2 or len(name) > 60:
        return False
    if not re.search(r"[A-Za-z]", name):
        return False
    if len(name.split()) > 6:
        return False              # a sentence, not a brand
    if re.match(r"^\d+$", low) or re.match(r"^[\d\s%$.,+-]+$", low):
        return False
    if "@" in name or low.startswith(("http", "www.", "/")) or low.endswith((".com", ".net", ".org")):
        return False
    if re.search(r"\.(png|jpe?g|svg|webp|gif)$", low):
        return False
    # The agency is not its own client. Compared on the squashed form as well as
    # on tokens, because "Alpine Digital" and alpinedigital.com share no tokens
    # at all - and the header logo on every page carries exactly that alt text.
    if agency_domain and _same_brand(name, agency_domain):
        return False
    if agency_name and _squash(name) == _squash(agency_name):
        return False
    agency_tokens = set(_tokens(agency_name)) | set(_tokens(agency_domain.split(".")[0]
                                                          if agency_domain else ""))
    name_tokens = set(_tokens(name))
    if name_tokens and agency_tokens and name_tokens <= agency_tokens:
        return False
    return True


def _tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 1]


def name_key(name):
    """Merge key: 'Canyon Plumbing Co.' and 'canyon plumbing co' are one client."""
    toks = _tokens(name)
    drop = {"the", "inc", "llc", "ltd", "co", "corp", "company", "group", "and"}
    core = [t for t in toks if t not in drop] or toks
    return " ".join(core)


# --------------------------------------------------------------------------
# Extractor 1: alt text  (high)
# --------------------------------------------------------------------------

def _img_attrs(tag):
    out = {}
    for m in _ATTR_RE.finditer(tag):
        val = m.group(2) or m.group(3) or m.group(4) or ""
        out[m.group(1).lower()] = val
    return out


def extract_alt_text(html, page_url, agency):
    found = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        attrs = _img_attrs(tag)
        src = _best_src(attrs)
        if src and _is_theme_asset(src):
            continue
        for attr in ("alt", "title", "aria-label"):
            name = clean_label(attrs.get(attr, ""))
            if plausible_name(name, agency.get("agency_name", ""), agency.get("agency_domain", "")):
                found.append({"client_name": name, "source": f"alt_text:{page_url}",
                              "confidence": "high", "_method": "alt_text",
                              "_src": src})
                break
    return found


def _best_src(attrs):
    """The real image URL, across every lazy-loading attribute in the wild."""
    for attr in ("data-lazy-src", "data-src", "data-original", "data-lazy",
                 "data-echo", "data-img", "src"):
        v = (attrs.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return v
    for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
        v = (attrs.get(attr) or "").strip()
        if v:
            first = v.split(",")[0].strip().split(" ")[0]
            if first and not first.startswith("data:"):
                return first
    return ""


def _is_theme_asset(src):
    """Theme furniture, or a credential badge. Either way, not a client logo."""
    low = (src or "").lower()
    if any(w in low for w in THEME_PATH_WORDS):
        return True
    stem = os.path.basename(urlparse(low).path or low)
    return any(w in stem for w in BADGE_FILENAME_WORDS)


# Anchor text is routinely a call to action wrapped around the brand:
# "Shop TrueNorth", "Visit Harbor Point". Strip the verb, keep the brand.
_CTA_PREFIX_RE = re.compile(
    r"^(?:shop|visit|view|see|explore|discover|browse|read|check\s+out|go\s+to|"
    r"learn\s+about|meet)\s+(?:the\s+)?", re.I)
_CTA_SUFFIX_RE = re.compile(
    r"\s+(?:website|site|online|now|here|case\s+study|story|project|work)$", re.I)


def strip_cta(label):
    out = _CTA_SUFFIX_RE.sub("", _CTA_PREFIX_RE.sub("", label or "")).strip(" .,:-–—")
    return out if len(out) >= 2 else label


# --------------------------------------------------------------------------
# Extractor 2: image filename  (medium)
# --------------------------------------------------------------------------

def name_from_filename(src):
    """
    /uploads/2024/01/summit-roofing-logo-300x120.png -> "Summit Roofing"

    Strips the path, the extension, dimension and retina suffixes, and the
    stock noise tokens agencies put in logo filenames. Returns "" when what's
    left isn't a name.
    """
    if not src:
        return ""
    path = urlparse(src).path or src
    stem = os.path.basename(path)
    stem = re.sub(r"\.(png|jpe?g|svg|webp|gif|avif)$", "", stem, flags=re.I)
    if not stem:
        return ""
    stem = re.sub(r"@\d+x$", "", stem, flags=re.I)
    prev = None
    while prev != stem:                       # "-logo-300x120-2x" needs a few passes
        prev = stem
        stem = _DIM_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"[%_+]", "-", stem)
    parts = [p for p in re.split(r"[-\s.]+", stem) if p]
    # Hashed CMS filenames carry no name at all.
    if len(parts) == 1 and (len(parts[0]) > 24 or re.fullmatch(r"[0-9a-f]{8,}", parts[0], re.I)):
        return ""
    kept = [p for p in parts if p.lower() not in FILENAME_NOISE
            and not re.fullmatch(r"\d{1,4}", p)
            and not re.fullmatch(r"[0-9a-f]{8,}", p, re.I)]
    if not kept:
        return ""
    return " ".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in kept)


def extract_image_filenames(html, page_url, agency):
    found = []
    srcs = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        src = _best_src(_img_attrs(tag))
        if src:
            srcs.append(src)
    srcs += _BG_IMG_RE.findall(html or "")
    for src in srcs:
        if _is_theme_asset(src):
            continue
        low = src.lower()
        if not any(w in low for w in LOGO_PATH_WORDS):
            continue
        name = clean_label(name_from_filename(src))
        if plausible_name(name, agency.get("agency_name", ""), agency.get("agency_domain", "")):
            found.append({"client_name": name, "source": f"image_filename:{src}",
                          "confidence": "medium", "_method": "image_filename",
                          "_src": src})
    return found


# --------------------------------------------------------------------------
# Extractor 3: outbound links  (high) - the only method that yields a domain
# --------------------------------------------------------------------------

def _credit_domains(html, page_url):
    """Domains linked as 'Site by ...' / 'Powered by ...'. Vendors, not clients."""
    out = set()
    for href in _CREDIT_LINK_RE.findall(html or ""):
        rd = common.root_domain(urljoin(page_url, href))
        if rd:
            out.add(rd)
    return out


def extract_outbound_links(html, page_url, agency):
    agency_domain = common.root_domain(agency.get("agency_domain", "")) or ""
    credits = _credit_domains(html, page_url)
    found = []
    for attrs_blob, inner in _A_TAG_RE.findall(html or ""):
        attrs = _img_attrs("<a " + attrs_blob + ">")
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(page_url, href)
        rd = common.root_domain(absolute)
        if not rd or rd == agency_domain or rd in BOILERPLATE_DOMAINS or rd in credits:
            continue
        if not urlparse(absolute).scheme.startswith("http"):
            continue

        # Anchor text first; then an image alt inside the anchor; then the domain.
        agency_name = agency.get("agency_name", "")
        label = strip_cta(clean_label(inner))
        from_domain = False
        if not plausible_name(label, agency_name, agency_domain):
            inner_img = _IMG_TAG_RE.search(inner or "")
            if inner_img:
                a = _img_attrs(inner_img.group(0))
                label = clean_label(a.get("alt") or a.get("title") or "")
        if not plausible_name(label, agency_name, agency_domain):
            label = _name_from_domain(rd)
            from_domain = True
        if not plausible_name(label, agency_name, agency_domain):
            continue

        found.append({"client_name": label, "client_domain": rd,
                      "source": f"outbound_link:{page_url}",
                      "confidence": "high", "_method": "outbound_link",
                      # A name derived from the domain is a placeholder. If any
                      # extractor found the brand written out, that wins.
                      "_name_from_domain": from_domain})
    return found


def _name_from_domain(rd):
    """acme-plumbing.com -> 'Acme Plumbing'. A label, not a verified brand name."""
    stem = (rd or "").split(".")[0]
    parts = [p for p in re.split(r"[-_]+", stem) if p]
    if not parts:
        return ""
    return " ".join(p.upper() if len(p) <= 3 and p.isalpha() else p.capitalize()
                    for p in parts)


# --------------------------------------------------------------------------
# Extractor 4: case study titles  (low)
# --------------------------------------------------------------------------

TITLE_PATTERNS = [
    # "How We Grew Acme Plumbing 300%"  /  "How We Scaled Acme to 4 Locations"
    re.compile(r"^how\s+(?:we|they)\s+\w+\s+(.+?)(?:\s+(?:by\s+)?\d+[\d.,]*\s*%?.*)?$", re.I),
    # "Case Study: Acme Plumbing"  /  "Client Spotlight - Acme"
    re.compile(r"^(?:case\s*stud(?:y|ies)|client\s+spotlight|success\s+story|"
               r"client\s+story|spotlight)\s*[:\-–—]\s*(.+)$", re.I),
    # "Scaling Acme Plumbing to 4 Locations"
    re.compile(r"^(?:scaling|growing|launching|rebuilding|driving|building)\s+(.+?)"
               r"(?:\s+(?:to|by|from|into|with|through)\b.*)?$", re.I),
    # "Acme Plumbing: 312% More Leads"  /  "Acme Plumbing — 6x ROAS on Meta"
    re.compile(r"^(.+?)\s*[:\-–—]\s*(?:\d|[a-z].{0,60}(?:roas|leads?|growth|revenue|"
               r"conversions?|cpl|cac|traffic|sales|revenue|bookings?|calls?))", re.I),
]

_TRAILING_METRIC_RE = re.compile(
    r"\s+(?:by\s+)?\d[\d.,]*\s*(?:%|x|percent)?(?:\s+(?:in|on|with|over|across)\b.*)?$", re.I)


def name_from_title(title):
    t = clean_label(title)
    if not t:
        return ""
    for rx in TITLE_PATTERNS:
        m = rx.match(t)
        if m:
            cand = _TRAILING_METRIC_RE.sub("", m.group(1)).strip(" .,:-–—")
            cand = re.sub(r"^(?:the|our|a)\s+", "", cand, flags=re.I).strip()
            if cand:
                return cand
    return ""


def extract_case_study_titles(html, page_url, agency):
    found = []
    seen = set()
    texts = [m.group(2) for m in _HEADING_RE.finditer(html or "")]
    # Case-study card links carry the same title as the heading; both are worth a look.
    for attrs_blob, inner in _A_TAG_RE.findall(html or ""):
        href = (_img_attrs("<a " + attrs_blob + ">").get("href") or "").lower()
        if any(w in href for w in ("case-stud", "casestud", "success-stor", "/work/",
                                   "/portfolio/", "/project")):
            texts.append(inner)
    for raw in texts:
        name = clean_label(name_from_title(raw))
        key = name_key(name)
        if key and key not in seen and plausible_name(
                name, agency.get("agency_name", ""), agency.get("agency_domain", "")):
            seen.add(key)
            found.append({"client_name": name, "source": f"case_study_title:{page_url}",
                          "confidence": "low", "_method": "case_study_title"})
    return found


EXTRACTORS = [
    ("alt_text", extract_alt_text),
    ("image_filename", extract_image_filenames),
    ("outbound_link", extract_outbound_links),
    ("case_study_title", extract_case_study_titles),
]

CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def merge_clients(records, agency):
    """
    Collapse the four extractors' output into one row per client.

    Confidence is the best method that found it. A name found only by filename
    parsing that also turns up as an outbound link is promoted to high and gains
    a domain - that cross-confirmation is the point of running all four.
    """
    by_key = {}
    for r in records:
        key = name_key(r.get("client_name", ""))
        if not key:
            continue
        if key not in by_key:
            by_key[key] = {
                "client_name": r["client_name"],
                "client_domain": r.get("client_domain", ""),
                "agency_name": agency.get("agency_name", ""),
                "agency_domain": common.root_domain(agency.get("agency_domain", "")) or "",
                "vertical": agency.get("vertical", ""),
                "source": r["source"],
                "confidence": r["confidence"],
                "_methods": {r["_method"]},
                "_name_from_domain": bool(r.get("_name_from_domain")),
            }
            continue
        cur = by_key[key]
        cur["_methods"].add(r["_method"])
        if r.get("client_domain") and not cur["client_domain"]:
            cur["client_domain"] = r["client_domain"]
        if CONFIDENCE_RANK[r["confidence"]] < CONFIDENCE_RANK[cur["confidence"]]:
            cur["confidence"] = r["confidence"]
            cur["source"] = r["source"]
        # Prefer the longer written form: "Canyon Plumbing Co." over "Canyon Plumbing".
        if not r.get("_name_from_domain") and (
                cur["_name_from_domain"] or len(r["client_name"]) > len(cur["client_name"])):
            cur["client_name"] = r["client_name"]
            cur["_name_from_domain"] = False

    records = _reconcile_domains(list(by_key.values()))

    # A name confirmed by two independent methods is worth more than either alone.
    for rec in records:
        if len(rec["_methods"]) > 1 and rec["confidence"] == "medium":
            rec["confidence"] = "high"
        rec["source"] = f"{'+'.join(sorted(rec['_methods']))} | {rec['source']}"

    return _attach_domains(_apply_learned(records, agency))


def _apply_learned(records, agency):
    """
    Apply the corrections you made in the Feedback tab.

    Runs after merging so a rename lands on the final row rather than on one
    extractor's guess, and so a correction can rescue a row two extractors
    disagreed about. Junk suppression already happened up in plausible_name.
    """
    agency_domain = common.root_domain(agency.get("agency_domain", "")) or ""
    out = []
    for rec in records:
        corrected = False

        fixed = RULES.correct_name(rec["client_name"])
        if fixed != rec["client_name"]:
            rec["source"] += f" | renamed from '{rec['client_name']}' (your correction)"
            rec["client_name"] = fixed
            corrected = True

        new_domain = RULES.correct_domain(rec["client_name"], rec["client_domain"])
        if new_domain and new_domain != rec["client_domain"]:
            rec["source"] += (f" | domain corrected from "
                              f"'{rec['client_domain'] or 'blank'}' (your correction)")
            rec["client_domain"] = new_domain
            corrected = True

        # A correction is a fact you supplied, so it outranks any heuristic.
        if corrected:
            rec["confidence"] = "high"

        # Re-check junk after renaming: a corrected name can land on a rule.
        if RULES.is_junk(rec["client_name"], rec["client_domain"], agency_domain):
            continue
        # Confidence learned from each method's measured precision. Takes the
        # best rating among the methods that found this row, and replaces the
        # built-in default in both directions: a method that reviews badly gets
        # demoted, not just one that reviews well getting promoted. A row you
        # corrected by hand keeps its high rating regardless.
        if not corrected:
            learned = [RULES.confidence_for(m, None) for m in rec["_methods"]]
            learned = [c for c in learned if c]
            if learned:
                rec["confidence"] = min(learned, key=lambda c: CONFIDENCE_RANK[c])
        out.append(rec)
    return out


def _squash(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _same_brand(name, domain):
    """
    Is this written name the same brand as this domain?

    "Lakeside Dermatology" / lakesidederm.com    -> yes (domain stem is a prefix)
    "TrueNorth Outfitters" / truenorthoutfitters.com -> yes (exact)
    "Summit Roofing"       / mesagaragedoors.com -> no

    Prefix matching in both directions, because agencies shorten brand names for
    domains at least as often as they lengthen them. The 6-character floor keeps
    "Air" from matching "airbnb.com".
    """
    n, d = _squash(name), _squash((domain or "").split(".")[0])
    if not n or not d:
        return False
    if n == d:
        return True
    shorter, longer = (n, d) if len(n) <= len(d) else (d, n)
    if len(shorter) >= 6 and longer.startswith(shorter):
        return True
    shared = set(_tokens(name)) & set(_tokens((domain or "").split(".")[0]))
    return len(shared) >= 2


def _reconcile_domains(records):
    """
    Fold a record whose name came from a domain into the record that has the
    brand written out. Without this, "Lakesidederm / lakesidederm.com" and
    "Lakeside Dermatology / (blank)" ship as two rows for one client - which
    inflates the client count and splits the TikTok-free tally.
    """
    with_domain = [r for r in records if r.get("client_domain")]
    without = [r for r in records if not r.get("client_domain")]
    absorbed = set()

    for named in without:
        for linked in with_domain:
            if id(linked) in absorbed:
                continue
            if not _same_brand(named["client_name"], linked["client_domain"]):
                continue
            # The written name wins over one derived from the domain, and the
            # fuller written form wins over a truncation: an anchor reading
            # "Shop TrueNorth" should not beat a title saying "TrueNorth Outfitters".
            fuller = (_squash(named["client_name"]).startswith(_squash(linked["client_name"]))
                      and len(named["client_name"]) > len(linked["client_name"]))
            if linked.get("_name_from_domain") or fuller:
                linked["client_name"] = named["client_name"]
                linked["_name_from_domain"] = False
            linked["_methods"] |= named["_methods"]
            if CONFIDENCE_RANK[named["confidence"]] < CONFIDENCE_RANK[linked["confidence"]]:
                linked["confidence"] = named["confidence"]
            absorbed.add(id(named))
            break

    return [r for r in records if id(r) not in absorbed]


def _attach_domains(records):
    """
    Give a name-only client the domain of a link found on the same page, when
    the name and the domain share enough tokens to be the same brand. Evidence,
    not guesswork - the link was on the page.
    """
    linked = [(name_key(r["client_name"]), r["client_domain"])
              for r in records if r.get("client_domain")]
    for rec in records:
        if rec.get("client_domain"):
            continue
        key_tokens = set(name_key(rec["client_name"]).split())
        if not key_tokens:
            continue
        for other_key, domain in linked:
            dom_tokens = set(_tokens(domain.split(".")[0]))
            other_tokens = set(other_key.split())
            if key_tokens & dom_tokens and len(key_tokens & dom_tokens) >= min(
                    2, len(key_tokens)):
                rec["client_domain"] = domain
                rec["source"] += f" | domain matched to {domain}"
                break
            if key_tokens == other_tokens:
                rec["client_domain"] = domain
                rec["source"] += f" | domain matched to {domain}"
                break
    return records


def verify_guessed_domain(fetcher, name):
    """
    Guess <slug>.com and confirm it by fetching. Only returns a domain when the
    fetched page actually names the brand. Anything less stays blank: a wrong
    domain gets pixel-checked and produces a confidently wrong answer.
    """
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    if len(slug) < 4:
        return "", "guess too short"
    for candidate in (f"{slug}.com", f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}.com"):
        origin = common.base_url(candidate)
        if not origin:
            continue
        page = fetcher.get(origin)
        if not page.ok:
            continue
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page.text)
        text = " ".join(re.sub(r"<[^>]+>", " ", text).split()).lower()
        wanted = [t for t in _tokens(name) if t not in ("the", "and")]
        if wanted and all(t in text for t in wanted):
            return common.root_domain(candidate), f"verified: brand name found on {candidate}"
    return "", "guess not verified"


# --------------------------------------------------------------------------
# Page selection and the per-agency run
# --------------------------------------------------------------------------

def candidate_pages(fetcher, domain):
    origin = common.base_url(domain)
    if not origin:
        return [], "bad_domain"
    picked, notes = [], []

    sm_urls, sm_note = common.sitemap_urls(fetcher, domain, max_urls=2000)
    notes.append(sm_note)
    if sm_urls:
        hits = [u for u in sm_urls
                if any(w in urlparse(u).path.lower() for w in CLIENT_URL_WORDS)]
        notes.append(f"sitemap:{len(hits)} client-shaped urls")
        picked.extend(hits)

    home = fetcher.get(origin)
    if home.ok:
        nav = common.nav_links(home.text, home.url, domain)
        hits = [u for u in nav
                if any(w in urlparse(u).path.lower() for w in CLIENT_URL_WORDS)]
        notes.append(f"nav:{len(hits)} client links of {len(nav)}")
        picked.extend(hits)
    else:
        notes.append(f"homepage {home.failure}")

    picked.extend(common.canonical_url(origin + p) for p in CANONICAL_PATHS)

    # Index pages before individual case studies: an index carries many brands,
    # a single case study carries one.
    def depth(u):
        return len([s for s in urlparse(u).path.split("/") if s])
    final = common.dedupe_urls(sorted(dict.fromkeys(picked), key=depth))
    return final[:MAX_PAGES], "; ".join(notes)


def looks_javascript_rendered(html):
    """
    Would a browser have shown more than we got? Two signals: an explicit JS
    framework/carousel marker, and a page that is nearly all markup and no text.
    """
    low = (html or "").lower()
    hits = [m for m in JS_MARKERS if m in low]
    text_len = len(" ".join(re.sub(r"<[^>]+>", " ", _strip_scripts(html)).split()))
    thin = len(html or "") > 2000 and text_len < 400
    return (bool(hits) or thin), hits[:4] + (["thin_text"] if thin else [])


def _strip_scripts(html):
    return re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html or "")


def parse_agency(fetcher, agency, verify_guess=False, show=False):
    """
    One agency in; (clients, agency_row) out. Never returns a zero it can't defend.
    """
    domain = common.root_domain(agency.get("agency_domain") or agency.get("domain") or "")
    arow = dict(agency)
    arow["agency_domain"] = domain or agency.get("agency_domain", "")
    if not domain:
        arow.update(status="bad_input", clients_found="0",
                    notes="not a parseable domain")
        return [], arow

    urls, page_note = candidate_pages(fetcher, domain)
    per_page, failures, js_pages, fetched = {}, [], [], []

    for url in urls:
        page = fetcher.get(url)
        if not page.ok:
            failures.append(f"{urlparse(url).path or '/'}={page.failure}")
            continue
        fetched.append(url)
        records = []
        for _, fn in EXTRACTORS:
            try:
                records.extend(fn(page.text, page.url, arow))
            except Exception as e:
                failures.append(f"{urlparse(url).path}=extractor_error:{type(e).__name__}")
        merged = merge_clients(records, arow)
        per_page[url] = merged
        if not merged:
            is_js, markers = looks_javascript_rendered(page.text)
            if is_js:
                js_pages.append(f"{urlparse(url).path or '/'}[{','.join(markers)}]")
        if show:
            print(f"    {url} -> {len(merged)} client(s)", file=sys.stderr)
            for c in merged:
                print(f"        {c['confidence']:6} {c['client_name']:34} "
                      f"{c['client_domain'] or '-':28} {c['source'][:70]}", file=sys.stderr)

    # One merge across every page, so a client on both /clients and /work is one row.
    all_records = []
    for url, recs in per_page.items():
        for r in recs:
            for method in sorted(r.get("_methods", {"merged"})):
                all_records.append({
                    "client_name": r["client_name"], "client_domain": r["client_domain"],
                    "source": r["source"], "confidence": r["confidence"],
                    "_method": method,
                    "_name_from_domain": r.get("_name_from_domain", False),
                })
    clients = merge_clients(all_records, arow)

    if verify_guess:
        for c in clients:
            if not c["client_domain"]:
                dom, why = verify_guessed_domain(fetcher, c["client_name"])
                if dom:
                    c["client_domain"] = dom
                    c["source"] += f" | {why}"
                else:
                    # An unverified guess must not raise confidence.
                    c["confidence"] = "low"
                    c["source"] += f" | {why}"

    best_page = max(per_page, key=lambda u: len(per_page[u])) if per_page else ""
    arow["client_page_url"] = best_page if per_page.get(best_page) else ""
    arow["clients_found"] = str(len(clients))
    arow["last_checked"] = common.now_stamp()

    notes = [f"pages_fetched={len(fetched)}/{len(urls)}", f"pages={page_note}"]
    by_conf = {c: sum(1 for x in clients if x["confidence"] == c)
               for c in ("high", "medium", "low")}
    notes.append("confidence=" + ",".join(f"{k}:{v}" for k, v in by_conf.items()))

    if not fetched:
        arow["status"] = "unreachable"
        arow["clients_found"] = ""
        notes.insert(0, "NO PAGES FETCHED - clients_found left blank, not 0")
    elif not clients and js_pages:
        arow["status"] = "needs_manual_review"
        arow["clients_found"] = ""
        notes.insert(0, "JS-RENDERED, nothing extractable: " + "; ".join(js_pages[:4]))
    elif not clients:
        arow["status"] = "no_clients_found"
        notes.insert(0, "pages read cleanly and contained no client list")
    else:
        arow["status"] = "clients_parsed"
    if failures:
        notes.append("failed=" + ",".join(failures[:6]))

    prior = (agency.get("notes") or "").strip()
    arow["notes"] = " | ".join(([prior] if prior else []) + notes)
    return clients, arow


CLIENT_FIELDS = ["client_name", "client_domain", "agency_name", "agency_domain",
                 "vertical", "source", "confidence", "last_checked"]
AGENCY_FIELDS = ["agency_name", "agency_domain", "vertical", "hq_location",
                 "employee_count", "mentions_tiktok", "tiktok_evidence",
                 "client_page_url", "clients_found", "status", "notes", "last_checked"]


# --------------------------------------------------------------------------
# Self-test against fixtures - run this before scaling
# --------------------------------------------------------------------------

def selftest():
    """
    Run every extractor against the fixtures in tests/fixtures and print what came
    back, so the parse can be eyeballed before it touches hundreds of real sites.
    """
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
    if not os.path.isdir(here):
        sys.exit(f"No fixtures at {here}")
    cases = [
        ("alpine_clients.html", "https://alpinedigital.com/clients",
         {"agency_name": "Alpine Digital", "agency_domain": "alpinedigital.com",
          "vertical": "home_services"}),
        ("northbeam_work.html", "https://northbeammedia.com/our-work",
         {"agency_name": "Northbeam Media", "agency_domain": "northbeammedia.com",
          "vertical": "local_health"}),
        ("js_portfolio.html", "https://crestlinegrowth.com/portfolio",
         {"agency_name": "Crestline Growth", "agency_domain": "crestlinegrowth.com",
          "vertical": "dtc"}),
    ]
    failures = 0
    for filename, url, agency in cases:
        path = os.path.join(here, filename)
        if not os.path.exists(path):
            print(f"MISSING FIXTURE {path}")
            failures += 1
            continue
        with open(path, encoding="utf-8") as f:
            html = f.read()

        print(f"\n{'=' * 78}\n{filename}  ({agency['agency_domain']})\n{'=' * 78}")
        raw = []
        for label, fn in EXTRACTORS:
            got = fn(html, url, agency)
            raw.extend(got)
            print(f"\n  -- {label} ({len(got)}) --")
            for r in got:
                print(f"     {r['confidence']:6} {r['client_name'][:38]:38} "
                      f"{r.get('client_domain', '') or '-':26}")

        merged = merge_clients(raw, agency)
        print(f"\n  == merged: {len(merged)} client(s) ==")
        for c in sorted(merged, key=lambda x: (CONFIDENCE_RANK[x['confidence']],
                                               x['client_name'])):
            print(f"     {c['confidence']:6} {c['client_name'][:36]:36} "
                  f"{c['client_domain'] or '-':26} {'+'.join(sorted(c['_methods']))}")

        if not merged:
            is_js, markers = looks_javascript_rendered(html)
            verdict = "needs_manual_review" if is_js else "no_clients_found"
            print(f"\n  == zero clients -> status={verdict}  markers={markers}")
            if filename == "js_portfolio.html" and not is_js:
                print("  FAIL: JS-rendered fixture was not detected as such")
                failures += 1
    print(f"\n{'=' * 78}")
    print("Selftest finished." if not failures else f"Selftest finished with {failures} failure(s).")
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(
        description="Extract each agency's client list into the Clients tab.")
    ap.add_argument("input", nargs="?",
                    help="an agency domain, a comma-separated list, or a CSV "
                         "with an agency_domain column")
    ap.add_argument("-c", "--column", default="agency_domain")
    ap.add_argument("-o", "--output", default="clients.csv")
    ap.add_argument("--agencies-output", default="agencies_after_parse.csv",
                    help="where the updated Agencies rows are written")
    ap.add_argument("--min-confidence", choices=["high", "medium", "low"], default="low",
                    help="drop clients below this confidence (default low = keep all)")
    ap.add_argument("--verify-guess", action="store_true",
                    help="for name-only clients, try <slug>.com and keep it only if "
                         "the fetched page names the brand")
    ap.add_argument("--show", action="store_true", help="print every parse as it happens")
    ap.add_argument("--selftest", action="store_true",
                    help="run the extractors against tests/fixtures and exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--update-agencies", action="store_true",
                    help="also write the updated Agencies rows to the sheet")
    common.add_crawl_args(ap)
    common.add_sheet_args(ap)
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if not args.input:
        ap.error("input is required unless --selftest is given")

    seeds = []
    if os.path.exists(args.input) and args.input.lower().endswith(".csv"):
        with open(args.input, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            col = args.column if args.column in fields else None
            if not col:
                for alt in ("agency_domain", "domain", "website", "url"):
                    if alt in fields:
                        col = alt
                        print(f"Column '{args.column}' not found; using '{col}'.",
                              file=sys.stderr)
                        break
            if not col:
                sys.exit(f"No domain column in {args.input}. Available: {fields}")
            for r in reader:
                if (r.get(col) or "").strip():
                    seeds.append(dict(r, agency_domain=r[col].strip()))
    else:
        seeds = [{"agency_domain": d}
                 for d in common.read_domain_list(args.input, args.column)]

    seen, agencies = set(), []
    for s in seeds:
        rd = common.root_domain(s["agency_domain"]) or s["agency_domain"]
        if rd not in seen:
            seen.add(rd)
            agencies.append(s)
    if args.limit:
        agencies = agencies[:args.limit]
    if not agencies:
        sys.exit(f"No agency domains found in {args.input!r}.")

    print(f"Parsing clients for {len(agencies)} agencies "
          f"({args.delay}s/request per host)...", file=sys.stderr)
    if RULES.empty:
        print("  no learned rules yet - review some rows and run "
              "`python feedback.py --learn`", file=sys.stderr)
    else:
        print(f"  applying your corrections: {RULES.summary()}", file=sys.stderr)

    fetcher = common.fetcher_from_args(args)
    all_clients, agency_rows = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(parse_agency, fetcher, a, args.verify_guess, args.show): a
                   for a in agencies}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                clients, arow = fut.result()
            except Exception as e:
                clients = []
                arow = dict(src, status=f"crashed:{type(e).__name__}",
                            clients_found="", notes=f"crashed: {e}")
            all_clients.extend(clients)
            agency_rows.append(arow)
            print(f"  [{i}/{len(agencies)}] {arow.get('agency_domain', ''):34} "
                  f"{arow.get('clients_found', '') or '-':>4} clients  "
                  f"({arow.get('status', '')})", file=sys.stderr)

    floor = CONFIDENCE_RANK[args.min_confidence]
    kept = [c for c in all_clients if CONFIDENCE_RANK[c["confidence"]] <= floor]
    dropped = len(all_clients) - len(kept)

    order = {common.root_domain(a["agency_domain"]) or a["agency_domain"]: i
             for i, a in enumerate(agencies)}
    agency_rows.sort(key=lambda r: order.get(r.get("agency_domain", ""), 1 << 30))

    common.report_stats(fetcher)
    by_conf = {c: sum(1 for x in kept if x["confidence"] == c)
               for c in ("high", "medium", "low")}
    review = sum(1 for a in agency_rows if a.get("status") == "needs_manual_review")
    unreachable = sum(1 for a in agency_rows if a.get("status") == "unreachable")
    print(f"\n  {len(kept)} clients across {len(agency_rows)} agencies", file=sys.stderr)
    print(f"    high: {by_conf['high']}  medium: {by_conf['medium']}  "
          f"low: {by_conf['low']}", file=sys.stderr)
    if dropped:
        print(f"    {dropped} dropped below --min-confidence {args.min_confidence}",
              file=sys.stderr)
    print(f"  {review} agencies need manual review (JS-rendered, not zero clients)",
          file=sys.stderr)
    print(f"  {unreachable} agencies unreachable", file=sys.stderr)
    with_domain = sum(1 for c in kept if c["client_domain"])
    print(f"  {with_domain}/{len(kept)} clients have a domain and can be pixel-checked",
          file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: nothing written.", file=sys.stderr)
        return

    common.write_csv(args.output, CLIENT_FIELDS, kept)
    common.write_csv(args.agencies_output, AGENCY_FIELDS, agency_rows)
    if args.sheet:
        common.push_sheet("Clients", kept, args.sheet_id, args.output)
        if args.update_agencies:
            common.push_sheet("Agencies", agency_rows, args.sheet_id, args.agencies_output)


if __name__ == "__main__":
    main()
