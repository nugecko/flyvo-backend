import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple

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


# ------------- Env and constants ------------- #

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

# Safety limits so searches do not blow up
MAX_DATE_PAIRS = 40        # max outbound/return combinations to scan
MAX_OFFERS = 2000          # max offers across all date pairs


# ------------- Helpers ------------- #

def generate_date_pairs(params: SearchParams, max_pairs: int = MAX_DATE_PAIRS) -> List[Tuple[date, date]]:
    """
    Generate (departure, return) pairs inside the window, respecting minStayDays and maxStayDays.
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

    return pairs


def map_duffel_offer_to_option(offer: dict, dep: date, ret: date) -> FlightOption:
    """
    Map a Duffel offer dict into your FlightOption model.
    """
    price = float(offer["total_amount"])
    currency = offer["total_currency"]

    slices = offer.get("slices", [])
    duration_minutes = 0
    stops_outbound = 0

    if slices:
        first_slice = slices[0]
        segments = first_slice.get("segments", [])
        if segments:
            stops_outbound = max(0, len(segments) - 1)
            try:
                first_seg = segments[0]
                last_seg = segments[-1]
                dep_dt = datetime.fromisoformat(first_seg["departing_at"].replace("Z", "+00:00"))
                arr_dt = datetime.fromisoformat(last_seg["arriving_at"].replace("Z", "+00:00"))
                duration_minutes = int((arr_dt - dep_dt).total_seconds() // 60)
            except Exception:
                duration_minutes = 0

    owner = offer.get("owner", {}) or {}
    airline_code = owner.get("iata_code")
    airline_name = AIRLINE_NAMES.get(airline_code, owner.get("name", airline_code or "Airline"))
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    return FlightOption(
        id=offer["id"],
        airline=airline_name,
        airlineCode=airline_code,
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


def duffel_create_offer_request(payload: dict) -> dict:
    """
    Low level helper that calls Duffel POST /air/offer_requests with the correct headers.
    Returns the parsed JSON response.
    """
    if not DUFFEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Duffel access token not configured")

    url = f"{DUFFEL_API_BASE}/air/offer_requests"
    headers = {
        "Authorization": f"Bearer {DUFFEL_ACCESS_TOKEN}",
        "Duffel-Version": DUFFEL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json={"data": payload}, timeout=30)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Duffel API error: {e}")

    if resp.status_code >= 400:
        # Surface Duffel error in a readable way
        try:
            err = resp.json()
        except Exception:
            err = {"error": resp.text}
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Duffel API error: {err}",
        )

    try:
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Invalid JSON from Duffel: {e}")


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

    Behaviour:
    - Generate all valid (departure, return) date pairs inside the window.
    - For each pair, call Duffel for a round trip (two slices).
    - Collect offers up to MAX_OFFERS.
    - Map to FlightOption, then apply price and stops filters.

    No bookings are created, this is search only.
    """

    date_pairs = generate_date_pairs(params, max_pairs=MAX_DATE_PAIRS)

    if not date_pairs:
        # Nothing to search
        return {
            "status": "ok",
            "source": "duffel_no_dates",
            "options": [],
        }

    collected: List[FlightOption] = []
    offers_count = 0

    for dep, ret in date_pairs:
        if offers_count >= MAX_OFFERS:
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
        passengers = [{"type": "adult"} for _ in range(params.passengers)]

        payload = {
            "slices": slices,
            "cabin_class": params.cabin.lower(),
            "passengers": passengers,
        }

        response_json = duffel_create_offer_request(payload)
        offer_request = response_json.get("data", {})
        offers = offer_request.get("offers", []) or []

        for offer in offers:
            option = map_duffel_offer_to_option(offer, dep, ret)
            collected.append(option)
            offers_count += 1
            if offers_count >= MAX_OFFERS:
                break

    if not collected:
        return {
            "status": "ok",
            "source": "duffel_no_results",
            "options": [],
        }

    filtered = apply_filters(collected, params)

    return {
        "status": "ok",
        "source": "duffel",
        "options": [o.dict() for o in filtered],
    }


# ------------- Admin credits endpoint ------------- #

USER_WALLETS: dict[str, int] = {}


@app.post("/admin/add-credits")
def admin_add_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
    # Debug logging for token mismatch investigation
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
    Simple test endpoint for Duffel search with a single slice.
    Uses whatever DUFFEL_ACCESS_TOKEN is configured (test or live).
    No bookings are created.
    """
    slices = [{
        "origin": origin,
        "destination": destination,
        "departure_date": departure.isoformat(),
    }]
    pax = [{"type": "adult"} for _ in range(passengers)]

    payload = {
        "slices": slices,
        "cabin_class": "business",
        "passengers": pax,
    }

    response_json = duffel_create_offer_request(payload)
    offer_request = response_json.get("data", {})
    offers = offer_request.get("offers", []) or []

    results = []
    for offer in offers:
        owner = offer.get("owner", {}) or {}
        results.append({
            "id": offer["id"],
            "airline": owner.get("name"),
            "airlineCode": owner.get("iata_code"),
            "price": float(offer["total_amount"]),
            "currency": offer["total_currency"],
        })

    return {
        "status": "ok",
        "source": "duffel",
        "offers": results,
    }
