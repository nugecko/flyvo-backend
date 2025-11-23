import os
import re
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from amadeus import Client, ResponseError


# ------------- Models ------------- #

class SearchParams(BaseModel):
    origin: str
    destination: str
    earliestDeparture: date
    latestDeparture: date
    minStayDays: int
    maxStayDays: int
    maxPrice: float
    cabin: str = "BUSINESS"
    passengers: int = 1


class FlightOption(BaseModel):
    id: str
    airline: str
    airlineCode: Optional[str] = None
    price: float
    currency: str
    departureDate: str
    returnDate: str
    stops: int

    durationMinutes: int
    totalDurationMinutes: Optional[int] = None
    duration: Optional[str] = None

    bookingUrl: Optional[str] = None
    url: Optional[str] = None


# ------------- FastAPI app ------------- #

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # later you can restrict this to your Base44 domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------- Amadeus client and admin token ------------- #

AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
AMADEUS_ENV = os.getenv("AMADEUS_ENV", "test")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

hostname = "production" if AMADEUS_ENV and AMADEUS_ENV.lower() == "production" else "test"

amadeus = None
if AMADEUS_API_KEY and AMADEUS_API_SECRET:
    amadeus = Client(
        client_id=AMADEUS_API_KEY,
        client_secret=AMADEUS_API_SECRET,
        hostname=hostname,
    )


# ------------- Airline maps ------------- #

AIRLINE_NAMES = {
    # Europe
    "LH": "Lufthansa",
    "BA": "British Airways",
    "AF": "Air France",
    "KL": "KLM",
    "LX": "SWISS",
    "SN": "Brussels Airlines",
    "OS": "Austrian Airlines",
    "SK": "Scandinavian Airlines",
    "AZ": "ITA Airways",
    "LO": "LOT Polish Airlines",
    "TP": "TAP Air Portugal",
    "IB": "Iberia",
    "VY": "Vueling",
    "U2": "easyJet",
    "FR": "Ryanair",
    "W6": "Wizz Air",
    "EI": "Aer Lingus",
    "AY": "Finnair",
    "BT": "airBaltic",
    "OK": "Czech Airlines",
    "RO": "TAROM",
    "JU": "Air Serbia",
    "FB": "Bulgaria Air",
    "HV": "Transavia",
    "DS": "easyJet Switzerland",
    "A3": "Aegean Airlines",
    "TK": "Turkish Airlines",
    "LG": "Luxair",
    "X3": "TUIfly",
    "LS": "Jet2",
    "VS": "Virgin Atlantic",
    # Middle East
    "LY": "El Al",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "EY": "Etihad Airways",
    "SV": "Saudia",
    "RJ": "Royal Jordanian",
    "WY": "Oman Air",
    "GF": "Gulf Air",
    "ME": "Middle East Airlines",
    # North America
    "AA": "American Airlines",
    "DL": "Delta Air Lines",
    "UA": "United Airlines",
    "B6": "JetBlue Airways",
    "WS": "WestJet",
    "AC": "Air Canada",
    "F9": "Frontier Airlines",
    "NK": "Spirit Airlines",
    "AS": "Alaska Airlines",
    "HA": "Hawaiian Airlines",
    # Latin America
    "LA": "LATAM Airlines",
    "AV": "Avianca",
    "AM": "Aeromexico",
    "CM": "Copa Airlines",
    "G3": "Gol Linhas Aéreas",
    "UX": "Air Europa",
    # Asia
    "SQ": "Singapore Airlines",
    "CX": "Cathay Pacific",
    "JL": "Japan Airlines",
    "NH": "ANA All Nippon Airways",
    "KE": "Korean Air",
    "OZ": "Asiana Airlines",
    "CI": "China Airlines",
    "BR": "EVA Air",
    "MU": "China Eastern Airlines",
    "CA": "Air China",
    "CZ": "China Southern Airlines",
    "HX": "Hong Kong Airlines",
    "TG": "Thai Airways",
    "VN": "Vietnam Airlines",
    "MH": "Malaysia Airlines",
    "GA": "Garuda Indonesia",
    "PR": "Philippine Airlines",
    "AI": "Air India",
    "UK": "Vistara",
    "6E": "IndiGo",
    "SG": "SpiceJet",
    "TR": "Scoot",
    "D7": "AirAsia X",
    "AK": "AirAsia",
    # Oceania
    "QF": "Qantas",
    "NZ": "Air New Zealand",
    "VA": "Virgin Australia",
    "JQ": "Jetstar",
    # Africa
    "ET": "Ethiopian Airlines",
    "KQ": "Kenya Airways",
    "MS": "EgyptAir",
    "AT": "Royal Air Maroc",
    "TU": "Tunisair",
    "SA": "South African Airways",
    "HM": "Air Seychelles",
    "WB": "RwandAir",
    # Low cost and regional
    "XR": "Corendon Airlines",
    "XQ": "SunExpress",
    "PC": "Pegasus Airlines",
}

