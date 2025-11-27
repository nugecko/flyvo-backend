import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple, Dict
from uuid import uuid4

import requests
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
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

    # Tuning fields, supplied by Base44
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


# Job tracking models

class SearchJobProgress(BaseModel):
    jobId: str
    status: str
    donePairs: int
    totalPairs: int
    totalResults: int
    error: Optional[str] = None


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


# ------------- Helpers ------------- #

def duffel_headers() -> dict:
    return {
        "Authorization": f"Bearer {DUFFEL_ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Duffel-Version": DUFFEL_VERSION,
    }


def generate_date_pairs(params: SearchParams, max_pairs: int = 60) -> List[Tuple[date, date]]:
    """
    Generate (departure, return) pairs across the window,
    respecting minStayDays and maxStayDays.
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

    resp = requests.post(url, json=payload, headers=duffel_headers(), timeout=30)
    if resp.status_code >= 400:
        print("Duffel offer_requests error:", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Duffel API error")

    body = resp.json()
    return body.get("data", {})


def duffel_list_offers(offer_request_id: str, limit: int = 300) -> List[dict]:
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

    resp = requests.get(url, params=params, headers=duffel_headers(), timeout=30)
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


def balance_airlines(options: List[FlightOption], max_total: int) -> List[FlightOption]:
    """
    Limit dominance by any single airline while keeping cheapest options.
    """
    if not options:
        return []

    groups: Dict[str, List[FlightOption]] = defaultdict(list)
    for o in options:
        key = o.airlineCode or o.airline or "Unknown"
        groups[key].append(o)

    # Sort each airline group by price
    for flights in groups.values():
        flights.sort(key=lambda x: x.price)

    num_airlines = len(groups)
    if num_airlines == 0:
        return options[:max_total]

    base_cap = max_total // num_airlines if num_airlines else max_total
    soft_cap = min(200, max(20, base_cap + 5))

    selected: List[FlightOption] = []
    per_airline_count: Dict[str, int] = {k: 0 for k in groups.keys()}

    # Round robin selection with per airline caps
    while len(selected) < max_total:
        progressed = False
        for key, flights in groups.items():
            if not flights:
                continue
            if per_airline_count[key] >= soft_cap:
                continue
            selected.append(flights.pop(0))
            per_airline_count[key] += 1
            progressed = True
            if len(selected) >= max_total:
                break
        if not progressed:
            break

    # If we still have space, fill with any remaining cheapest flights
    if len(selected) < max_total:
        remaining: List[FlightOption] = []
        for flights in groups.values():
            remaining.extend(flights)
        remaining.sort(key=lambda x: x.price)
        for f in remaining:
            if len(selected) >= max_total:
                break
            selected.append(f)

    selected.sort(key=lambda x: x.price)
    return selected[:max_total]


# Shared core search logic so sync and async paths stay in step

def run_search_core(params: SearchParams, progress_hook=None):
    """
    Core scanning logic.
    progress_hook, if given, is called as progress_hook(done_pairs, total_pairs, collected_count)
    """

    if not DUFFEL_ACCESS_TOKEN:
        return {
            "status": "error",
            "source": "duffel_not_configured",
            "options": [],
        }

    # Hard safety caps on the server
    HARD_MAX_OFFERS_PER_PAIR = 300
    HARD_MAX_OFFERS_TOTAL = 15000
    HARD_MAX_DATE_PAIRS = 60

    # Tuning values from the client, with sensible defaults
    client_max_per_pair = params.maxOffersPerPair or 50
    client_max_total = params.maxOffersTotal or 5000
    client_max_pairs = params.maxDatePairs or 20

    max_offers_per_pair = max(10, min(client_max_per_pair, HARD_MAX_OFFERS_PER_PAIR))
    max_offers_total = max(100, min(client_max_total, HARD_MAX_OFFERS_TOTAL))
    max_date_pairs = max(1, min(client_max_pairs, HARD_MAX_DATE_PAIRS))

    date_pairs = generate_date_pairs(params, max_pairs=max_date_pairs)
    if not date_pairs:
        return {
            "status": "ok",
            "source": "no_date_pairs",
            "options": [],
            "total_pairs": 0,
            "done_pairs": 0,
        }

    collected_offers: List[Tuple[dict, date, date]] = []
    total_count = 0
    total_pairs = len(date_pairs)

    for idx, (dep, ret) in enumerate(date_pairs, start=1):
        if total_count >= max_offers_total:
            break

        if progress_hook:
            progress_hook(idx - 1, total_pairs, total_count)

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

    if progress_hook:
        progress_hook(total_pairs, total_pairs, total_count)

    if not collected_offers:
        return {
            "status": "ok",
            "source": "duffel_no_results",
            "options": [],
            "total_pairs": total_pairs,
            "done_pairs": total_pairs,
        }

    mapped: List[FlightOption] = [
        map_duffel_offer_to_option(offer, dep, ret)
        for offer, dep, ret in collected_offers
    ]

    filtered = apply_filters(mapped, params)

    max_total_for_balance = min(max_offers_total, len(filtered))
    balanced = balance_airlines(filtered, max_total_for_balance)

    return {
        "status": "ok",
        "source": "duffel",
        "options": [o.dict() for o in balanced],
        "total_pairs": total_pairs,
        "done_pairs": total_pairs,
    }


# ------------- Routes: health and synchronous search ------------- #

@app.get("/")
def home():
    return {"message": "Flyyv backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search-business")
def search_business(params: SearchParams):
    """
    Synchronous endpoint used by the Base44 frontend.
    """
    result = run_search_core(params)
    return result


# ------------- Async search jobs ------------- #

# In memory job store, fine for now, can later be moved to Redis or database
JOBS: Dict[str, Dict] = {}


def _update_job_progress(job_id: str, done_pairs: int, total_pairs: int, collected: int):
    job = JOBS.get(job_id)
    if not job:
        return
    job["done_pairs"] = done_pairs
    job["total_pairs"] = total_pairs
    job["total_results"] = collected


def _search_job_runner(job_id: str, params: SearchParams):
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        job["status"] = "running"

        def hook(done_pairs, total_pairs, collected):
            _update_job_progress(job_id, done_pairs, total_pairs, collected)

        result = run_search_core(params, progress_hook=hook)
        job["status"] = "completed"
        job["result"] = result
        job["total_pairs"] = result.get("total_pairs", job.get("total_pairs", 0))
        job["done_pairs"] = result.get("done_pairs", job.get("done_pairs", 0))
        job["total_results"] = len(result.get("options", []))
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)


@app.post("/search-business-async")
def search_business_async(params: SearchParams, background_tasks: BackgroundTasks):
    """
    Start an asynchronous search job and return a job id.
    """
    job_id = str(uuid4())
    JOBS[job_id] = {
        "status": "pending",
        "total_pairs": 0,
        "done_pairs": 0,
        "total_results": 0,
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_search_job_runner, job_id, params)
    return {"job_id": job_id, "status": "accepted"}


@app.get("/search-status/{job_id}")
def search_status(job_id: str, offset: int = 0, limit: int = 50):
    """
    Poll endpoint for job status and to fetch result slices.

    - While status is pending or running, returns only progress and no results.
    - Once status is completed, returns a slice of the balanced options,
      controlled by offset and limit, suitable for "load more".
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job.get("status", "unknown")
    total_pairs = job.get("total_pairs", 0)
    done_pairs = job.get("done_pairs", 0)
    total_results = job.get("total_results", 0)
    error = job.get("error")

    result_slice = []

    if status == "completed" and job.get("result"):
        options = job["result"].get("options", [])
        if options:
            start = max(0, offset)
            max_limit = max(1, min(limit, 200))
            end = start + max_limit
            result_slice = options[start:end]

    response = {
        "job_id": job_id,
        "status": status,
        "progress": {
            "donePairs": done_pairs,
            "totalPairs": total_pairs,
            "totalResults": total_results,
        },
        "results": result_slice,
        "error": error,
    }
    return response


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
    Uses whatever DUFFEL_ACCESS_TOKEN is configured.
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
