import os
import re
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from amadeus import Client, ResponseError  # kept for possible future use
from duffel_api import Duffel

from airlines import AIRLINE_NAMES, AIRLINE_BOOKING_URLS


# ------------- Models ------------- #

class SearchParams(BaseModel):
    origin: str
    destination: str
    earliestDeparture: date
    latestDeparture: date
    minStayDays: int
    maxStayDays: int
    # None means no price limit
    maxPrice: Optional[float] = None
    cabin: str = "BUSINESS"
    passengers: int = 1
    # Optional filter for number of stops, for example [0] or [0, 1, 2]
    # 3 is treated as "3 or more stops"
    stopsFilter: Optional[List[int]] = None


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


class CreditUpdateRequest(BaseModel):
    userId: str
    amount: Optional[int] = None
    delta: Optional[int] = None
    creditAmount: Optional[int] = None
    value: Optional[int] = None
    reason: Optional[str] = None


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
# Amadeus is configured but not used in search, kept only in case you want it later

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


# ------------- Duffel client ------------- #

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")

# Use a supported Duffel API version, adjust if Duffel docs change
duffel = Duffel(access_token=DUFFEL_ACCESS_TOKEN, api_version="beta") if DUFFEL_ACCESS_TOKEN else None


# ------------- Search limits ------------- #

PAIR_OFFER_LIMIT = 40          # max offers we pull per (dep, ret) pair, ordered by cheapest
GLOBAL_OPTION_CAP = 2000       # hard cap on total unique options returned


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


def generate_date_pairs(params: SearchParams) -> List[Tuple[date, date]]:
    """
    Generate (departure, return) pairs for all valid dates
    inside the window, respecting minStayDays and maxStayDays.
    """
    pairs: List[Tuple[date, date]] = []

    # Normalise stay range
    min_stay = max(1, params.minStayDays)
    max_stay = max(min_stay, params.maxStayDays)

    stays = list(range(min_stay, max_stay + 1))

    current = params.earliestDeparture

    while current <= params.latestDeparture:
        for stay in stays:
            ret = current + timedelta(days=stay)
            if ret <= params.latestDeparture:
                pairs.append((current, ret))
        current += timedelta(days=1)

    return pairs