AIRLINE_BOOKING_URLS = {
    "LH": "https://www.lufthansa.com/gb/en/flight-search",
    "BA": "https://www.britishairways.com/travel/home/public/en_gb",
    "AF": "https://wwws.airfrance.co.uk/",
    "KL": "https://www.klm.co.uk/",
    "LX": "https://www.swiss.com/gb/en/homepage",
    "SN": "https://www.brusselsairlines.com/",
    "OS": "https://www.austrian.com/",
    "SK": "https://www.flysas.com/en/",
    "AZ": "https://www.ita-airways.com/en_gb",
    "LO": "https://www.lot.com/gb/en",
    "TP": "https://www.flytap.com/en-gb/",
    "IB": "https://www.iberia.com/gb/",
    "VY": "https://www.vueling.com/en",
    "U2": "https://www.easyjet.com/en",
    "FR": "https://www.ryanair.com/gb/en",
    "W6": "https://wizzair.com/",
    "EI": "https://www.aerlingus.com/",
    "AY": "https://www.finnair.com/",
    "BT": "https://www.airbaltic.com/en/",
    "OK": "https://www.csa.cz/en/",
    "RO": "https://www.tarom.ro/en",
    "JU": "https://www.airserbia.com/en",
    "FB": "https://www.air.bg/en",
    "HV": "https://www.transavia.com/en-UK/home/",
    "DS": "https://www.easyjet.com/en",
    "A3": "https://en.aegeanair.com/",
    "TK": "https://www.turkishairlines.com/en-int/flights/",
    "LG": "https://www.luxair.lu/en",
    "X3": "https://www.tuifly.com/",
    "LS": "https://www.jet2.com/",
    "VS": "https://www.virginatlantic.com/",
    "LY": "https://www.elal.com/en/",
    "EK": "https://www.emirates.com/uk/english/",
    "QR": "https://www.qatarairways.com/en-gb/homepage.html",
    "EY": "https://www.etihad.com/en-gb/",
    "SV": "https://www.saudia.com/",
    "RJ": "https://www.rj.com/",
    "WY": "https://www.omanair.com/",
    "GF": "https://www.gulfair.com/",
    "ME": "https://www.mea.com.lb/english",
    "AA": "https://www.aa.com/",
    "DL": "https://www.delta.com/",
    "UA": "https://www.united.com/",
    "B6": "https://www.jetblue.com/",
    "WS": "https://www.westjet.com/",
    "AC": "https://www.aircanada.com/",
    "F9": "https://www.flyfrontier.com/",
    "NK": "https://www.spirit.com/",
    "AS": "https://www.alaskaair.com/",
    "HA": "https://www.hawaiianairlines.com/",
    "LA": "https://www.latamairlines.com/",
    "AV": "https://www.avianca.com/",
    "AM": "https://aeromexico.com/en-gb",
    "CM": "https://www.copaair.com/",
    "G3": "https://www.voegol.com.br/en",
    "UX": "https://www.aireuropa.com/",
    "SQ": "https://www.singaporeair.com/",
    "CX": "https://www.cathaypacific.com/",
    "JL": "https://www.jal.co.jp/jp/en/",
    "NH": "https://www.ana.co.jp/en/jp/",
    "KE": "https://www.koreanair.com/",
    "OZ": "https://flyasiana.com/",
    "CI": "https://www.china-airlines.com/",
    "BR": "https://www.evaair.com/",
    "MU": "https://www.ceair.com/",
    "CA": "https://www.airchina.com/",
    "CZ": "https://www.csair.com/",
    "HX": "https://www.hongkongairlines.com/",
    "TG": "https://www.thaiairways.com/",
    "VN": "https://www.vietnamairlines.com/",
    "MH": "https://www.malaysiaairlines.com/",
    "GA": "https://www.garuda-indonesia.com/",
    "PR": "https://www.philippineairlines.com/",
    "AI": "https://www.airindia.com/",
    "UK": "https://www.airvistara.com/",
    "6E": "https://www.goindigo.in/",
    "SG": "https://www.spicejet.com/",
    "TR": "https://www.flyscoot.com/",
    "D7": "https://www.airasia.com/",
    "AK": "https://www.airasia.com/",
    "QF": "https://www.qantas.com/",
    "NZ": "https://www.airnewzealand.co.uk/",
    "VA": "https://www.virginaustralia.com/",
    "JQ": "https://www.jetstar.com/",
    "ET": "https://www.ethiopianairlines.com/",
    "KQ": "https://www.kenya-airways.com/",
    "MS": "https://www.egyptair.com/",
    "AT": "https://www.royalairmaroc.com/",
    "TU": "https://www.tunisair.com/",
    "SA": "https://www.flysaa.com/",
    "HM": "https://www.airseychelles.com/",
    "WB": "https://www.rwandair.com/",
    "XR": "https://www.corendonairlines.com/",
    "XQ": "https://www.sunexpress.com/en/",
    "PC": "https://www.flypgs.com/en",
}


