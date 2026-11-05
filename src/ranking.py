from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from feeds import Article, canonical_url
from utils import contains_keyword, keyword_hits, normalize_text, now_utc

# Lightweight cross-lingual event fingerprinting.
# Maps Arabic/English surface forms to canonical concepts so the same story
# from different outlets/languages can be deduplicated before summarization.
CONCEPT_MAP = {
    "homs": ["homs", "حمص"], "aleppo": ["aleppo", "حلب"], "idlib": ["idlib", "إدلب", "ادلب"],
    "damascus": ["damascus", "دمشق"], "lebanon": ["lebanon", "لبنان"], "beirut": ["beirut", "بيروت"],
    "hezbollah": ["hezbollah", "حزب الله"], "israel": ["israel", "israeli", "إسرائيل", "اسرائيل"],
    "gaza": ["gaza", "غزة"], "iran": ["iran", "iranian", "إيران", "ايران"], "iraq": ["iraq", "العراق"],
    "turkey": ["turkey", "turkish", "تركيا"], "china": ["china", "chinese", "صيني", "الصين"],
    "refinery": ["refinery", "مصفاة", "مصافي"], "oil": ["oil", "نفط", "نفطي"],
    "plot": ["plot", "مخطط", "خلية"], "foiled": ["foiled", "احباط", "إحباط", "أحبطت", "احبطت"],
    "arrest": ["arrest", "arrested", "اعتقال", "القبض", "توقيف"], "cell": ["cell", "خلية"],
    "deny": ["denies", "deny", "denial", "ينفي", "نفي", "ينفي علاقته"],
    "investment": ["investment", "استثمار", "استثمارات"], "reconstruction": ["reconstruction", "إعادة إعمار", "اعمار"],
    "sharaa": ["al-sharaa", "sharaa", "al sharaa", "الشرع"], "president": ["president", "رئيس"],
    "killed": ["killed", "kills", "قتلى", "قتيل", "مقتل", "قتلت"], "blast": ["blast", "explosion", "انفجار", "تفجير"],
    "landmine": ["landmine", "mine", "لغم", "الغام"], "girl": ["girl", "فتاة", "طفلة"],
    "economic": ["economic", "economy", "اقتصاد", "اقتصادي"], "companies": ["companies", "شركات"],
    "meeting": ["meeting", "meet", "بحث", "لقاء", "اجتماع"],
    "saudi": ["saudi", "السعودية", "السعودي", "السعوديه"],
    "riyadh": ["riyadh", "الرياض"],
    "mbs": ["bin salman", "mohammed bin", "mohamed bin", "بن سلمان"],
    "agreement": ["agreement", "agreements", "deal", "pact", "اتفاقية", "اتفاقيات", "مذكرات"],
    "loan": ["loan", "loans", "fund for development", "قروض", "قرض", "الصندوق السعودي"],
    "defence": ["defense", "defence", "military", "دفاع", "عسكري", "عسكرية"],
    "visit": ["visit", "visits", "زيارة", "يزور"],
    "citizenship": ["citizenship", "nationality", "الجنسية", "جنسية"],
    "emirates_airline": ["emirates airline", "طيران الإمارات"],
    "houthi": ["houthi", "houthis", "الحوثي", "الحوثيون", "الحوثيين"],
    "hezbollah_denial": ["hezbollah denies", "حزب الله ينفي", "ينفي"],
    "detention": ["detention", "detainees", "prisoners", "معتقل", "معتقلين", "سجون", "احتجاز"],
    "sdf": ["sdf", "قسد", "syrian democratic forces", "قوات سوريا الديمقراطية"],
    "kurdish": ["kurdish", "كردية", "كردي", "الأكراد"],
    "heritage": ["church", "heritage", "كنيسة", "آثار", "تراث"],
}
GENERIC_CONCEPTS = {"syria", "region", "economic", "president", "meeting", "killed"}
# An event is usually identified by WHO + WHERE (+ WHAT).
ACTORS = {"sharaa", "mbs", "hezbollah", "houthi", "emirates_airline"}
PLACES = {"riyadh", "saudi", "homs", "aleppo", "idlib", "damascus", "beirut", "gaza", "iraq", "turkey", "iran"}
ACTIONS = {"agreement", "loan", "defence", "visit", "plot", "blast", "arrest", "investment", "citizenship", "refinery"}


