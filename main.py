import os

from datetime import date, datetime, timedelta
from enum import Enum
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import engine, Base, SessionLocal
import models  # noqa: F401
from models import AdminConfig

# =======================================
# SECTION: AIRLINES IMPORTS
# =======================================

# Robust import from airlines so a naming mismatch does not crash the app
try:
    from airlines import AIRLINE_NAMES  # type: ignore
except ImportError:
    AIRLINE_NAMES: Dict[str, str] = {}

try:
    # Prefer singular
    from airlines import AIRLINE_BOOKING_URL  # type: ignore
except ImportError:
    try:
        # Fall back to plural if that is what airlines.py uses
        from airlines import AIRLINE_BOOKING_URLS as AIRLINE_BOOKING_URL  # type: ignore
    except ImportError:
        AIRLINE_BOOKING_URL: Dict[str, str] = {}

# ===== END SECTION: AIRLINES IMPORTS =====


# =======================================
# SECTION: ADMIN CONFIG HELPERS
# =======================================

def _get_config_row(db: Session, key: str) -> Optional[AdminConfig]:
    return db.query(AdminConfig).filter(AdminConfig.key == key).first()


def get_config_str(key: str, default_value: Optional[str] = None) -> Optional[str]:
    """
    Read a config value from admin_config as string.
    If the key is missing or value is null, return default_value.
    """
    db = SessionLocal()
    try:
        row = _get_config_row(db, key)
        if not row or row.value is None:
            return default_value
        return str(row.value)
    finally:
        db.close()


def get_config_int(key: str, default_value: int) -> int:
    """
    Read a config value from admin_config and cast to int.
    Falls back to default_value if missing or invalid.
    """
    raw = get_config_str(key, None)
    if raw is None:
        return default_value
    try:
        return int(raw)
    except ValueError:
        return default_value

# ===== END SECTION: ADMIN CONFIG HELPERS =====


# =======================================
# SECTION: Pydantic MODELS
# =======================================

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
    # Optional filter for number of stops, for example [0, 1, 2]
    # 3 is treated as "3 or more stops"
    stopsFilter: Optional[List[int]] = None

    # Tuning parameters coming from the frontend
    maxOffersPerPair: int = 50
    maxOffersTotal: int = 5000
    maxDatePairs: int = 45

    # Hint to use async full coverage mode
    fullCoverage: bool = True


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
    origin: Optional[str] = None              # origin IATA code
    destination: Optional[str] = None         # destination IATA code
    originAirport: Optional[str] = None       # full origin airport name
    destinationAirport: Optional[str] = None  # full destination airport name

    # Stopover info for display
    stopoverCodes: Optional[List[str]] = None
    stopoverAirports: Optional[List[str]] = None

    # Detailed segment info for stopover display
    segments: Optional[List[Dict[str, Any]]] = None

    # Aircraft information, optional
    aircraftCodes: Optional[List[str]] = None
    aircraftNames: Optional[List[str]] = None

    bookingUrl: Optional[str] = None
    url: Optional[str] = None


class CreditUpdateRequest(BaseModel):
    userId: str
    amount: Optional[int] = None
    delta: Optional[int] = None
    creditAmount: Optional[int] = None
    value: Optional[int] = None
    reason: Optional[str] = None


# Job models for async search

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SearchJob(BaseModel):
    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    params: SearchParams
    total_pairs: int = 0
    processed_pairs: int = 0
    error: Optional[str] = None


class SearchStatusResponse(BaseModel):
    jobId: str
    status: JobStatus
    processedPairs: int
    totalPairs: int
    progress: float
    error: Optional[str] = None
    previewCount: int = 0
    previewOptions: List[FlightOption] = Field(default_factory=list)


class SearchResultsResponse(BaseModel):
    jobId: str
    status: JobStatus
    totalResults: int
    offset: int
    limit: int
    options: List[FlightOption]

# ===== END SECTION: Pydantic MODELS =====