# ------------- Helpers ------------- #

def parse_iso_duration_to_minutes(iso_duration: str) -> int:
    if not iso_duration:
        return 0
    pattern = r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?"
    match = re.match(pattern, iso_duration)
    if not match:
        return 0
    days_str, hours_str, minutes_str = match.groups()
    days = int(days_str) if days_str else 0
    hours = int(hours_str) if hours_str else 0
    minutes = int(minutes_str) if minutes_str else 0
    return (days * 24 + hours) * 60 + minutes


def dummy_results(params: SearchParams) -> List[FlightOption]:
    return [
        FlightOption(
            id="TK123",
            airline="Turkish Airlines",
            airlineCode="TK",
            price=1299,
            currency="GBP",
            departureDate=params.earliestDeparture.isoformat(),
            returnDate=params.latestDeparture.isoformat(),
            stops=1,
            durationMinutes=240,
            totalDurationMinutes=240,
            duration="PT4H0M",
            bookingUrl=AIRLINE_BOOKING_URLS.get("TK"),
            url=AIRLINE_BOOKING_URLS.get("TK"),
        ),
        FlightOption(
            id="LH456",
            airline="Lufthansa",
            airlineCode="LH",
            price=1550,
            currency="GBP",
            departureDate=params.earliestDeparture.isoformat(),
            returnDate=params.latestDeparture.isoformat(),
            stops=1,
            durationMinutes=270,
            totalDurationMinutes=270,
            duration="PT4H30M",
            bookingUrl=AIRLINE_BOOKING_URLS.get("LH"),
            url=AIRLINE_BOOKING_URLS.get("LH"),
        ),
    ]