def concept_set(text: str) -> set[str]:
    norm = normalize_text(text)
    found = set()
    for concept, surfaces in CONCEPT_MAP.items():
        for s in surfaces:
            if normalize_text(s) in norm:
                found.add(concept)
                break
    return found


def score_article(a: Article, kw: dict) -> Article:
    title_norm = normalize_text(a.title)
    body_norm = normalize_text(a.snippet)
    combined = title_norm + " " + body_norm

    syria_kw = kw["syria"]["en"] + kw["syria"]["ar"]
    region_kw = kw["region"]["en"] + kw["region"]["ar"]
    econ_kw = kw["economy"]["en"] + kw["economy"]["ar"]

    syria_title = keyword_hits(title_norm, syria_kw)
    syria_body = keyword_hits(body_norm, syria_kw)
    region_title = keyword_hits(title_norm, region_kw)
    region_body = keyword_hits(body_norm, region_kw)
    econ_title = keyword_hits(title_norm, econ_kw)
    econ_body = keyword_hits(body_norm, econ_kw)

    is_syria = (syria_title + syria_body) > 0
    is_region = (region_title + region_body) > 0
    is_econ = (econ_title + econ_body) >= 2 or (
        a.source_category == "economy" and (econ_title + econ_body) >= 1 and (is_syria or is_region)
    )

    # Relevance gate: must concern Syria/region, or economy with regional/oil angle.
    global_econ_angle = contains_keyword(combined, ["oil", "opec", "sanctions", "نفط", "أوبك", "عقوبات", "الشرق الأوسط", "middle east"])
    if not (is_syria or is_region or (is_econ and global_econ_angle)):
        a.score = -1
        return a

    score = 0.0
    score += 6 * syria_title + 2.5 * syria_body
    score += 2.0 * region_title + 0.8 * region_body
    score += 1.5 * econ_title + 0.5 * econ_body
    score += a.source_weight * 0.6

    if a.published:
        age_hours = max(0.0, (now_utc() - a.published).total_seconds() / 3600)
        score += max(0.0, 4.0 - age_hours / 8.0)
    else:
        score -= 1.0

    a.tags = []
    if is_syria:
        a.tags.append("سوريا")
    if is_econ:
        a.tags.append("اقتصاد")
        a.category = "economy"
    else:
        a.category = "politics"

    # Security incidents around economic infrastructure are primarily politics/security,
    # unless the article is clearly about investment, trade, markets, or reconstruction.
    security_kw = ["plot", "foiled", "arrest", "cell", "blast", "explosion", "killed", "landmine",
                   "مخطط", "إحباط", "احباط", "خلية", "انفجار", "قتلى", "لغم", "اعتقال"]
    pure_econ_kw = ["investment", "reconstruction", "trade", "central bank", "markets", "stocks",
                    "استثمار", "إعادة إعمار", "تجارة", "البنك المركزي", "أسواق", "أسهم"]
    if a.category == "economy" and contains_keyword(combined, security_kw) and not contains_keyword(combined, pure_econ_kw):
        a.category = "politics"

    if is_region and not is_syria:
        a.tags.append("المنطقة")
    a.score = score
    return a


def dedupe(articles: list[Article]) -> list[Article]:
    seen_urls: dict[str, Article] = {}
    kept: list[Article] = []
    kept_concepts: list[set[str]] = []
    for a in sorted(articles, key=lambda x: (x.score, x.source_weight), reverse=True):
        if a.score < 0:
            continue
        curl = canonical_url(a.link)
        if curl in seen_urls:
            continue
        norm_title = normalize_text(re.sub(r"\s*[-–|]\s*[^-–|]{2,40}$", "", a.title))
        concepts = concept_set(a.title + " " + a.snippet[:300])
        specific = concepts - GENERIC_CONCEPTS
        dup = False
        for b, b_concepts in zip(kept, kept_concepts):
            b_norm = normalize_text(re.sub(r"\s*[-–|]\s*[^-–|]{2,40}$", "", b.title))
            if SequenceMatcher(None, norm_title, b_norm).ratio() > 0.86:
                dup = True
                break
            b_specific = b_concepts - GENERIC_CONCEPTS
            shared = specific & b_specific
            # Same event when WHO + WHERE match, or when many specifics overlap.
            same_actor = bool(shared & ACTORS)
            same_place = bool(shared & PLACES)
            same_action = bool(shared & ACTIONS)
            if (same_actor and same_place) or (same_actor and same_action) or len(shared) >= 3:
                dup = True
                break
        if dup:
            continue
        seen_urls[curl] = a
        kept.append(a)
        kept_concepts.append(concepts)
    return kept


