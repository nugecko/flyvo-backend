import os
import re
from datetime import date, timedelta, datetime
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from amadeus import Client, ResponseError
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


class DuffelPassenger(BaseModel):
    id: str  # passenger id from Duffel offer or offer_request
    phone_number: str
    email: str
    born_on: date
    title: str
    gender: str
    family_name: str
    given_name: str
    infant_passenger_id: Optional[str] = None


class CreateTestOrderRequest(BaseModel):
    offer_id: str
    passengers: List[DuffelPassenger]


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
# Amadeus is still configured but no longer used in /search-business

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


# ------------- Duffel clients ------------- #

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
DUFFEL_TEST_ACCESS_TOKEN = os.getenv("DUFFEL_TEST_ACCESS_TOKEN")
DUFFEL_ALLOW_LIVE_ORDERS = os.getenv("DUFFEL_ALLOW_LIVE_ORDERS", "false").lower() == "true"

duffel = Duffel(access_token=DUFFEL_ACCESS_TOKEN) if DUFFEL_ACCESS_TOKEN else None
duffel_test = Duffel(access_token=DUFFEL_TEST_ACCESS_TOKEN) if DUFFEL_TEST_ACCESS_TOKEN else None


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
    # Kept for possible future Amadeus use
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


def map_duffel_offer_to_option(offer, dep: date, ret: date, index: int) -> FlightOption:
    """
    Map a Duffel offer to your FlightOption model.
    """
    price = float(offer.total_amount)
    currency = offer.total_currency

    # Outbound slice is first
    first_slice = offer.slices[0]
    outbound_segments = first_slice.segments
    stops_outbound = len(outbound_segments) - 1

    # Try to compute a rough total duration for outbound
    try:
        first_segment = outbound_segments[0]
        last_segment = outbound_segments[-1]
        dep_dt = datetime.fromisoformat(first_segment.departing_at)
        arr_dt = datetime.fromisoformat(last_segment.arriving_at)
        duration_minutes = int((arr_dt - dep_dt).total_seconds() // 60)
    except Exception:
        duration_minutes = 0

    airline_code = offer.owner.iata_code
    airline_name = AIRLINE_NAMES.get(airline_code, offer.owner.name)
    booking_url = AIRLINE_BOOKING_URLS.get(airline_code)

    return FlightOption(
        id=offer.id,  # very important for booking later
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


def generate_date_pairs(params: SearchParams, max_pairs: int = 60):
    """
    Generate (departure, return) pairs for all valid dates
    inside the window, respecting minStayDays and maxStayDays.
    """
    pairs: List[tuple[date, date]] = []

    # Normalise stay range
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


def apply_filters(options: List[FlightOption], params: SearchParams) -> List[FlightOption]:
    """
    Apply price and stops filters, then sort by price.
    """
    filtered = list(options)

    # Price filter
    if params.maxPrice is not None and params.maxPrice > 0:
        filtered = [o for o in filtered if o.price <= params.maxPrice]

    # Stops filter, values like [0], [0, 1], [0, 1, 2], [2, 3] etc
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
    - For each pair, call Duffel for a round trip (two slices).
    - Map to FlightOption, then apply price and stops filters.
    """

    # If Duffel is not configured, fall back to dummy results
    if duffel is None:
        options = dummy_results(params)
        filtered = apply_filters(options, params)
        return {
            "status": "ok",
            "source": "dummy_no_duffel",
            "options": [o.dict() for o in filtered],
        }

    try:
        offers = []

        date_pairs = generate_date_pairs(params, max_pairs=60)

        # Safety fallback
        if not date_pairs:
            date_pairs = [(params.earliestDeparture, params.latestDeparture)]

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
            pax = [{"type": "adult"} for _ in range(params.passengers)]

            try:
                offer_request = duffel.offer_requests.create(
                    slices=slices,
                    cabin_class=params.cabin.lower(),
                    passengers=pax,
                )
                offers_iter = duffel.offers.list(offer_request_id=offer_request.id)
                for offer in offers_iter:
                    offers.append((offer, dep, ret))
            except Exception as e:
                print("Duffel error for", dep, "to", ret, ":", e)
                continue

        if not offers:
            options = dummy_results(params)
            filtered = apply_filters(options, params)
            return {
                "status": "ok",
                "source": "duffel_no_results_fallback_dummy",
                "options": [o.dict() for o in filtered],
            }

        mapped: List[FlightOption] = []
        for idx, (offer, dep, ret) in enumerate(offers):
            mapped.append(map_duffel_offer_to_option(offer, dep, ret, idx))

        filtered = apply_filters(mapped, params)

        return {
            "status": "ok",
            "source": "duffel",
            "options": [o.dict() for o in filtered],
        }

    except Exception as e:
        print("Unexpected error in search_business with Duffel:", e)
        options = dummy_results(params)
        filtered = apply_filters(options, params)
        return {
            "status": "ok",
            "source": "dummy_on_error",
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


# ------------- Duffel test booking endpoint ------------- #

@app.post("/duffel/create-test-order")
def create_duffel_test_order(payload: CreateTestOrderRequest):
    """
    Creates a Duffel order in TEST mode only.
    Uses DUFFEL_TEST_ACCESS_TOKEN, never live.
    """

    if duffel_test is None:
        raise HTTPException(status_code=500, detail="Duffel test client not configured")

    # Get the latest version of the offer from Duffel
    try:
        offer = duffel_test.offers.get(payload.offer_id)
    except Exception as e:
        print("Duffel get offer error:", e)
        raise HTTPException(status_code=400, detail="Invalid offer id or Duffel error")

    total_amount = offer.total_amount
    total_currency = offer.total_currency

    # Map passengers from your request to Duffel format
    passengers_payload = []
    for p in payload.passengers:
        passenger_data = {
            "id": p.id,
            "phone_number": p.phone_number,
            "email": p.email,
            "born_on": p.born_on.isoformat(),
            "title": p.title,
            "gender": p.gender,
            "family_name": p.family_name,
            "given_name": p.given_name,
        }
        if p.infant_passenger_id:
            passenger_data["infant_passenger_id"] = p.infant_passenger_id

        passengers_payload.append(passenger_data)

    # Create the order in Duffel TEST
    try:
        order = duffel_test.orders.create(
            selected_offers=[payload.offer_id],
            payments=[
                {
                    "type": "balance",
                    "currency": total_currency,
                    "amount": total_amount,
                }
            ],
            passengers=passengers_payload,
        )
    except Exception as e:
        print("Duffel create order error:", e)
        raise HTTPException(status_code=500, detail="Failed to create order with Duffel")

    booking_reference = getattr(order, "booking_reference", None)

    return {
        "status": "ok",
        "environment": "test",
        "order_id": order.id,
        "booking_reference": booking_reference,
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
    """

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

    return {
        "status": "ok",
        "source": "duffel",
        "offers": results,
    }