def map_amadeus_offer_to_option(offer, params: SearchParams, index: int) -> FlightOption:
    price = float(offer["price"]["grandTotal"])
    currency = offer["price"]["currency"]

    first_itinerary = offer["itineraries"][0]
    last_itinerary = offer["itineraries"][-1]

    departure_date = first_itinerary["segments"][0]["departure"]["at"][:10]
    return_date = last_itinerary["segments"][-1]["arrival"]["at"][:10]

    airline_code = ""
    if "validatingAirlineCodes" in offer and offer["validatingAirlineCodes"]:
        airline_code = offer["validatingAirlineCodes"][0]

    airline_name = AIRLINE_NAMES.get(airline_code, airline_code or "Airline")
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    iso_duration = first_itinerary.get("duration", "PT0H0M")
    duration_minutes = parse_iso_duration_to_minutes(iso_duration)

    stops_outbound = len(first_itinerary["segments"]) - 1

    return FlightOption(
        id=f"offer_{index}",
        airline=airline_name,
        airlineCode=airline_code or None,
        price=price,
        currency=currency,
        departureDate=departure_date,
        returnDate=return_date,
        stops=stops_outbound,
        durationMinutes=duration_minutes,
        totalDurationMinutes=duration_minutes,
        duration=iso_duration,
        bookingUrl=booking_url,
        url=booking_url,
    )


def generate_date_pairs(params: SearchParams, max_pairs: int = 20):
    """
    Generate (departure, return) pairs within the window.
    """
    stays: List[int] = []

    if params.minStayDays == params.maxStayDays:
        stays = [params.minStayDays]
    else:
        for s in (4, 7):
            if params.minStayDays <= s <= params.maxStayDays:
                stays.append(s)

    if not stays:
        stays = [params.minStayDays]

    pairs = []
    current = params.earliestDeparture

    while current <= params.latestDeparture and len(pairs) < max_pairs:
        for stay in stays:
            ret = current + timedelta(days=stay)
            if ret <= params.latestDeparture:
                pairs.append((current, ret))
                if len(pairs) >= max_pairs:
                    break
        current += timedelta(days=1)

    return pairs


# ------------- Routes: health and search ------------- #

@app.get("/")
def home():
    return {"message": "Flyvo backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search-business")
def search_business(params: SearchParams):
    """
    Main endpoint used by the Base44 frontend.
    """

    if amadeus is None:
        return {
            "status": "ok",
            "source": "dummy_no_amadeus",
            "options": [o.dict() for o in dummy_results(params)],
        }

    try:
        window_days = (params.latestDeparture - params.earliestDeparture).days + 1
        offers = []

        if window_days <= 1:
            resp = amadeus.shopping.flight_offers_search.get(
                originLocationCode=params.origin,
                destinationLocationCode=params.destination,
                departureDate=params.earliestDeparture.isoformat(),
                returnDate=params.latestDeparture.isoformat(),
                adults=params.passengers,
                travelClass=params.cabin,
                currencyCode="GBP",
                max=20,
            )
            offers = resp.data or []

        elif window_days <= 14:
            date_pairs = generate_date_pairs(params, max_pairs=20)
            for dep, ret in date_pairs:
                try:
                    resp = amadeus.shopping.flight_offers_search.get(
                        originLocationCode=params.origin,
                        destinationLocationCode=params.destination,
                        departureDate=dep.isoformat(),
                        returnDate=ret.isoformat(),
                        adults=params.passengers,
                        travelClass=params.cabin,
                        currencyCode="GBP",
                        max=5,
                    )
                    offers.extend(resp.data or [])
                except ResponseError as e:
                    print("Amadeus error for", dep, "to", ret, ":", e)
                    continue

        else:
            print(
                "Date window larger than 14 days, using single Amadeus call "
                "from earliestDeparture to latestDeparture."
            )
            resp = amadeus.shopping.flight_offers_search.get(
                originLocationCode=params.origin,
                destinationLocationCode=params.destination,
                departureDate=params.earliestDeparture.isoformat(),
                returnDate=params.latestDeparture.isoformat(),
                adults=params.passengers,
                travelClass=params.cabin,
                currencyCode="GBP",
                max=20,
            )
            offers = resp.data or []

        if not offers:
            return {
                "status": "ok",
                "source": "amadeus_no_results_fallback_dummy",
                "options": [o.dict() for o in dummy_results(params)],
            }

        mapped = [
            map_amadeus_offer_to_option(offer, params, i)
            for i, offer in enumerate(offers)
        ]

        mapped.sort(key=lambda x: x.price)

        return {
            "status": "ok",
            "source": "amadeus",
            "options": [o.dict() for o in mapped],
        }

    except ResponseError as e:
        print("Amadeus API error:", e)
        return {
            "status": "ok",
            "source": "dummy_on_error",
            "options": [o.dict() for o in dummy_results(params)],
        }
    except Exception as e:
        print("Unexpected error in search_business:", e)
        return {
            "status": "ok",
            "source": "dummy_on_error",
            "options": [o.dict() for o in dummy_results(params)],
        }


