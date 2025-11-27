import os
import time
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple, Dict

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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

    # Tuning values coming from Base44 "Search Tuning" screen
    maxOffersPerPair: Optional[int] = None
    maxOffersTotal: Optional[int] = None
    maxDatePairs: Optional[int] = None


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

    # Fields for Base44 filtering
    origin: Optional[str] = None              # origin IATA code, for example LHR
    destination: Optional[str] = None         # destination IATA code, for example TLV
    originAirport: Optional[str] = None       # full origin airport name
    destinationAirport: Optional[str] = None  # full destination airport name

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


# ------------- Env and admin token ------------- #

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

# Duffel configuration
DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

if not DUFFEL_ACCESS_TOKEN:
    print("WARNING: DUFFEL_ACCESS_TOKEN is not set, searches will fail")


# ------------- Limits and tuning defaults ------------- #

# Reasonable defaults if Base44 does not send tuning values
DEFAULT_MAX_OFFERS_PER_PAIR = 50
DEFAULT_MAX_OFFERS_TOTAL = 5000
DEFAULT_MAX_DATE_PAIRS = 20

# Hard safety caps so nobody can overload the server
HARD_CAP_OFFERS_PER_PAIR = 100
HARD_CAP_OFFERS_TOTAL = 8000
HARD_CAP_DATE_PAIRS = 60

# Per airline fairness cap
MAX_RESULTS_PER_AIRLINE = 80

# Wall clock hard limit for a search
MAX_SEARCH_SECONDS = 25.0

# Duffel HTTP timeouts per request
DUFFEL_TIMEOUT_SECONDS = 10.0


# ------------- Helpers ------------- #

def duffel_headers() -> dict:
    return {
        "Authorization": f"Bearer {DUFFEL_ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Duffel-Version": DUFFEL_VERSION,
    }


def generate_date_pairs(params: SearchParams, max_pairs: int) -> List[Tuple[date, date]]:
    """
    Generate (departure, return) pairs across the window,
    respecting minStayDays and maxStayDays, then clamp to max_pairs.
    """
    pairs: List[Tuple[date, date]] = []

    min_stay = max(1, params.minStayDays)
    max_stay = max(min_stay, params.maxStayDays)

    stays = list(range(min_stay, max_stay + 1))
    current = params.earliestDeparture

    while current <= params.latestDeparture and len(pairs) < max_pairs:
        for stay in stays:
            ret = current + timedelta(days=stay)
            if ret <= params.latestDeparture:
                pairs.append((current, ret))
                if len(pairs) >= max_pairs:
                    break
        current += timedelta(days=1)

    return pairs[:max_pairs]


