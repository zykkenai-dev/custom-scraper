"""In-niche filtering based on page tokens and URL characteristics."""

from urllib.parse import urlparse

from config.niches import Niche

# Domains that are never a business lead: reference sites, social platforms,
# property/marketplace portals, aggregators, mega-corporations, banks.
DENY_HOSTS = {
    # Reference / dictionaries
    "wikipedia.org", "wikimedia.org", "wiktionary.org",
    "dictionary.com", "merriam-webster.com", "cambridge.org",
    "oxfordlearnersdictionaries.com", "collinsdictionary.com",
    "thefreedictionary.com", "vocabulary.com", "urbandictionary.com",
    "investopedia.com", "britannica.com",
    "businessnewsdaily.com", "forbes.com", "entrepreneur.com",
    # Tech tutorial / content farms (never an outreach lead)
    "geeksforgeeks.org", "tutorialspoint.com", "javatpoint.com",
    "w3schools.com", "stackoverflow.com", "stackexchange.com",
    "medium.com", "searchenginejournal.com", "neilpatel.com",
    "businessinsider.com", "techcrunch.com", "theverge.com",
    "howtogeek.com", "sciencedirect.com", "springer.com", "mdpi.com",
    # Social / content platforms
    "youtube.com", "youtu.be", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "quora.com", "pinterest.com",
    "linkedin.com", "github.com", "medium.com", "wordpress.com",
    # Property / marketplace portals
    "zillow.com", "realtor.com", "trulia.com", "redfin.com", "homes.com",
    "housing.com", "magicbricks.com", "99acres.com", "commonfloor.com",
    "apartments.com", "streeteasy.com", "rightmove.co.uk", "zoopla.co.uk",
    "realestateindia.com", "nobroker.in", "proptiger.com", "makaan.com",
    "squareyards.com", "commonfloor", "19acres.com", "globesestate.com",
    "dhproperty.com", "bayut.com", "propertyfinder.ae", "dubizzle.com",
    "zameen.com", "lamudi.com", "iproperty.com.my", "propertyguru.com",
    "onlinerealtor.com", "propertyshark.com", "propertyline-india.com",
    "booking.com", "airbnb.com", "expedia.com", "tripadvisor.com",
    "indeed.com", "glassdoor.com", "yelp.com", "yellowpages.com",
    "craigslist.org", "ebay.com", "amazon.com", "walmart.com",
    "homedepot.com", "lowes.com",
    # Banks / financial institutions (not outreach leads)
    "sbi.co.in", "hdfcbank.com", "icicibank.com", "axisbank.com",
    "citibank.com", "chase.com", "bankofamerica.com", "wellsfargo.com",
    "jpmorgan.com", "goldmansachs.com", "boi.co.in", "pnbindia.in",
    "bankofindia.com", "majorbanks.in", "reservebank",
    # Nationals / industry bodies / exchanges
    "ibef.org", "india.gov.in", "sebi.gov.in", "stockexchangeofindia",
    "bseindia.com", "nseindia.com", "nhc.co.in",
    # Logistics / telecom giants
    "dtdc.com", "tata.com", "tatagroup.com", "airtel.in", "jio.com",
    # Big consulting / accounting firms (never boutique clients)
    "bain.com", "mckinsey.com", "bcg.com", "deloitte.com", "deloitte.de",
    "pwc.com", "pwc.de", "ey.com", "kpmg.com", "accenture.com",
    "boozallen.com", "oliverwyman.com", "consultancy.us", "capgemini.com",
    "becg.com", "zolakospartners.com", "aataoracle.com", "marcint.com",
    "potentialreserve.com", "martosamelia.com", "guillermobayard.com",
    "stratadvisorygroup.com", "slingshotconsultinggroup.com",
    "auren.com", "bdo.com", "russellreynolds.com", "heidrick.com",
    "kornferry.com", "spencerstuart.com", "egonzehnder.com",
    "mckinsey.com", "marsh.com", "mercer.com", "willistowerswatson.com",
    "aon.com", "thoughtworks.com", "globant.com", "wipro.com", "infosys.com",
    "tcs.com", "hcltech.com", "techmahindra.com", "cognizant.com",
    # Big tech / platforms (not clients)
    "google.com", "googleusercontent.com", "microsoft.com", "apple.com",
    "meta.com", "facebookusa.com", "amazonaws.com", "cloudflare.com",
    "netflix.com", "salesforce.com", "oracle.com", "ibm.com", "adobe.com",
    "shopify.com", "stripe.com", "squareup.com", "hubspot.com", "webflow.com",
    # Corporate / directory data portals
    "opencorporates.com", "crunchbase.com", "zoominfo.com", "datanyze.com",
    "leadfeeder.com", "kompass.com", "g2.com", "trustpilot.com",
    # Government / municipal tax & civic portals
    "punecorporation.org", "nagarpalika", "municipal",
    # Search engines / web portals (never organic-result leads)
    "baidu.com", "sogou.com", "bing.com", "yahoo.com", "duckduckgo.com",
    "yandex.com", "search.brave.com", "ecosia.org", "startpage.com",
    # Encyclopedia / wiki farms beyond wikipedia
    "fandom.com", "baike.com", "wikiwand.com", "howstuffworks.com",
    # Q&A / forums / user content
    "zhihu.com", "windowsforum.kr", "quoracdn.net", "answers.com",
    # Video / streaming platforms
    "bilibili.com", "dailymotion.com", "vimeo.com", "twitch.tv",
    "odysee.com", "dailymotion.com",
    # Blog / site-builder platforms (lead lives on the customer's own domain)
    "blogspot.com", "tumblr.com", "livejournal.com", "substack.com",
    "weebly.com", "carrd.co", "notion.site", "wixsite.com", "squarespace.com",
    "w3spaces.com", "godaddysites.com", "wordpress.org",
    # Data / public-service portals seen polluting search results
    "pincode.net.in", "pagibigfundservices.com", "dgpa.gov.tw",
    "unienrol.com", "private.com", "studocu.com", "coursehero.com",
    # Adult / explicit hosts (never B2B leads)
    "pornhub.com", "xvideos.com", "xhamster.com", "xnxx.com",
    "youporn.com", "redtube.com", "onlyfans.com", "pornhd.com",
    "pornhubpremium.com", "spankbang.com", "eporner.com",
    # Content / tool / Q&A / news farms that dominate phrase-queries
    "wordreference.com", "softonic.com", "computerhope.com",
    "simplilearn.com", "mapsofindia.com", "indiamapia.com", "icbse.com",
    "pincodedata.com", "kknews.cc", "tvcn.com.cn", "businesstoday.com.tw",
    "hersexhealth.com.hk", "uptodown.com", "readingoutpost.com",
    "office.com", "microsoftonline.com", "live.com", "msn.com",
    "zillow.com", "sciencealert.com", "livescience.com", "brightside.com",
    "wikihow.com", "ehow.com", "thespruce.com", "verywellhealth.com",
    "healthline.com", "webmd.com", "mayoclinic.org", "nhs.uk",
    "medscape.com", "medicinenet.com", "rxlist.com",
    # Shopping / coupon / review aggregators
    "flipkart.com", "jiomart.com", "myntra.com", "shopclues.com",
    "snapdeal.com", "coupang.com", "alibaba.com", "aliexpress.com",
    "ebay.in", "cartify.com", "shopifyblog",
    # Event / directory / listing platforms
    "eventbrite.com", "meetup.com", "classpass.com", "groupon.com",
    "justdial.com", "sulekha.com", "urbanpro.com", "indiaMART.com",
    "indiamart.com", "tradeindia.com", "exportersindia.com",
    # Property / estate-sale portals observed polluting real-estate queries
    "sasteghar.com", "propertywala.com", "estatesale.com", "estatesales.net",
    "homesandland.com", "estatesales.org", "listingvine.com",
    "edgeprop.sg", "99.co", "fazwaz.sg", "hoihup.com",
    # Big brokerage franchises / corporate real-estate (not boutique clients)
    "sothebysrealty.com", "chapinsothebysrealty.com", "remax.com",
    "century21.com", "compass.com", "kw.com", "exprealty.com",
    "cbre.com", "jll.com", "colliers.com", "savills.com",
    "realtor.com", "homes.com", "homefinder.com",
    # Look-alike brands that are NOT real-estate / niche businesses
    "realmadrid.com", "therealreal.com", "realsports.io", "projectreal.gg",
    "real.com", "realapp.com", "realtybiznews.com",
    "realestateagent.com", "realtyexecutives.com",
    # Retail mega-brands leaked by loose phrase queries
    "bestbuy.com", "target.com", "ikea.com", "costco.com",
    "uslawexplained.com",
    # Marketing / lead-gen providers that sell INTO the niches (competitors)
    "emailmovers.com", "return72.com", "leadfeeder.com", "lusha.com",
    "snov.io", "apollo.io", "clearbit.com", "hunter.io", "disify.com",
    "neverbounce.com", "zerobounce.com", "mailtester.com", "de-bounce.com",
    "thesearchlab.co", "seo.com", "mangools.com", "semrush.com",
    "ahrefs.com", "moz.com", "reputation.com", "vendasta.com",
    # Portals / listings observed leaking into real-estate & finance queries
    "estatesales.net", "reply.gs", "lookbeyond.io", "racialjustice.abc7ny.com",
    # Industry associations / national bodies with a "contact" page (not leads)
    "nar.realtor", "realtor.org", "rla.org.au", "rea.org.au", "areaa.com.au",
    # Software / SaaS selling INTO the real-estate niche (never a prospect)
    "real-agent.ai", "kvcore.com", "followupboss.com", "chime.io",
}