# ------------- Admin credits endpoint ------------- #

class CreditUpdateRequest(BaseModel):
    userId: str
    amount: Optional[int] = None
    delta: Optional[int] = None
    creditAmount: Optional[int] = None
    value: Optional[int] = None
    reason: Optional[str] = None


# In memory stores for MVP
USER_WALLETS: Dict[str, int] = {}
USER_WALLET_HISTORY: Dict[str, List[Dict[str, Any]]] = {}


@app.post("/admin/update-credits")
@app.post("/admin/add-credits")
def admin_update_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
    """
    Admin endpoint to adjust user wallet credits.

    Accepts multiple possible field names for the credit change:
    - delta
    - amount
    - creditAmount
    - value
    """

    # Debug logging for token investigation if needed
    print("DEBUG_received_token:", repr(x_admin_token))
    print("DEBUG_expected_token:", repr(ADMIN_API_TOKEN))

    received = (x_admin_token or "").strip()
    expected = (ADMIN_API_TOKEN or "").strip()

    if received.lower().startswith("bearer "):
        received = received[7:].strip()

    if expected == "":
        raise HTTPException(status_code=500, detail="Admin token not configured")

    if received != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")

    # Accept any field name for the credit change
    change_amount = (
        payload.delta
        if payload.delta is not None
        else payload.amount
        if payload.amount is not None
        else payload.creditAmount
        if payload.creditAmount is not None
        else payload.value
    )

    if change_amount is None:
        raise HTTPException(
            status_code=400,
            detail="Missing credit amount. Expected one of: amount, delta, creditAmount, value.",
        )

    user_id = payload.userId
    current = USER_WALLETS.get(user_id, 0)
    new_balance = max(0, current + change_amount)
    USER_WALLETS[user_id] = new_balance

    # Record history entry
    entry = {
        "date": datetime.utcnow().isoformat() + "Z",
        "description": payload.reason or "Manual adjustment",
        "change": change_amount,
        "balance_after": new_balance,
    }
    USER_WALLET_HISTORY.setdefault(user_id, []).append(entry)

    return {
        "userId": user_id,
        "newBalance": new_balance,
    }


# ------------- Profile endpoint ------------- #

@app.get("/profile")
def get_profile(
    userId: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """
    Returns profile data in the schema expected by Base44.

    Tries several ways to determine the user id:
    - userId query parameter
    - user_id query parameter
    - email query parameter
    - X-User-Id header
    """

    uid = userId or user_id or email or x_user_id or "test-user-1"

    wallet_balance = USER_WALLETS.get(uid, 0)
    history = USER_WALLET_HISTORY.get(uid, [])

    # Very simple user info for now, Base44 mainly needs wallet part
    user_obj = {
        "name": uid,
        "email": uid,
        "plan": None,  # "BASIC", "PRO", "ELITE", or null
    }

    subscription_obj = {
        "plan": "Flyvo Free",
        "billing_cycle": "N/A",
        "renewal_date": None,
        "monthly_credits": 0,
    }

    wallet_obj = {
        "wallet_credits": wallet_balance,
        "history": history,
    }

    alerts_obj = {
        "active_count": 0,
        "total_count": 0,
    }

    return {
        "user": user_obj,
        "subscription": subscription_obj,
        "wallet": wallet_obj,
        "alerts": alerts_obj,
    }