# =======================================
# SECTION: FastAPI APP AND CORS
# =======================================

app = FastAPI()


@app.on_event("startup")
def on_startup():
    # Create all database tables automatically
    Base.metadata.create_all(bind=engine)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # later you can restrict this to your Base44 domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== END SECTION: FastAPI APP AND CORS =====


# =======================================
# SECTION: ENV AND DUFFEL CONFIG
# =======================================

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

if not DUFFEL_ACCESS_TOKEN:
    print("WARNING: DUFFEL_ACCESS_TOKEN is not set, searches will fail")

# Limits and defaults coming from admin_config
CONFIG_MAX_OFFERS_TOTAL = get_config_int("MAX_OFFERS_TOTAL", 5000)
CONFIG_MAX_OFFERS_PER_PAIR = get_config_int("MAX_OFFERS_PER_PAIR", 50)
MAX_PASSENGERS = get_config_int("MAX_PASSENGERS", 4)
DEFAULT_CABIN = get_config_str("DEFAULT_CABIN", "BUSINESS") or "BUSINESS"
SEARCH_MODE = get_config_str("SEARCH_MODE", "AUTO") or "AUTO"

# Hard safety caps, independent from frontend values
# Control panel values are further clamped by these to protect the container
MAX_OFFERS_PER_PAIR_HARD = 30       # max offers per date pair
MAX_OFFERS_TOTAL_HARD = 1000        # total offers across all pairs
MAX_DATE_PAIRS_HARD = 20            # total date pairs scanned per job

# Below this number of date pairs, we run synchronously
SYNC_PAIR_THRESHOLD = 10

# How many date pairs to process in parallel inside async jobs
PARALLEL_WORKERS = 2

# ===== END SECTION: ENV AND DUFFEL CONFIG =====


# =======================================
# SECTION: IN MEMORY STORES
# =======================================

# Wallets for admin credits endpoint
USER_WALLETS: Dict[str, int] = {}

# Async search jobs and results
JOBS: Dict[str, SearchJob] = {}
JOB_RESULTS: Dict[str, List[FlightOption]] = {}

# ===== END SECTION: IN MEMORY STORES =====


