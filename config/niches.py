"""Pre-configured high-ticket niches with query templates.

Each niche defines the search queries used to discover businesses
and the keywords that mark a site as "in-niche" so we only keep
leads that match the target vertical.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Niche:
    id: str
    label: str
    search_queries: list = field(default_factory=list)
    scope_keywords: list = field(default_factory=list)
    exclusion_keywords: list = field(default_factory=list)


NICHES: dict[str, Niche] = {
    "real_estate": Niche(
        id="real_estate",
        label="Real Estate & Property Development",
        search_queries=[
            '"real estate" brokerage "contact us" -zillow -trulia -realtor.com',
            '"estate agency" "our team" "email"',
            '"realty" "search homes" "contact" -"for rent" -zillow',
            '"buying" "selling" "real estate" "get in touch"',
        ],
        scope_keywords=[
            "real estate", "property", "realtor", "realty",
            "estate agent", "brokerage", "condos", "villas",
        ],
        exclusion_keywords=[
            "for sale by owner", "rental listings aggregator",
            "estate sale", "estate liquidation", "property tax",
            "municipal corporation", "ward office",
        ],
    ),
    "finance": Niche(
        id="finance",
        label="Wealth Management & Finance",
        search_queries=[
            '"wealth management" "our team" "contact" -investopedia -morgan',
            '"financial advisor" "office" "contact us" -investopedia',
            '"investment advisory" "about us" "contact"',
            '"financial planning" firms "get in touch"',
        ],
        scope_keywords=[
            "wealth", "financial", "investment", "asset management",
            "private equity", "hedge fund", "estate planning",
            "financial planning", "advisory",
        ],
        exclusion_keywords=["student loans", "payday"],
    ),
    "healthcare": Niche(
        id="healthcare",
        label="Private Healthcare & Clinics",
        search_queries=[
            '"cosmetic clinic" "book" "contact" -nhs -doctors.org',
            '"private clinic" "our doctors" "contact us"',
            '"dental practice" "new patients" "contact"',
            '"aesthetic clinic" "before and after" "contact"',
        ],
        scope_keywords=[
            "clinic", "medical", "dentistry", "dental", "dermatology",
            "surgery center", "aesthetic", "med spa", "healthcare",
        ],
        exclusion_keywords=["government", "public hospital"],
    ),
    "legal": Niche(
        id="legal",
        label="Law Firms & Legal Services",
        search_queries=[
            '"law firm" "attorneys" "contact" -legalaid',
            '"business law" firm "get in touch"',
            '"corporate lawyers" "our team" "contact"',
            '"law firm" "practice areas" "contact us"',
        ],
        scope_keywords=[
            "law firm", "attorney", "lawyers", "legal", "solicitors",
            "barristers", "litigation", "counsel",
        ],
        exclusion_keywords=["legal aid", "pro bono"],
    ),
    "saas": Niche(
        id="saas",
        label="SaaS & Software Companies",
        search_queries=[
            '"software" company "request a demo"',
            '"saas" "schedule a demo" "contact"',
            '"b2b" "software product" "book a demo"',
            '"platform" "talk to sales" "pricing"',
        ],
        scope_keywords=[
            "software", "saas", "platform", "cloud", "solutions",
            "tech", "app", "api",
        ],
        exclusion_keywords=["freelance", "portfolio"],
    ),
    "ecommerce": Niche(
        id="ecommerce",
        label="E-commerce & D2C Brands",
        search_queries=[
            '"d2c" brand "contact us" -flipkart -amazon',
            '"online store" "about us" "contact" -shopify -ebay',
            '"direct to consumer" brand "our story"',
            '"ecommerce" brand "customer support"',
        ],
        scope_keywords=[
            "shop", "store", "brand", "d2c", "retail",
            "merchandise", "boutique",
        ],
        exclusion_keywords=["marketplace", "walmart", "amazon seller"],
    ),
    "coaching": Niche(
        id="coaching",
        label="High-Ticket Coaching & Consulting",
        search_queries=[
            '"business coach" "work with me" -fitness -diet',
            '"executive coach" programs "contact"',
            '"consulting" "our clients" "contact us"',
            '"consultancy" "services" "contact" firm',
        ],
        scope_keywords=[
            "coach", "consulting", "consultancy", "mentor",
            "training", "advisory",
        ],
        exclusion_keywords=["diet coach", "fitness coach"],
    ),
    "automotive": Niche(
        id="automotive",
        label="Luxury Automotive & Dealerships",
        search_queries=[
            '"luxury" dealership "inventory" "contact"',
            '"exotic cars" dealership "showroom"',
            'dealership "our team" "book a test drive"',
            '"luxury" cars "our showroom" "visit us"',
        ],
        scope_keywords=[
            "dealership", "luxury cars", "exotic cars", "auto",
            "motors", "vehicles", "maserati", "porsche", "range rover",
        ],
        exclusion_keywords=["used car auction", "rental cars"],
    ),
    "hospitality": Niche(
        id="hospitality",
        label="Luxury Hospitality & Hotels",
        search_queries=[
            '"boutique hotel" "reservations" "contact" -booking.com -airbnb',
            '"luxury resort" "our rooms" "contact"',
            '"fine dining" "reservations" "contact" -eventbrite',
            'hotel group "our hotels" "contact us"',
        ],
        scope_keywords=[
            "hotel", "resort", "hospitality", "boutique hotel",
            "luxury stays", "restaurant group", "fine dining",
        ],
        exclusion_keywords=["hostel", "bed and breakfast"],
    ),
}


def get_niche(niche_id: str) -> Niche | None:
    return NICHES.get(niche_id)


def all_niche_ids() -> list[str]:
    return list(NICHES.keys())


def load_niches(ids: list[str] | None = None) -> list[Niche]:
    """Return a validated list of niches to scrape."""
    if not ids:
        return list(NICHES.values())
    result = []
    for nid in ids:
        niche = get_niche(nid)
        if niche:
            result.append(niche)
    return result