SUFFIX_DENY = (
    ".gov", ".gov.in", ".gov.uk", ".gov.au", ".gov.tw", ".gov.sg",
    ".gov.cn", ".go.kr", ".go.jp", ".mil", ".edu", ".edu.cn", ".ac.in",
    ".ac.uk",
)
# Institutional/bank .co.in generic patterns are covered above.

# Strong signals that the page belongs to a large listed corporation that
# does not buy boutique agency services (Tier-1 corporate filter).
CORPORATE_SCALE_SIGNALS = (
    "investor relations",
    "investor-relations",
    "market capitalization",
    "market cap",
    "nasdaq",
    "nyse",
    "bombay stock exchange",
    "national stock exchange",
    "stock exchange",
    "listed company",
    "listed on the",
    "annual report",
    "quarterly results",
    "earnings call",
    "shareholders",
    "fortune 500",
    "ftse 100",
    "s&p 500",
    "multinational corporation",
    "publicly traded",
    "ipo",
    "dividend declared",
    "global headquarters",
    "corporate headquarters",
    "profit after tax",
    "earnings before interest",
    "your bank",
    "tier-1 consulting",
    "tier 1 consulting",
    "global strategy consulting",
    "fortune global 500",
    "fortune 100",
    "8000 employees",
    "10000 employees",
    "50000+",
    "listed on the nyse",
    "gb1 emissions",
    "sti program",
    "board of directors",
    "corporate governance",
    "esg reporting",
    "esg report",
    "internal audit",
    "annual revenue",
    "revenues of over",
    "market value of over",
    "gross profit of over",
    "net income of over",
    "operating income of over",
    "pre-tax profit of over",
)