# =======================================
# SECTION: DUFFEL HELPERS
# =======================================

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

    Special case:
    If earliestDeparture equals latestDeparture and minStayDays equals maxStayDays,
    treat it as a single fixed trip and return exactly one pair.

    This matches the One off payload from Base44, which sends:
    earliestDeparture = chosen outbound date
    latestDeparture = same date
    minStayDays = chosen number of nights
    maxStayDays = same as minStayDays
    fullCoverage = false
    maxDatePairs = 1
    """
    pairs: List[Tuple[date, date]] = []

    min_stay = max(1, params.minStayDays)
    max_stay = max(min_stay, params.maxStayDays)

    # One off fixed trip: earliest equals latest and single stay length
    if params.earliestDeparture == params.latestDeparture and min_stay == max_stay:
        dep = params.earliestDeparture
        ret = dep + timedelta(days=min_stay)
        pairs.append((dep, ret))
        return pairs[:max_pairs]

    # Normal flexible window behaviour for Smart mode
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

    resp = requests.get(url, params=params, headers=duffel_headers(), timeout=15)
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
    airline_name = AIRLINE_NAMES.get(
        airline_code,
        owner.get("name", airline_code or "Airline"),
    )
    booking_url = AIRLINE_BOOKING_URL.get(airline_code) if isinstance(AIRLINE_BOOKING_URL, dict) else None

    slices = offer.get("slices", []) or []
    outbound_segments: List[dict] = []
    return_segments: List[dict] = []

    if len(slices) >= 1:
        outbound_segments = slices[0].get("segments", []) or []
    if len(slices) >= 2:
        return_segments = slices[1].get("segments", []) or []

    stops_outbound = max(0, len(outbound_segments) - 1)

    # Duration and airport info, based on the outbound slice
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

    # Stopover codes and airports for summary, outbound only
    stopover_codes: List[str] = []
    stopover_airports: List[str] = []
    if len(outbound_segments) > 1:
        for seg in outbound_segments[:-1]:
            dest_obj = seg.get("destination", {}) or {}
            code = dest_obj.get("iata_code")
            name = dest_obj.get("name")
            if code:
                stopover_codes.append(code)
            if name:
                stopover_airports.append(name)

    # Build segments list for detailed display, including both outbound and return,
    # plus aircraft and layover minutes
    segments_info: List[Dict[str, Any]] = []
    aircraft_codes: List[str] = []
    aircraft_names: List[str] = []

    for direction, seg_list in (("outbound", outbound_segments), ("return", return_segments)):
        for idx, seg in enumerate(seg_list):
            o = seg.get("origin", {}) or {}
            d = seg.get("destination", {}) or {}
            aircraft = seg.get("aircraft", {}) or {}

            aircraft_code = aircraft.get("iata_code")
            aircraft_name = aircraft.get("name")

            if aircraft_code:
                aircraft_codes.append(aircraft_code)
            if aircraft_name:
                aircraft_names.append(aircraft_name)

            # Compute layover until the next segment within this slice
            layover_minutes_to_next: Optional[int] = None
            if idx < len(seg_list) - 1:
                this_arr = seg.get("arriving_at")
                next_dep = seg_list[idx + 1].get("departing_at")
                try:
                    this_arr_dt = datetime.fromisoformat(this_arr.replace("Z", "+00:00"))
                    next_dep_dt = datetime.fromisoformat(next_dep.replace("Z", "+00:00"))
                    layover_minutes_to_next = int((next_dep_dt - this_arr_dt).total_seconds() // 60)
                except Exception:
                    layover_minutes_to_next = None

            segments_info.append(
                {
                    "direction": direction,  # "outbound" or "return"
                    "origin": o.get("iata_code"),
                    "originAirport": o.get("name"),
                    "destination": d.get("iata_code"),
                    "destinationAirport": d.get("name"),
                    "departingAt": seg.get("departing_at"),
                    "arrivingAt": seg.get("arriving_at"),
                    "aircraftCode": aircraft_code,
                    "aircraftName": aircraft_name,
                    "layoverMinutesToNext": layover_minutes_to_next,
                }
            )

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
        stopoverCodes=stopover_codes or None,
        stopoverAirports=stopover_airports or None,
        segments=segments_info or None,
        aircraftCodes=aircraft_codes or None,
        aircraftNames=aircraft_names or None,
        bookingUrl=booking_url,
        url=booking_url,
    )

# ===== END SECTION: DUFFEL HELPERS =====


# =======================================
# SECTION: FILTERING AND BALANCING
# =======================================

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


def balance_airlines(options: List[FlightOption], max_per_airline: int = 50) -> List[FlightOption]:
    """
    Limit how many results each airline can dominate.
    """
    buckets: Dict[str, List[FlightOption]] = {}
    for opt in options:
        key = opt.airlineCode or opt.airline
        buckets.setdefault(key, []).append(opt)

    trimmed: List[FlightOption] = []
    for key, bucket in buckets.items():
        trimmed.extend(bucket[:max_per_airline])

    trimmed.sort(key=lambda x: x.price)
    return trimmed

# ===== END SECTION: FILTERING AND BALANCING =====


# =======================================
# SECTION: SHARED SEARCH HELPERS
# =======================================

def effective_caps(params: SearchParams) -> Tuple[int, int, int]:
    """
    Compute effective caps for this search, combining:
    - frontend hints (maxOffersPerPair, maxOffersTotal, maxDatePairs)
    - Directus config values
    - hard coded safety caps
    """
    # Max date pairs is only clamped by hard cap for now
    max_pairs = max(1, min(params.maxDatePairs, MAX_DATE_PAIRS_HARD))

    # Combine frontend request, Directus config and hard caps
    requested_per_pair = max(1, params.maxOffersPerPair)
    requested_total = max(1, params.maxOffersTotal)

    max_offers_pair = max(
        1,
        min(requested_per_pair, CONFIG_MAX_OFFERS_PER_PAIR, MAX_OFFERS_PER_PAIR_HARD),
    )
    max_offers_total = max(
        1,
        min(requested_total, CONFIG_MAX_OFFERS_TOTAL, MAX_OFFERS_TOTAL_HARD),
    )

    return max_pairs, max_offers_pair, max_offers_total


def estimate_date_pairs(params: SearchParams) -> int:
    max_pairs, _, _ = effective_caps(params)
    pairs = generate_date_pairs(params, max_pairs=max_pairs)
    return len(pairs)


def run_duffel_scan(params: SearchParams) -> List[FlightOption]:
    """
    Synchronous scan used for small searches.
    """
    max_pairs, max_offers_pair, max_offers_total = effective_caps(params)
    date_pairs = generate_date_pairs(params, max_pairs=max_pairs)
    if not date_pairs:
        return []

    collected_offers: List[Tuple[dict, date, date]] = []
    total_count = 0

    for dep, ret in date_pairs:
        if total_count >= max_offers_total:
            break

        slices = [
            {"origin": params.origin, "destination": params.destination, "departure_date": dep.isoformat()},
            {"origin": params.destination, "destination": params.origin, "departure_date": ret.isoformat()},
        ]
        pax = [{"type": "adult"} for _ in range(params.passengers)]

        try:
            offer_request = duffel_create_offer_request(slices, pax, params.cabin)
            offer_request_id = offer_request.get("id")
            if not offer_request_id:
                continue

            per_pair_limit = min(max_offers_pair, max_offers_total - total_count)
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

    mapped: List[FlightOption] = [
        map_duffel_offer_to_option(offer, dep, ret)
        for offer, dep, ret in collected_offers
    ]

    filtered = apply_filters(mapped, params)
    balanced = balance_airlines(filtered, max_per_airline=50)
    return balanced

# ===== END SECTION: SHARED SEARCH HELPERS =====


# =======================================
# SECTION: ASYNC JOB RUNNER
# =======================================

def process_date_pair_offers(
    params: SearchParams,
    dep: date,
    ret: date,
    max_offers_pair: int,
) -> List[FlightOption]:
    """
    Helper for async job runner.
    Performs Duffel calls for a single date pair and maps results to FlightOption objects.
    """
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
            print(f"[PAIR {dep} -> {ret}] No offer_request id")
            return []

        offers_json = duffel_list_offers(offer_request_id, limit=max_offers_pair)
    except HTTPException as e:
        print(f"[PAIR {dep} -> {ret}] Duffel HTTPException: {e.detail}")
        return []
    except Exception as e:
        print(f"[PAIR {dep} -> {ret}] Unexpected Duffel error: {e}")
        return []

    batch_mapped: List[FlightOption] = [
        map_duffel_offer_to_option(offer, dep, ret) for offer in offers_json
    ]
    return batch_mapped


def run_search_job(job_id: str):
    """
    Background job that performs the Duffel scan and updates progress.

    This version:
    - Processes date pairs in parallel batches inside a ThreadPoolExecutor
    - Updates processed_pairs as each pair completes
    - Maintains JOB_RESULTS[job_id] as filtered and balanced partial results
      so that /search-status and /search-results can return data while still running
    """
    job = JOBS.get(job_id)
    if not job:
        print(f"[JOB {job_id}] Job not found in memory")
        return

    job.status = JobStatus.RUNNING
    job.updated_at = datetime.utcnow()
    JOBS[job_id] = job
    print(f"[JOB {job_id}] Starting async search")

    # Ensure there is a list to hold results
    if job_id not in JOB_RESULTS:
        JOB_RESULTS[job_id] = []

    try:
        max_pairs, max_offers_pair, max_offers_total = effective_caps(job.params)
        date_pairs = generate_date_pairs(job.params, max_pairs=max_pairs)
        total_pairs = len(date_pairs)
        job.total_pairs = total_pairs
        job.processed_pairs = 0
        job.updated_at = datetime.utcnow()
        JOBS[job_id] = job
        print(
            f"[JOB {job_id}] total_pairs={total_pairs}, "
            f"max_offers_pair={max_offers_pair}, max_offers_total={max_offers_total}"
        )

        total_count = 0

        if total_pairs == 0:
            job.status = JobStatus.COMPLETED
            job.updated_at = datetime.utcnow()
            JOBS[job_id] = job
            print(f"[JOB {job_id}] No date pairs, completed with 0 options")
            return

        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            # Process date pairs in batches to limit concurrency
            for batch_start in range(0, total_pairs, PARALLEL_WORKERS):
                if total_count >= max_offers_total:
                    print(f"[JOB {job_id}] Reached max_offers_total before batch, stopping")
                    break

                batch_pairs = date_pairs[batch_start: batch_start + PARALLEL_WORKERS]
                futures = {
                    executor.submit(
                        process_date_pair_offers,
                        job.params,
                        dep,
                        ret,
                        max_offers_pair,
                    ): (dep, ret)
                    for dep, ret in batch_pairs
                }

                for future in as_completed(futures):
                    dep, ret = futures[future]

                    # Mark one more pair as processed for progress bar
                    job.processed_pairs += 1
                    job.updated_at = datetime.utcnow()
                    JOBS[job_id] = job

                    print(
                        f"[JOB {job_id}] processed pair {job.processed_pairs}/{total_pairs}: "
                        f"{dep} -> {ret}, current_results={total_count}"
                    )

                    try:
                        batch_mapped = future.result()
                    except Exception as e:
                        print(f"[JOB {job_id}] Future error for pair {dep} -> {ret}: {e}")
                        continue

                    if not batch_mapped:
                        continue

                    # Combine with existing partial results
                    existing = JOB_RESULTS.get(job_id, [])
                    combined = existing + batch_mapped

                    # Apply filters and balancing
                    filtered = apply_filters(combined, job.params)
                    balanced = balance_airlines(filtered, max_per_airline=50)

                    # Enforce total cap on number of results stored
                    if len(balanced) > max_offers_total:
                        balanced = balanced[:max_offers_total]

                    JOB_RESULTS[job_id] = balanced
                    total_count = len(balanced)

                    print(f"[JOB {job_id}] partial results updated, count={total_count}")

                    if total_count >= max_offers_total:
                        print(f"[JOB {job_id}] Reached max_offers_total={max_offers_total}, stopping")
                        break

                if total_count >= max_offers_total:
                    break

        final_results = JOB_RESULTS.get(job_id, [])

        job.status = JobStatus.COMPLETED
        job.updated_at = datetime.utcnow()
        JOBS[job_id] = job
        print(f"[JOB {job_id}] Completed with {len(final_results)} options")

    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.updated_at = datetime.utcnow()
        JOBS[job_id] = job
        print(f"[JOB {job_id}] FAILED: {e}")

# ===== END SECTION: ASYNC JOB RUNNER =====


# =======================================
# SECTION: ROOT AND HEALTH ROUTES
# =======================================

@app.get("/")
def home():
    return {"message": "Flyyv backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}

# ===== END SECTION: ROOT AND HEALTH ROUTES =====


# =======================================
# SECTION: MAIN SEARCH ROUTES
# =======================================

from uuid import uuid4  # keep import close to usage to avoid clutter


@app.post("/search-business")
def search_business(params: SearchParams, background_tasks: BackgroundTasks):
    """
    Main endpoint used by the Base44 frontend.

    Behaviour:
    - For small searches with few date pairs run synchronously and return results.
    - For large searches or fullCoverage True create an async job and return jobId.
    """
    if not DUFFEL_ACCESS_TOKEN:
        return {
            "status": "error",
            "source": "duffel_not_configured",
            "options": [],
        }

    # Enforce backend controlled passenger and cabin defaults
    if params.passengers > MAX_PASSENGERS:
        params.passengers = MAX_PASSENGERS

    if not params.cabin:
        params.cabin = DEFAULT_CABIN

    estimated_pairs = estimate_date_pairs(params)

    # Use SEARCH_MODE from Directus to optionally force sync or async
    mode = (SEARCH_MODE or "AUTO").upper()
    if mode == "SYNC":
        use_async = False
    elif mode == "ASYNC":
        use_async = True
    else:
        use_async = params.fullCoverage or estimated_pairs > SYNC_PAIR_THRESHOLD

    if not use_async:
        # Small search, do it inline
        options = run_duffel_scan(params)
        return {
            "status": "ok",
            "mode": "sync",
            "source": "duffel",
            "options": [o.dict() for o in options],
        }

    # Large search, create async job
    job_id = str(uuid4())
    job = SearchJob(
        id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        params=params,
        total_pairs=0,
        processed_pairs=0,
    )
    JOBS[job_id] = job

    # Initialise empty results so status and results endpoints can return partial data
    JOB_RESULTS[job_id] = []

    # Start background job
    background_tasks.add_task(run_search_job, job_id)

    return {
        "status": "ok",
        "mode": "async",
        "jobId": job_id,
        "message": "Search started",
    }


@app.get("/search-status/{job_id}", response_model=SearchStatusResponse)
def get_search_status(job_id: str, preview_limit: int = 20):
    """
    Return job status, progress and optional preview of results.
    Used for the scanning progress bar.
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    options = JOB_RESULTS.get(job_id, [])
    if preview_limit > 0:
        preview = options[:preview_limit]
    else:
        preview = []

    total_pairs = job.total_pairs or 0
    processed_pairs = job.processed_pairs or 0
    progress = float(processed_pairs) / float(total_pairs) if total_pairs > 0 else 0.0

    return SearchStatusResponse(
        jobId=job_id,
        status=job.status,
        processedPairs=processed_pairs,
        totalPairs=total_pairs,
        progress=progress,
        error=job.error,
        previewCount=len(preview),
        previewOptions=preview,
    )