def duffel_create_offer_request(
    slices: List[dict],
    passengers: List[dict],
    cabin_class: str,
) -> dict:
    """
    Call Duffel to create an offer request and return the JSON body.
    """
    if not DUFFEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Duffel not configured")

    url = f"{DUFFEL_API_BASE}/air/offer_requests"
    payload = {
        "data": {
            "slices": slices,
            "passengers": passengers,
            "cabin_class": cabin_class.lower(),
        }
    }

    resp = requests.post(
        url,
        json=payload,
        headers=duffel_headers(),
        timeout=DUFFEL_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        print("Duffel offer_requests error:", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Duffel API error")

    body = resp.json()
    return body.get("data", {})


def duffel_list_offers(offer_request_id: str, limit: int) -> List[dict]:
    """
    List offers for a given offer request.
    Simple one page fetch, then truncate to limit.
    """
    url = f"{DUFFEL_API_BASE}/air/offers"
    params = {
        "offer_request_id": offer_request_id,
        "limit": min(limit, 300),
        "sort": "total_amount",
    }

    resp = requests.get(
        url,
        params=params,
        headers=duffel_headers(),
        timeout=DUFFEL_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        print("Duffel offers error:", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Duffel API error")

    body = resp.json()
    data = body.get("data", [])
    return list(data)[:limit]


def build_iso_duration(minutes: int) -> str:
    """
    Create a rough ISO 8601 duration string like PT4H30M from minutes.
    """
    if minutes <= 0:
        return "PT0M"
    hours = minutes // 60
    mins = minutes % 60
    if hours and mins:
        return f"PT{hours}H{mins}M"
    if hours:
        return f"PT{hours}H"
    return f"PT{mins}M"


def map_duffel_offer_to_option(
    offer: dict,
    dep: date,
    ret: date,
) -> FlightOption:
    """
    Map Duffel offer JSON to our FlightOption model.
    """

    price = float(offer.get("total_amount", 0))
    currency = offer.get("total_currency", "GBP")

    owner = offer.get("owner", {}) or {}
    airline_code = owner.get("iata_code")
    airline_name = AIRLINE_NAMES.get(airline_code, owner.get("name", airline_code or "Airline"))
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    slices = offer.get("slices", []) or []
    outbound_segments = []
    if slices:
        outbound_segments = slices[0].get("segments", []) or []

    stops_outbound = max(0, len(outbound_segments) - 1)

    # Duration and airport info
    duration_minutes = 0
    origin_code = None
    destination_code = None
    origin_airport = None
    destination_airport = None

    if outbound_segments:
        first_segment = outbound_segments[0]
        last_segment = outbound_segments[-1]

        origin_obj = first_segment.get("origin", {}) or {}
        dest_obj = last_segment.get("destination", {}) or {}

        origin_code = origin_obj.get("iata_code")
        destination_code = dest_obj.get("iata_code")
        origin_airport = origin_obj.get("name")
        destination_airport = dest_obj.get("name")

        dep_at = first_segment.get("departing_at")
        arr_at = last_segment.get("arriving_at")

        try:
            dep_dt = datetime.fromisoformat(dep_at.replace("Z", "+00:00"))
            arr_dt = datetime.fromisoformat(arr_at.replace("Z", "+00:00"))
            duration_minutes = int((arr_dt - dep_dt).total_seconds() // 60)
        except Exception:
            duration_minutes = 0

    iso_duration = build_iso_duration(duration_minutes)

    return FlightOption(
        id=offer.get("id", ""),
        airline=airline_name,
        airlineCode=airline_code or None,
        price=price,
        currency=currency,
        departureDate=dep.isoformat(),
        returnDate=ret.isoformat(),
        stops=stops_outbound,
        durationMinutes=duration_minutes,
        totalDurationMinutes=duration_minutes,
        duration=iso_duration,
        origin=origin_code,
        destination=destination_code,
        originAirport=origin_airport,
        destinationAirport=destination_airport,
        bookingUrl=booking_url,
        url=booking_url,
    )


def apply_filters(options: List[FlightOption], params: SearchParams) -> List[FlightOption]:
    """
    Apply price and stops filters, then sort by price.
    """
    filtered = list(options)

    if params.maxPrice is not None and params.maxPrice > 0:
        filtered = [o for o in filtered if o.price <= params.maxPrice]

    if params.stopsFilter:
        allowed = set(params.stopsFilter)
        if 3 in allowed:
            filtered = [o for o in filtered if (o.stops in allowed or o.stops >= 3)]
        else:
            filtered = [o for o in filtered if o.stops in allowed]

    filtered.sort(key=lambda x: x.price)
    return filtered


def limit_by_airline(options: List[FlightOption],
                     max_per_airline: int,
                     max_total: int) -> List[FlightOption]:
    """
    Ensure no single airline dominates the list.

    Strategy:
    - Group by airline code (or name if missing)
    - Keep at most max_per_airline cheapest options per airline
    - Merge all buckets and sort by price
    - Return up to max_total overall
    """
    buckets: Dict[str, List[FlightOption]] = {}

    for opt in options:
        key = opt.airlineCode or opt.airline
        buckets.setdefault(key, []).append(opt)

    trimmed: List[FlightOption] = []
    for key, bucket in buckets.items():
        # bucket is already roughly price sorted, but sort to be sure
        bucket_sorted = sorted(bucket, key=lambda o: o.price)
        trimmed.extend(bucket_sorted[:max_per_airline])

    trimmed.sort(key=lambda o: o.price)
    return trimmed[:max_total]


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
    - Generate valid (departure, return) date pairs across the window.
    - Clamp by maxDatePairs and a global time limit.
    - For each pair, call Duffel for a round trip.
    - Limit offers per date pair and overall to protect the server.
    - Apply price and stops filters.
    - Enforce a per airline cap so no airline dominates.
    """

    if not DUFFEL_ACCESS_TOKEN:
        return {
            "status": "error",
            "source": "duffel_not_configured",
            "options": [],
        }

    # Resolve tuning values with safety caps
    max_offers_per_pair = params.maxOffersPerPair or DEFAULT_MAX_OFFERS_PER_PAIR
    max_offers_per_pair = max(1, min(max_offers_per_pair, HARD_CAP_OFFERS_PER_PAIR))

    max_offers_total = params.maxOffersTotal or DEFAULT_MAX_OFFERS_TOTAL
    max_offers_total = max(1, min(max_offers_total, HARD_CAP_OFFERS_TOTAL))

    max_date_pairs = params.maxDatePairs or DEFAULT_MAX_DATE_PAIRS
    max_date_pairs = max(1, min(max_date_pairs, HARD_CAP_DATE_PAIRS))

    # Generate date pairs within the cap
    date_pairs = generate_date_pairs(params, max_pairs=max_date_pairs)
    if not date_pairs:
        return {
            "status": "ok",
            "source": "no_date_pairs",
            "options": [],
        }

    collected_offers: List[Tuple[dict, date, date]] = []
    total_count = 0

    start_time = time.time()

    for dep, ret in date_pairs:
        # Global time guard to avoid browser timeouts
        if time.time() - start_time > MAX_SEARCH_SECONDS:
            print("Stopping search early due to time limit")
            break

        if total_count >= max_offers_total:
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
            offer_request = duffel_create_offer_request(slices, pax, params.cabin)
            offer_request_id = offer_request.get("id")
            if not offer_request_id:
                continue

            per_pair_limit = min(max_offers_per_pair, max_offers_total - total_count)
            offers_json = duffel_list_offers(offer_request_id, limit=per_pair_limit)
        except HTTPException as e:
            print("Duffel error for", dep, "to", ret, ":", e.detail)
            continue
        except Exception as e:
            print("Unexpected Duffel error for", dep, "to", ret, ":", e)
            continue

        for offer in offers_json:
            collected_offers.append((offer, dep, ret))
            total_count += 1
            if total_count >= max_offers_total:
                break

    if not collected_offers:
        return {
            "status": "ok",
            "source": "duffel_no_results",
            "options": [],
        }

    mapped: List[FlightOption] = [
        map_duffel_offer_to_option(offer, dep, ret)
        for offer, dep, ret in collected_offers
    ]

    filtered = apply_filters(mapped, params)

    # Fairness per airline
    max_results_total = min(max_offers_total, HARD_CAP_OFFERS_TOTAL)
    limited = limit_by_airline(filtered, MAX_RESULTS_PER_AIRLINE, max_results_total)

    return {
        "status": "ok",
        "source": "duffel",
        "options": [o.dict() for o in limited],
    }


# ------------- Admin credits endpoint ------------- #

USER_WALLETS: Dict[str, int] = {}


@app.post("/admin/add-credits")
def admin_add_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
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

    if not DUFFEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Duffel not configured")

    slices = [
        {
            "origin": origin,
            "destination": destination,
            "departure_date": departure.isoformat(),
        }
    ]
    pax = [{"type": "adult"} for _ in range(passengers)]

    offer_request = duffel_create_offer_request(slices, pax, "business")
    offer_request_id = offer_request.get("id")
    if not offer_request_id:
        return {"status": "error", "message": "No offer_request id from Duffel"}

    offers_json = duffel_list_offers(offer_request_id, limit=50)

    results = []
    for offer in offers_json:
        owner = offer.get("owner", {}) or {}
        results.append(
            {
                "id": offer.get("id"),
                "airline": owner.get("name"),
                "airlineCode": owner.get("iata_code"),
                "price": float(offer.get("total_amount", 0)),
                "currency": offer.get("total_currency", "GBP"),
            }
        )

    return {
        "status": "ok",
        "source": "duffel",
        "offers": results,
    }
