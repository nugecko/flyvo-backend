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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------- Duffel client ------------- #

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
duffel = Duffel(access_token=DUFFEL_ACCESS_TOKEN) if DUFFEL_ACCESS_TOKEN else None


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


def generate_date_pairs(params: SearchParams, max_pairs: int = 60):
    """
    Generate departure and return date pairs based on user-selected stay length.
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
    price = float(offer.total_amount)
    currency = offer.total_currency

    first_slice = offer.slices[0]
    segments = first_slice.segments
    stops = len(segments) - 1

    try:
        first_seg = segments[0]
        last_seg = segments[-1]
        dep_dt = datetime.fromisoformat(first_seg.departing_at.replace("Z", "+00:00"))
        arr_dt = datetime.fromisoformat(last_seg.arriving_at.replace("Z", "+00:00"))
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
    )


def apply_filters(options: List[FlightOption], params: SearchParams) -> List[FlightOption]:
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
    Main search endpoint.
    No dummy data, real date pairs, real Duffel results.
    If zero offers are found, return status no_results.
    """

    if duffel is None:
        return {"status": "error", "message": "Duffel not configured", "options": []}

    try:
        all_options: List[FlightOption] = []

        date_pairs = generate_date_pairs(params, max_pairs=60)

        if not date_pairs:
            return {"status": "no_results", "message": "No valid date combinations", "options": []}

        for dep, ret in date_pairs:
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

            try:
                offer_request = duffel.offer_requests.create(
                    slices=slices,
                    cabin_class=params.cabin.lower(),
                    passengers=passengers,
                )

                offers_iter = duffel.offers.list(offer_request_id=offer_request.id)

                for idx, offer in enumerate(offers_iter):
                    all_options.append(map_duffel_offer_to_option(offer, dep, ret, idx))

            except Exception as e:
                print("Duffel error for", dep, ret, ":", e)
                continue

        if not all_options:
            return {"status": "no_results", "message": "No flights found", "options": []}

        filtered = apply_filters(all_options, params)

        return {"status": "ok", "source": "duffel", "options": [o.dict() for o in filtered]}

    except Exception as e:
        print("Unexpected Duffel search error:", e)
        return {"status": "error", "message": "Unexpected backend error", "options": []}


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
    if duffel is None:
        return {"status": "error", "message": "Duffel not configured"}

    slices = [{
        "origin": origin,
        "destination": destination,
        "departure_date": departure.isoformat(),
    }]
    pax = [{"type": "adult"} for _ in range(passengers)]

    try:
        offer_request = duffel.offer_requests.create(
            slices=slices,
            cabin_class="business",
            passengers=pax,
        )
        offers_iter = duffel.offers.list(offer_request_id=offer_request.id)
    except Exception as e:
        print("Duffel error:", e)
        raise HTTPException(status_code=500, detail="Duffel API error")

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
