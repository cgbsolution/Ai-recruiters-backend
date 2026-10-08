"""
Place names for resume parsing: which strings are real locations.

The old location extractor took whatever followed the letters "city",
"location", "address"... anywhere in a resume, even inside other words, so
"cost allocation, and profitability" became the location "And Profitability"
and "capacity building" became "Building". Locations are now accepted only
when they name a known city (or "Town, State" with a known state/country), so
a sentence fragment can never pass as one.
"""

import re
from functools import lru_cache
from typing import Dict, Iterable, Optional

# canonical name -> extra spellings (lower-case). Left out on purpose: cities
# whose names are also common Indian names or English words (Anand, Kota,
# Erode) — a header scan would find them in "Anand Kumar" or "erode margins".
_INDIA = {
    "Mumbai": ["bombay"], "Navi Mumbai": [], "Thane": [], "Delhi": ["new delhi"], "Delhi NCR": ["ncr", "delhi-ncr"],
    "Noida": [], "Greater Noida": [], "Gurugram": ["gurgaon"], "Faridabad": [], "Ghaziabad": [],
    "Bengaluru": ["bangalore", "bengalore", "banglore"], "Hyderabad": ["secunderabad"], "Chennai": ["madras"],
    "Kolkata": ["calcutta"], "Pune": ["poona"], "Ahmedabad": [], "Gandhinagar": [], "Surat": [], "Vadodara": ["baroda"],
    "Rajkot": [], "Jaipur": [], "Jodhpur": [], "Udaipur": [], "Ajmer": [], "Lucknow": [], "Kanpur": [],
    "Agra": [], "Varanasi": ["banaras"], "Prayagraj": ["allahabad"], "Meerut": [], "Bareilly": [], "Aligarh": [],
    "Gorakhpur": [], "Chandigarh": [], "Mohali": [], "Panchkula": [], "Ludhiana": [], "Amritsar": [], "Jalandhar": [],
    "Dehradun": [], "Haridwar": [], "Shimla": [], "Jammu": [], "Srinagar": [], "Indore": [], "Bhopal": [],
    "Gwalior": [], "Jabalpur": [], "Raipur": [], "Bhilai": [], "Bilaspur": [], "Nagpur": [], "Nashik": [],
    "Aurangabad": ["chhatrapati sambhajinagar"], "Kolhapur": [], "Solapur": [], "Sangli": [], "Satara": [],
    "Amravati": [], "Akola": [], "Goa": ["panaji", "panjim"], "Margao": [], "Vasco da Gama": [],
    "Kochi": ["cochin", "ernakulam"], "Thiruvananthapuram": ["trivandrum"], "Kozhikode": ["calicut"],
    "Thrissur": [], "Kollam": [], "Kannur": [], "Coimbatore": [], "Madurai": [], "Tiruchirappalli": ["trichy"],
    "Salem": [], "Tiruppur": [], "Vellore": [], "Tirunelveli": [], "Puducherry": ["pondicherry"],
    "Mysuru": ["mysore"], "Mangaluru": ["mangalore"], "Hubballi": ["hubli", "hubli-dharwad"], "Dharwad": [],
    "Belagavi": ["belgaum"], "Davanagere": [], "Ballari": ["bellary"], "Shivamogga": ["shimoga"], "Udupi": [],
    "Manipal": [], "Tumakuru": ["tumkur"], "Kalaburagi": ["gulbarga"], "Visakhapatnam": ["vizag", "vishakhapatnam"],
    "Vijayawada": [], "Guntur": [], "Tirupati": [], "Nellore": [], "Kakinada": [], "Rajahmundry": ["rajamahendravaram"],
    "Warangal": [], "Karimnagar": [], "Bhubaneswar": [], "Cuttack": [], "Rourkela": [], "Patna": [],
    "Gaya": [], "Muzaffarpur": [], "Ranchi": [], "Jamshedpur": [], "Dhanbad": [], "Bokaro": [], "Guwahati": [],
    "Shillong": [], "Imphal": [], "Agartala": [], "Siliguri": [], "Durgapur": [], "Asansol": [], "Howrah": [],
    "Bhiwadi": [], "Sonipat": [], "Panipat": [], "Karnal": [], "Rohtak": [], "Hisar": [], "Ambala": [],
    "Jhansi": [], "Mathura": [], "Noida Extension": [], "Kharghar": [], "Vapi": [], "Bharuch": [],
    "Jamnagar": [], "Bhavnagar": [], "Hosur": [], "Port Blair": [], "Leh": [], "Gangtok": [], "Itanagar": [],
    "Aizawl": [], "Kohima": [], "Silchar": [], "Dibrugarh": [], "Jorhat": [],
}
_WORLD = {
    "Dubai": [], "Abu Dhabi": [], "Sharjah": [], "Ajman": [], "Al Ain": [], "Ras Al Khaimah": [], "Fujairah": [],
    "Riyadh": [], "Jeddah": [], "Dammam": [], "Al Khobar": ["khobar"], "Doha": [], "Muscat": [], "Kuwait City": [],
    "Manama": [], "Singapore": [], "Kuala Lumpur": [], "Bangkok": [], "Hong Kong": [], "Tokyo": [], "Sydney": [],
    "Melbourne": [], "Brisbane": [], "Perth": [], "Auckland": [], "London": [], "Manchester": [], "Birmingham": [],
    "Edinburgh": [], "Glasgow": [], "Dublin": [], "Berlin": [], "Munich": [], "Frankfurt": [], "Amsterdam": [],
    "Paris": [], "Zurich": [], "Stockholm": [], "Warsaw": [], "New York": ["nyc"], "San Francisco": [],
    "San Jose": [], "Los Angeles": [], "Seattle": [], "Chicago": [], "Boston": [], "Austin": [], "Dallas": [],
    "Houston": [], "Atlanta": [], "Denver": [], "New Jersey": [], "Toronto": [], "Vancouver": [], "Montreal": [],
    "Calgary": [], "Kathmandu": [], "Dhaka": [], "Colombo": [], "Karachi": [], "Lahore": [], "Nairobi": [],
    "Johannesburg": [], "Cape Town": [], "Lagos": [], "Cairo": [],
}
STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana",
    "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur",
    "meghalaya", "mizoram", "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal", "jammu and kashmir", "ladakh",
    "delhi", "chandigarh", "puducherry", "andaman and nicobar islands", "dadra and nagar haveli", "lakshadweep",
}
COUNTRIES = {
    "india", "uae", "united arab emirates", "saudi arabia", "ksa", "qatar", "oman", "kuwait", "bahrain",
    "singapore", "malaysia", "usa", "united states", "us", "uk", "united kingdom", "canada", "australia",
    "germany", "netherlands", "ireland", "france", "new zealand", "nepal", "sri lanka", "bangladesh",
}
# Words that never appear in a place name but do in the sentence fragments
# the old extractor produced ("and profitability", "of the client").
_NOT_PLACE_WORDS = {
    "and", "or", "the", "of", "to", "with", "for", "from", "by", "on", "at", "in", "as", "using", "via",
    "experience", "years", "months", "team", "client", "clients", "project", "projects", "management",
    "analysis", "reporting", "process", "processes", "models", "model", "skills", "company", "employer",
}