# Strong signals the page is an educational institution or aggregator portal.
EDUCATION_SIGNALS = (
    "university",
    "college",
    "university school",
    "admission ",
    "faculty of",
    "academic calendars",
    "courses offered",
    "university library",
    "results portal",
    "class of ",
)

# Aggregators / marketplaces masquerading as niche sites.
PORTAL_SIGNALS = (
    "sign in to your account",
    "create your account",
    "seller central",
    "become a partner",
    "compare & save",
    "compare prices",
    "membership plans available",
    "price comparison",
    "placing an order",
    "track your order",
)

# Marketing/lead-gen agencies that SELL to the target niche (competitors,
# not prospects). Keep phrases narrow so real businesses aren't flagged.
PROVIDER_SIGNALS = (
    "marketing agency",
    "digital marketing agency",
    "seo agency",
    "ppc agency",
    "web design agency",
    "lead generation agency",
    "email marketing agency",
    "inbound marketing agency",
    "performance marketing agency",
    # Loose-but-safe provider markers distinct from a niche business
    "email marketing",
    "email marketing services",
    "bulk email service",
    "marketing automation",
    "lead generation services",
    "seo services",
    "ppc management",
    "web development services",
    "content marketing services",
    "social media marketing agency",
)

# Business-name patterns that mark a provider/marketing vendor rather than a
# niche business (e.g. "Emailmovers | Email Marketing | Data | Design").
PROVIDER_NAME_SIGNALS = (
    "email marketing",
    "digital marketing",
    "lead generation",
    " seo ",
    " seo |",
    "| seo",
    "ppc ",
    "web design",
    "web development ",
    "marketing agency",
    "ad agency",
    "social media marketing",
    "content marketing",
    "growth agency",
    "inbound marketing",
    "performance marketing",
    "email list",
    "data & design",
    "data | design",
)