def select_articles(articles: list[Article], settings: dict) -> list[Article]:
    max_items = int(settings.get("max_items", 10))
    max_per_source = int(settings.get("max_per_source", 2))
    max_per_publisher = int(settings.get("max_per_publisher", 2))
    min_econ = int(settings.get("min_economy_items", 3))

    ranked = sorted(articles, key=lambda x: x.score, reverse=True)
    selected: list[Article] = []
    per_source: dict[str, int] = defaultdict(int)
    per_publisher: dict[str, int] = defaultdict(int)

    def pub_key(a: Article) -> str:
        return a.publisher or a.source

    def try_add(a: Article) -> bool:
        if per_source[a.source] >= max_per_source:
            return False
        if per_publisher[pub_key(a)] >= max_per_publisher:
            return False
        selected.append(a)
        per_source[a.source] += 1
        per_publisher[pub_key(a)] += 1
        return True

    def remove(a: Article) -> None:
        selected.remove(a)
        per_source[a.source] -= 1
        per_publisher[pub_key(a)] -= 1

    for a in ranked:
        if len(selected) >= max_items:
            break
        try_add(a)

    # Ensure major international outlets are represented when available.
    min_intl = int(settings.get("min_international_items", 0))
    intl_count = sum(1 for a in selected if a.source_group == "international")
    if intl_count < min_intl:
        intl_pool = [a for a in ranked if a.source_group == "international" and a not in selected]
        # Prefer one item per publisher before adding a second from the same one.
        intl_pool.sort(key=lambda a: (per_publisher[pub_key(a)], -a.score))
        for a in intl_pool:
            if intl_count >= min_intl:
                break
            if len(selected) < max_items:
                if try_add(a):
                    intl_count += 1
            else:
                victims = sorted([x for x in selected if x.source_group != "international"],
                                 key=lambda x: x.score)
                if not victims:
                    break
                victim = victims[0]
                remove(victim)
                if try_add(a):
                    intl_count += 1
                else:
                    # Restore if publisher/source cap prevented the swap.
                    try_add(victim)

    # Ensure economy minimum if possible.
    econ_count = sum(1 for a in selected if a.category == "economy")
    if econ_count < min_econ:
        econ_pool = [a for a in ranked if a.category == "economy" and a not in selected]
        for a in econ_pool:
            if econ_count >= min_econ or len(selected) >= max_items:
                break
            if try_add(a):
                econ_count += 1
        # If full, swap lowest-scoring politics for missing economy.
        if econ_count < min_econ and len(selected) >= max_items:
            intl_now = sum(1 for x in selected if x.source_group == "international")
            politics = sorted([a for a in selected if a.category != "economy"
                               and not (a.source_group == "international" and intl_now <= min_intl)],
                              key=lambda x: x.score)
            for a in econ_pool:
                if econ_count >= min_econ or not politics:
                    break
                if per_source[a.source] >= max_per_source:
                    continue
                victim = politics.pop(0)
                remove(victim)
                if try_add(a):
                    econ_count += 1
                else:
                    try_add(victim)

    # Ensure a minimum of non-Syria region items if available.
    min_region = int(settings.get("min_region_items", 2))
    region_count = sum(1 for a in selected if "سوريا" not in a.tags)
    if region_count < min_region:
        region_pool = [a for a in ranked if "سوريا" not in a.tags and a not in selected]
        for a in region_pool:
            if region_count >= min_region:
                break
            if len(selected) < max_items:
                if try_add(a):
                    region_count += 1
            else:
                intl_now = sum(1 for x in selected if x.source_group == "international")
                econ_now = sum(1 for x in selected if x.category == "economy")
                syria_items = sorted([a for a in selected if "سوريا" in a.tags
                                      and not (a.source_group == "international" and intl_now <= min_intl)
                                      and not (a.category == "economy" and econ_now <= min_econ)],
                                     key=lambda x: x.score)
                if not syria_items:
                    break
                if per_source[a.source] >= max_per_source:
                    continue
                victim = syria_items[0]
                remove(victim)
                if try_add(a):
                    region_count += 1
                else:
                    try_add(victim)

    selected.sort(key=lambda x: (0 if "سوريا" in x.tags else 1, x.category != "economy", -x.score))
    for i, a in enumerate(selected, 1):
        a.id = f"A{i:02d}"
    return selected