def _aliases() -> Dict[str, str]:
    table: Dict[str, str] = {}
    for group in (_INDIA, _WORLD):
        for canonical, extra in group.items():
            table[canonical.lower()] = canonical
            for alias in extra:
                table[alias] = canonical
    return table


ALIASES = _aliases()


@lru_cache(maxsize=1)
def _city_regex() -> re.Pattern:
    # Longest first, so "Navi Mumbai" wins over "Mumbai", "Greater Noida" over "Noida".
    names = sorted(ALIASES, key=len, reverse=True)
    return re.compile(r"(?<![a-z])(" + "|".join(re.escape(n) for n in names) + r")(?![a-z])", re.IGNORECASE)


def find_city(text: Optional[str]) -> Optional[str]:
    """Canonical name of the first known city mentioned in `text`."""
    if not text:
        return None
    m = _city_regex().search(text)
    return ALIASES[m.group(1).lower()] if m else None


def _clean(part: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z .'-]", " ", part)).strip(" .-'")


def looks_like_place_name(part: str) -> bool:
    """A short run of words with no sentence words in it ("Bhiwadi", "C.V. Raman Nagar")."""
    words = _clean(part).lower().split()
    return 0 < len(words) <= 4 and not (set(words) & _NOT_PLACE_WORDS) and all(len(w) > 1 for w in words)


def place_from_text(value: Optional[str]) -> Optional[str]:
    """A location from a free-text value (an "Address:" line, a parsed
    location), or None when it isn't one.

    Known city anywhere -> its canonical name. Otherwise a "Town, State" or
    "Town, Country" pair with a known state/country -> "Town". Anything else
    (sentence fragments, job titles) -> None.
    """
    if not value or not isinstance(value, str):
        return None
    city = find_city(value)
    if city:
        return city
    parts = [_clean(p) for p in re.split(r"[,/|;\n]|\s-\s", value)]
    parts = [p for p in parts if p]
    for i, part in enumerate(parts):
        later = {p.lower() for p in parts[i + 1:]}
        if part.lower() in STATES or part.lower() in COUNTRIES:
            continue
        if (later & STATES or later & COUNTRIES) and looks_like_place_name(part):
            return part.title()
    return None


def is_plausible_location(value: Optional[str]) -> bool:
    return place_from_text(value) is not None


def known_place_names() -> Iterable[str]:
    return sorted(set(ALIASES.values()))
