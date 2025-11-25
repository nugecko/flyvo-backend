import os
import re
from datetime import date, timedelta, datetime
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
    maxPrice: Optional[float] = None
    cabin: str = "BUSINESS"
    passengers: int = 1
    # Optional filter for number of stops, for example [0] or [0,1,2]
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

    # Extra fields so the UI can show real details instead of TBD
    originAirport: Optional[str] = None
    destinationAirport: Optional[str] = None
    departureTime: Optional[str] = None
    arrivalTime: Optional[str] = None


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


# ------------- Duffel client ------------- #

# Prefer DUFFEL_ACCESS_TOKEN but fall back to DUFFEL_TEST_ACCESS_TOKEN if present
DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN") or os.getenv("DUFFEL_TEST_ACCESS_TOKEN")
duffel = Duffel(access_token=DUFFEL_ACCESS_TOKEN) if DUFFEL_ACCESS_TOKEN else None


# ------------- Helpers ------------- #

# Map whatever the frontend sends into valid Duffel cabin values
CABIN_MAP = {
    "economy": "economy",
    "eco": "economy",
    "coach": "economy",
    "business": "business",
    "business class": "business",
    "biz": "business",
    "first": "first",
    "first class": "first",
    "premium economy": "premium_economy",
    "premium_economy": "premium_economy",
    "premium econom": "premium_economy",
    "prem eco": "premium_economy",
    "prem econ": "premium_economy",
}


def normalise_cabin(raw_cabin: str) -> str:
    key = (raw_cabin or "").strip().lower()
    return CABIN_MAP.get(key, "business")


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


def generate_date_pairs(params: SearchParams, max_pairs: int = 60):
    """
    Generate departure and return date pairs based on user selected stay length.
    No dummy logic, pure real date generation.
    """
    pairs: List[tuple[date, date]] = []

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


def map_duffel_offer_to_option(offer, dep: date, ret: date, index: int) -> FlightOption:
    """
    Map a Duffel offer to the FlightOption model, including enough detail
    for the frontend to show real airports and times.
    """
    price = float(offer.total_amount)
    currency = offer.total_currency

    # Outbound slice is the first slice
    first_slice = offer.slices[0]
    segments = first_slice.segments
    stops = max(0, len(segments) - 1)

    origin_airport = None
    destination_airport = None
    departure_time_str = None
    arrival_time_str = None
    duration_minutes = 0

    if segments:
        first_seg = segments[0]
        last_seg = segments[-1]

        try:
            origin_airport = first_seg.origin.iata_code
        except Exception:
            origin_airport = None

        try:
            destination_airport = last_seg.destination.iata_code
        except Exception:
            destination_airport = None

        departure_time_str = getattr(first_seg, "departing_at", None)
        arrival_time_str = getattr(last_seg, "arriving_at", None)

        if departure_time_str and arrival_time_str:
            try:
                dep_dt = datetime.fromisoformat(departure_time_str.replace("Z", "+00:00"))
                arr_dt = datetime.fromisoformat(arrival_time_str.replace("Z", "+00:00"))
                duration_minutes = int((arr_dt - dep_dt).total_seconds() // 60)
            except Exception:
                duration_minutes = 0

    airline_code = offer.owner.iata_code
    airline_name = AIRLINE_NAMES.get(airline_code, offer.owner.name)
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    return FlightOption(
        id=offer.id,
        airline=airline_name,
        airlineCode=airline_code,
        price=price,
        currency=currency,
        departureDate=dep.isoformat(),
        returnDate=ret.isoformat(),
        stops=stops,
        durationMinutes=duration_minutes,
        totalDurationMinutes=duration_minutes,
        duration=None,
        bookingUrl=booking_url,
        url=booking_url,
        originAirport=origin_airport,
        destinationAirport=destination_airport,
        departureTime=departure_time_str,
        arrivalTime=arrival_time_str,
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


# ------------- Routes ------------- #

@app.get("/")
def home():
    return {"message": "Flyyv backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search-business")
def search_business(params: SearchParams):
    """
    Main search endpoint used by the Base44 frontend.

    Behaviour:
    - Generate date pairs inside the window.
    - For each pair, call Duffel for a round trip.
    - Collect real offers only.
    - If there are no offers, return status no_results.
    """

    if duffel is None:
        return {
            "status": "error",
            "message": "Duffel not configured",
            "options": [],
        }

    try:
        all_options: List[FlightOption] = []

        date_pairs = generate_date_pairs(params, max_pairs=60)

        if not date_pairs:
            return {
                "status": "no_results",
                "message": "No valid date combinations",
                "options": [],
            }

        cabin_value = normalise_cabin(params.cabin)

        for dep, ret in date_pairs:
            body = {
                "slices": [
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
                ],
                "passengers": [{"type": "adult"} for _ in range(params.passengers)],
                "cabin_class": cabin_value,
            }

            try:
                offer_request = duffel.offer_requests.create(body)
                offers_iter = duffel.offers.list(offer_request_id=offer_request.id)

                for idx, offer in enumerate(offers_iter):
                    all_options.append(
                        map_duffel_offer_to_option(offer, dep, ret, idx)
                    )

            except Exception as e:
                print("Duffel error for", dep, ret, ":", repr(e))
                continue

        if not all_options:
            return {
                "status": "no_results",
                "message": "No flights found",
                "options": [],
            }

        filtered = apply_filters(all_options, params)

        return {
            "status": "ok",
            "source": "duffel",
            "options": [o.dict() for o in filtered],
        }

    except Exception as e:
        print("Unexpected Duffel search error:", repr(e))
        return {
            "status": "error",
            "message": "Unexpected backend error",
            "options": [],
        }


# ------------- Admin credits endpoint ------------- #

USER_WALLETS: dict[str, int] = {}


@app.post("/admin/add-credits")
def admin_add_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
    received = (x_admin_token or "").strip()
    expected = (os.getenv("ADMIN_API_TOKEN") or "").strip()

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
            detail="Missing credit amount. Use amount, delta, creditAmount, or value.",
        )

    current = USER_WALLETS.get(payload.userId, 0)
    new_balance = max(0, current + change_amount)
    USER_WALLETS[payload.userId] = new_balance

    return {"userId": payload.userId, "newBalance": new_balance}


# ------------- Duffel test endpoint ------------- #

@app.get("/duffel-test")
def duffel_test(
    origin: str,
    destination: str,
    departure: date,
    passengers: int = 1,
):
    """
    Simple single slice Duffel search for debugging.
    Does not create any bookings.
    """

    if duffel is None:
        return {"status": "error", "message": "Duffel not configured"}

    body = {
        "slices": [{
            "origin": origin,
            "destination": destination,
            "departure_date": departure.isoformat(),
        }],
        "passengers": [{"type": "adult"} for _ in range(passengers)],
        "cabin_class": "business",
    }

    try:
        offer_request = duffel.offer_requests.create(body)
        offers_iter = duffel.offers.list(offer_request_id=offer_request.id)
    except Exception as e:
        print("Duffel error in /duffel-test:", repr(e))
        raise HTTPException(
            status_code=500,
            detail=f"Duffel API error: {e}",
        )

    results = []
    for offer in offers_iter:
        results.append({
            "id": offer.id,
            "airline": offer.owner.name,
            "airlineCode": offer.owner.iata_code,
            "price": float(offer.total_amount),
            "currency": offer.total_currency,
        })

    return {"status": "ok", "source": "duffel", "offers": results}