def map_duffel_offer_to_option(offer, dep: date, ret: date) -> FlightOption:
    """
    Map a Duffel offer to your FlightOption model.
    """
    price = float(offer.total_amount)
    currency = offer.total_currency

    first_slice = offer.slices[0]
    outbound_segments = first_slice.segments
    stops_outbound = len(outbound_segments) - 1

    # Compute rough total duration for outbound
    try:
        first_segment = outbound_segments[0]
        last_segment = outbound_segments[-1]
        dep_dt = datetime.fromisoformat(first_segment.departing_at.replace("Z", "+00:00"))
        arr_dt = datetime.fromisoformat(last_segment.arriving_at.replace("Z", "+00:00"))
        duration_minutes = int((arr_dt - dep_dt).total_seconds() // 60)
    except Exception:
        duration_minutes = 0

    airline_code = offer.owner.iata_code
    airline_name = AIRLINE_NAMES.get(airline_code, offer.owner.name)
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    return FlightOption(
        id=offer.id,
        airline=airline_name,
        airlineCode=airline_code or None,
        price=price,
        currency=currency,
        departureDate=dep.isoformat(),
        returnDate=ret.isoformat(),
        stops=stops_outbound,
        durationMinutes=duration_minutes,
        totalDurationMinutes=duration_minutes,
        duration=None,
        bookingUrl=booking_url,
        url=booking_url,
    )


def itinerary_key(offer, dep: date, ret: date) -> Tuple:
    """
    Build a key that represents a unique real trip,
    so we can collapse multiple fare brands for the same flight.
    """
    first_slice = offer.slices[0]
    segments = first_slice.segments

    first_segment = segments[0]
    last_segment = segments[-1]

    airline_code = offer.owner.iata_code
    outbound_depart = first_segment.departing_at
    outbound_arrive = last_segment.arriving_at
    stops_outbound = len(segments) - 1
    cabin = getattr(offer, "cabin_class", None) or first_slice.cabin_class

    return (
        airline_code,
        outbound_depart,
        outbound_arrive,
        stops_outbound,
        cabin,
        dep.isoformat(),
        ret.isoformat(),
    )


def apply_filters(options: List[FlightOption], params: SearchParams) -> List[FlightOption]:
    """
    Apply price and stops filters, then sort by price.
    """
    filtered = list(options)

    # Price filter
    if params.maxPrice is not None and params.maxPrice > 0:
        filtered = [o for o in filtered if o.price <= params.maxPrice]

    # Stops filter, values like [0], [0, 1], [0, 1, 2], [2, 3] and so on
    if params.stopsFilter:
        allowed = set(params.stopsFilter)
        if 3 in allowed:
            # 3 means "3 or more stops"
            filtered = [o for o in filtered if (o.stops in allowed or o.stops >= 3)]
        else:
            filtered = [o for o in filtered if o.stops in allowed]

    filtered.sort(key=lambda x: x.price)
    return filtered


# ------------- Routes: health and search ------------- #

@app.get("/")
def home():
    return {"message": "Flyyv backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search-business")
def search_business(params: SearchParams):
    """
    Main endpoint used by the Base44 frontend.

    Behaviour:
    - Generate all valid (departure, return) date pairs inside the window.
    - For each pair, call Duffel for a round trip, limited to the cheapest offers.
    - Group similar itineraries, keep only the cheapest per group.
    - Apply filters, then return up to GLOBAL_OPTION_CAP options.

    No bookings are created, this is search only.
    """

    if duffel is None:
        raise HTTPException(status_code=500, detail="Duffel not configured")

    date_pairs = generate_date_pairs(params)

    if not date_pairs:
        return {
            "status": "ok",
            "source": "duffel_no_pairs",
            "options": [],
        }

    unique_options: Dict[Tuple, FlightOption] = {}
    total_processed = 0

    # Normalise cabin name for Duffel
    duffel_cabin = params.cabin.lower()

    for dep, ret in date_pairs:
        if len(unique_options) >= GLOBAL_OPTION_CAP:
            break

        slices = [
            {
                "origin": params.origin,
                "destination": params.destination,
                "departure_date": dep.isoformat(),
            },
            {
                "origin": params.destination,
                "destination": params.origin,
                "departure_date": ret.isoformat(),
            },
        ]
        pax = [{"type": "adult"} for _ in range(params.passengers)]

        try:
            offer_request = duffel.offer_requests.create(
                data={
                    "slices": slices,
                    "cabin_class": duffel_cabin,
                    "passengers": pax,
                }
            )

            offers_iter = duffel.offers.list(
                offer_request_id=offer_request.id,
                limit=PAIR_OFFER_LIMIT,
                sort="total_amount",
            )
        except Exception as e:
            print("Duffel error for", dep, "to", ret, ":", e)
            continue

        for offer in offers_iter:
            key = itinerary_key(offer, dep, ret)
            option = map_duffel_offer_to_option(offer, dep, ret)

            if key in unique_options:
                # Keep the cheapest version of this itinerary
                if option.price < unique_options[key].price:
                    unique_options[key] = option
            else:
                unique_options[key] = option

            total_processed += 1

            if len(unique_options) >= GLOBAL_OPTION_CAP:
                break

        if len(unique_options) >= GLOBAL_OPTION_CAP:
            break

    options = list(unique_options.values())

    if not options:
        return {
            "status": "ok",
            "source": "duffel_no_results",
            "options": [],
        }

    filtered = apply_filters(options, params)

    return {
        "status": "ok",
        "source": "duffel",
        "options": [o.dict() for o in filtered],
    }


# ------------- Admin credits endpoint ------------- #

USER_WALLETS: Dict[str, int] = {}


@app.post("/admin/add-credits")
def admin_add_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
    # Debug logging for token mismatch investigation
    print("DEBUG_received_token:", repr(x_admin_token))
    print("DEBUG_expected_token:", repr(ADMIN_API_TOKEN))

    # Normalise values
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

    current = USER_WALLETS.get(payload.userId, 0)
    new_balance = max(0, current + change_amount)
    USER_WALLETS[payload.userId] = new_balance

    return {
        "userId": payload.userId,
        "newBalance": new_balance,
    }


# ------------- Duffel test endpoint ------------- #

@app.get("/duffel-test")
def duffel_test(
    origin: str,
    destination: str,
    departure: date,
    passengers: int = 1,
):
    """
    Simple test endpoint for Duffel search.
    Uses whatever DUFFEL_ACCESS_TOKEN is configured (test or live).
    No bookings are created.
    """

    if duffel is None:
        return {"status": "error", "message": "Duffel not configured"}

    slices = [
        {
            "origin": origin,
            "destination": destination,
            "departure_date": departure.isoformat(),
        }
    ]
    pax = [{"type": "adult"} for _ in range(passengers)]

    try:
        offer_request = duffel.offer_requests.create(
            data={
                "slices": slices,
                "cabin_class": "business",
                "passengers": pax,
            }
        )
        offers_iter = duffel.offers.list(
            offer_request_id=offer_request.id,
            limit=PAIR_OFFER_LIMIT,
            sort="total_amount",
        )
    except Exception as e:
        print("Duffel error:", e)
        raise HTTPException(status_code=500, detail=f"Duffel API error: {e}")

    results = []
    for offer in offers_iter:
        results.append(
            {
                "id": offer.id,
                "airline": offer.owner.name,
                "airlineCode": offer.owner.iata_code,
                "price": float(offer.total_amount),
                "currency": offer.total_currency,
            }
        )

    return {
        "status": "ok",
        "source": "duffel",
        "offers": results,
    }