@app.get("/search-results/{job_id}", response_model=SearchResultsResponse)
def get_search_results(job_id: str, offset: int = 0, limit: int = 50):
    """
    Return a slice of results for a job.
    Powers load more results on the frontend and can serve partial data
    while the job is still running.
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    options = JOB_RESULTS.get(job_id, [])

    offset = max(0, offset)
    limit = max(1, min(limit, 200))

    end = min(offset + limit, len(options))
    slice_ = options[offset:end]

    return SearchResultsResponse(
        jobId=job_id,
        status=job.status,
        totalResults=len(options),
        offset=offset,
        limit=limit,
        options=slice_,
    )

# ===== END SECTION: MAIN SEARCH ROUTES =====


# =======================================
# SECTION: ADMIN CREDITS ENDPOINT
# =======================================

@app.post("/admin/add-credits")
def admin_add_credits(
    payload: CreditUpdateRequest,
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
):
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

# ===== END SECTION: ADMIN CREDITS ENDPOINT =====


# =======================================
# SECTION: DUFFEL TEST ENDPOINT
# =======================================

@app.get("/duffel-test")
def duffel_test(
    origin: str,
    destination: str,
    departure: date,
    passengers: int = 1,
):
    """
    Simple test endpoint for Duffel search.
    Uses whatever DUFFEL_ACCESS_TOKEN is configured, test or live.
    No bookings are created.
    """
    if not DUFFEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Duffel not configured")

    if passengers > MAX_PASSENGERS:
        passengers = MAX_PASSENGERS

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

# ===== END SECTION: DUFFEL TEST ENDPOINT =====