# Data / reference / directory sites that are never a business lead.
REFERENCE_SIGNALS = (
    "pincode",
    "pin code",
    "postal code",
    "postcode lookup",
    "census data",
    "census of india",
    "village directory",
    "business directory",
    "city directory",
    "address directory",
    "directory listing",
    "stock quote",
    "share price of",
    "weather forecast",
    "movie review",
    "book review",
    "download pdf",
    "read online",
    "free download",
    "lyrics",
    "obituary",
    "classified ads",
    "news aggregator",
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


# Brand names that should never be treated as clients even when hosted on a
# subdomain we do not recognize (Google Workspace -> workspace.google.gg).
MEGA_BRAND_MARKERS = (
    "google", "microsoft", "apple", "amazon", "microsoft365", "meta ",
    "netflix", "salesforce", "oracle", "ibm", "adobe", "shopify", "stripe",
    "square inc", "hubspot", "webflow", "opencorporates",
)


def name_is_denied(business_name: str) -> bool:
    """True when a page's business name matches a known mega brand."""
    lowered = (business_name or "").lower()
    for marker in MEGA_BRAND_MARKERS:
        if marker in lowered:
            return True
    return False


# File extensions / path fragments that signal a non-business resource page.
JUNK_PATH_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".zip", ".doc", ".docx")
JUNK_PATH_FRAGMENTS = (
    "/wiki/", "/questions/", "/q/", "/topic/", "/videos/", "/video/",
    "/download/", "/feeds/", "/wp-json/", "/api/", "/search?", "/app/",
    "/listing/", "/item/", "/collections/",
    "/blog/", "/news/", "/articles/", "/resources/", "/insights/",
    "/guides/", "/press/", "/webinars/", "/faq/", "/portfolio/",
    "/case-studies/", "/testimonials/",
)


def url_is_denied(url: str) -> bool:
    """True when a candidate website URL should be skipped outright."""
    host = _host_of(url)
    if not host:
        return True
    if host.endswith(SUFFIX_DENY):
        return True
    for deny in DENY_HOSTS:
        if host == deny or host.endswith("." + deny):
            return True
    # Blind block of the largest Indian financial majors even if subdomains vary.
    for marker in ("sbi.co", ".sbi", "hdfcbank", "bseindia", "nseindia", "ibef.org"):
        if marker in host:
            return True
    # Directly-linked assets and platform sub-paths are never a lead page.
    path = (urlparse(url).path or "").lower()
    if path.endswith(JUNK_PATH_EXTENSIONS):
        return True
    for frag in JUNK_PATH_FRAGMENTS:
        if frag in path:
            return True
    return False


def text_matches_scope(text: str, niche: Niche) -> bool:
    """True if a page's visible text looks like it belongs to the niche."""
    if not niche.scope_keywords:
        return True  # no filter configured means accept
    lowered = text.lower()
    for kw in niche.scope_keywords:
        if kw in lowered:
            return True
    return False


def text_has_exclusions(text: str, niche: Niche) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in niche.exclusion_keywords)


def is_corporate_scale(text: str) -> bool:
    """True when page signals a large listed/sovereign-scale organisation."""
    lowered = text.lower()
    hits = [s for s in CORPORATE_SCALE_SIGNALS if s in lowered]
    return len(hits) >= 1


def is_educational(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in EDUCATION_SIGNALS)


def is_portal(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in PORTAL_SIGNALS)


def is_reference(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in REFERENCE_SIGNALS)


def is_provider(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in PROVIDER_SIGNALS)


def is_provider_name(business_name: str) -> bool:
    """True when a business NAME identifies a marketing/lead-gen vendor —
    these sell into the niches, so they are never a prospect."""
    if not business_name:
        return False
    name = business_name.lower()
    return any(s in name for s in PROVIDER_NAME_SIGNALS)


def is_valid_candidate_lead(
    text: str,
    niche: Niche,
    *,
    require_contact: bool = True,
    require_scope: bool = True,
    exclude_corporate: bool = True,
    exclude_education: bool = True,
    exclude_portals: bool = True,
    exclude_reference: bool = True,
) -> bool:
    if require_scope and not text_matches_scope(text, niche):
        return False
    if text_has_exclusions(text, niche):
        return False
    if exclude_corporate and is_corporate_scale(text):
        return False
    if exclude_education and is_educational(text):
        return False
    if exclude_portals and is_portal(text):
        return False
    if exclude_reference and is_reference(text):
        return False
    if is_provider(text):
        return False
    return True
