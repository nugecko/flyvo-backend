import os
from datetime import date, datetime, timedelta
from enum import Enum
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4
from collections import defaultdict
import smtplib
from email.message import EmailMessage

import requests
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import engine, Base, SessionLocal
import models  # noqa: F401
from models import AdminConfig, AppUser

# =======================================
# SECTION: AIRLINES IMPORTS
# =======================================

try:
    from airlines import AIRLINE_NAMES  # type: ignore
except ImportError:
    AIRLINE_NAMES: Dict[str, str] = {}

try:
    from airlines import AIRLINE_BOOKING_URL  # type: ignore
except ImportError:
    try:
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
    maxPrice: Optional[float] = None
    cabin: str = "BUSINESS"
    passengers: int = 1
    stopsFilter: Optional[List[int]] = None

    # Target 50-100 offers per date pair, default via config will clamp
    maxOffersPerPair: int = 300
    maxOffersTotal: int = 10000
    maxDatePairs: int = 60

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

    origin: Optional[str] = None
    destination: Optional[str] = None
    originAirport: Optional[str] = None
    destinationAirport: Optional[str] = None

    stopoverCodes: Optional[List[str]] = None
    stopoverAirports: Optional[List[str]] = None

    segments: Optional[List[Dict[str, Any]]] = None

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


class UserSyncPayload(BaseModel):
    external_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    country: Optional[str] = None
    marketing_consent: Optional[bool] = None
    source: Optional[str] = None


class ProfileUser(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    plan: Optional[str] = None


class SubscriptionInfo(BaseModel):
    plan: str
    billingCycle: Optional[str] = None
    renewalDate: Optional[str] = None
    monthlyCredits: int = 0


class WalletInfo(BaseModel):
    balance: int
    currency: str = "credits"


class ProfileResponse(BaseModel):
    user: ProfileUser
    subscription: SubscriptionInfo
    wallet: WalletInfo


class PublicConfig(BaseModel):
    maxDepartureWindowDays: int
    maxStayNights: int
    minStayNights: int
    maxPassengers: int

# ===== END SECTION: Pydantic MODELS =====


# =======================================
# SECTION: FastAPI APP AND CORS
# =======================================

app = FastAPI()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== END SECTION: FastAPI APP AND CORS =====


# =======================================
# SECTION: ENV, DUFFEL AND EMAIL CONFIG
# =======================================

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

if not DUFFEL_ACCESS_TOKEN:
    print("WARNING: DUFFEL_ACCESS_TOKEN is not set, searches will fail")

# Hard caps, tuned for two month window and 50-100 offers per pair
MAX_OFFERS_PER_PAIR_HARD = 300
MAX_OFFERS_TOTAL_HARD = 4000
MAX_DATE_PAIRS_HARD = 60

SYNC_PAIR_THRESHOLD = 10
# Base default, can be overridden via AdminConfig in run_search_job
PARALLEL_WORKERS = 6

# Email alert configuration for SMTP2Go
SMTP_HOST = os.getenv("SMTP_HOST", "mail-eu.smtp2go.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "2525"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", "price-alert@flyyv.com")
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")

# Frontend base URL for deep links in alert emails
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://app.flyyv.com")

# Simple single price watch config for testing
WATCH_ORIGIN = os.getenv("WATCH_ORIGIN", "LON")
WATCH_DESTINATION = os.getenv("WATCH_DESTINATION", "TLV")
WATCH_START_DATE = os.getenv("WATCH_START_DATE")  # YYYY-MM-DD
WATCH_END_DATE = os.getenv("WATCH_END_DATE")      # YYYY-MM-DD
WATCH_STAY_NIGHTS = int(os.getenv("WATCH_STAY_NIGHTS", "7"))
WATCH_MAX_PRICE = float(os.getenv("WATCH_MAX_PRICE", "720"))

# Global toggle for alerts
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "true").lower() == "true"

# ===== END SECTION: ENV, DUFFEL AND EMAIL CONFIG =====


# =======================================
# SECTION: IN MEMORY STORES
# =======================================

USER_WALLETS: Dict[str, int] = {}
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
    pairs: List[Tuple[date, date]] = []

    min_stay = max(1, params.minStayDays)
    max_stay = max(min_stay, params.maxStayDays)

    if params.earliestDeparture == params.latestDeparture and min_stay == max_stay:
        dep = params.earliestDeparture
        ret = dep + timedelta(days=min_stay)
        pairs.append((dep, ret))
        return pairs[:max_pairs]

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

    resp = requests.post(url, json=payload, headers=duffel_headers(), timeout=10)
    if resp.status_code >= 400:
        print("Duffel offer_requests error:", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Duffel API error")

    body = resp.json()
    return body.get("data", {})


def duffel_list_offers(offer_request_id: str, limit: int = 300) -> List[dict]:
    url = f"{DUFFEL_API_BASE}/air/offers"
    params = {
        "offer_request_id": offer_request_id,
        "limit": min(limit, 300),
        "sort": "total_amount",
    }

    resp = requests.get(url, params=params, headers=duffel_headers(), timeout=10)
    if resp.status_code >= 400:
        print("Duffel offers error:", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Duffel API error")

    body = resp.json()
    data = body.get("data", [])
    return list(data)[:limit]


def build_iso_duration(minutes: int) -> str:
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
                    "direction": direction,
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


def balance_airlines(
    options: List[FlightOption],
    max_total: Optional[int] = None,
) -> List[FlightOption]:
    """
    Ensure airlines get fair representation while keeping cheapest options first.
    Uses MAX_AIRLINE_SHARE_PERCENT from AdminConfig, default 40 percent.
    The cap is applied relative to the actual result size and is not relaxed later.
    """
    if not options:
        return []

    sorted_by_price = sorted(options, key=lambda x: x.price)

    if max_total is None or max_total <= 0:
        max_total = len(sorted_by_price)

    actual_total = min(max_total, len(sorted_by_price))

    max_share_percent = get_config_int("MAX_AIRLINE_SHARE_PERCENT", 40)
    if max_share_percent <= 0 or max_share_percent > 100:
        max_share_percent = 40

    airline_counts: Dict[str, int] = defaultdict(int)
    result: List[FlightOption] = []

    unique_airlines = {o.airlineCode or o.airline for o in sorted_by_price}
    num_airlines = max(1, len(unique_airlines))

    base_cap = max(1, (max_share_percent * actual_total) // 100)
    per_airline_cap = max(base_cap, actual_total // num_airlines if num_airlines else base_cap)

    for opt in sorted_by_price:
        if len(result) >= actual_total:
            break

        key = opt.airlineCode or opt.airline
        if airline_counts[key] >= per_airline_cap:
            continue

        airline_counts[key] += 1
        result.append(opt)

    result.sort(key=lambda x: x.price)
    return result

# ===== END SECTION: FILTERING AND BALANCING =====


# =======================================
# SECTION: SHARED SEARCH HELPERS
# =======================================

def effective_caps(params: SearchParams) -> Tuple[int, int, int]:
    """
    Decide how many date pairs and offers to scan in total.

    We ignore any maxDatePairs coming from the frontend and instead use
    an admin config key MAX_DATE_PAIRS, so Smart mode always scans the
    full window you define in Directus.
    """
    # How many departure/return pairs we are allowed to scan
    config_max_pairs = get_config_int("MAX_DATE_PAIRS", 60)
    max_pairs = max(1, min(config_max_pairs, MAX_DATE_PAIRS_HARD))

    # Requested caps from the client
    requested_per_pair = max(1, params.maxOffersPerPair)
    requested_total = max(1, params.maxOffersTotal)

    # Global caps from admin config
    config_max_offers_pair = get_config_int("MAX_OFFERS_PER_PAIR", 80)
    config_max_offers_total = get_config_int("MAX_OFFERS_TOTAL", 4000)

    # Final per pair and total caps, respecting both config and hard limits
    max_offers_pair = max(
        1,
        min(requested_per_pair, config_max_offers_pair, MAX_OFFERS_PER_PAIR_HARD),
    )
    max_offers_total = max(
        1,
        min(requested_total, config_max_offers_total, MAX_OFFERS_TOTAL_HARD),
    )

    return max_pairs, max_offers_pair, max_offers_total


def estimate_date_pairs(params: SearchParams) -> int:
    max_pairs, _, _ = effective_caps(params)
    pairs = generate_date_pairs(params, max_pairs=max_pairs)
    return len(pairs)


def run_duffel_scan(params: SearchParams) -> List[FlightOption]:
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
    balanced = balance_airlines(filtered, max_total=max_offers_total)
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
    job = JOBS.get(job_id)
    if not job:
        print(f"[JOB {job_id}] Job not found in memory")
        return

    job.status = JobStatus.RUNNING
    job.updated_at = datetime.utcnow()
    JOBS[job_id] = job
    print(f"[JOB {job_id}] Starting async search")

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

        # Allow tuning of worker count from AdminConfig
        parallel_workers = get_config_int("PARALLEL_WORKERS", PARALLEL_WORKERS)
        parallel_workers = max(1, min(parallel_workers, 16))

        with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
            for batch_start in range(0, total_pairs, parallel_workers):
                if total_count >= max_offers_total:
                    print(f"[JOB {job_id}] Reached max_offers_total before batch, stopping")
                    break

                batch_pairs = date_pairs[batch_start: batch_start + parallel_workers]
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

                    existing = JOB_RESULTS.get(job_id, [])
                    combined = existing + batch_mapped

                    filtered = apply_filters(combined, job.params)
                    balanced = balance_airlines(filtered, max_total=max_offers_total)

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
# SECTION: PRICE WATCH HELPERS
# =======================================

def build_flyyv_link(params: SearchParams, departure: str, return_date: str) -> str:
    base = FRONTEND_BASE_URL.rstrip("/")
    return (
        f"{base}/?origin={params.origin}"
        f"&destination={params.destination}"
        f"&departure={departure}"
        f"&return={return_date}"
        f"&cabin={params.cabin}"
        f"&passengers={params.passengers}"
    )


def run_price_watch() -> Dict[str, Any]:
    """
    Single test watch rule:
    origin, destination, start, end, stay nights and max price are from env.
    For each date pair we store:
      - totalFlights
      - minPrice and maxPrice
      - whether any fare is under WATCH_MAX_PRICE
      - a single representative flight under the threshold for deep links
    """
    if not WATCH_START_DATE or not WATCH_END_DATE:
        raise HTTPException(
            status_code=500,
            detail="WATCH_START_DATE and WATCH_END_DATE must be configured in YYYY-MM-DD format",
        )

    try:
        start = datetime.strptime(WATCH_START_DATE, "%Y-%m-%d").date()
        end = datetime.strptime(WATCH_END_DATE, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail="WATCH_START_DATE or WATCH_END_DATE invalid format, expected YYYY-MM-DD",
        )

    if end < start:
        raise HTTPException(
            status_code=500,
            detail="WATCH_END_DATE must be on or after WATCH_START_DATE",
        )

    params = SearchParams(
        origin=WATCH_ORIGIN,
        destination=WATCH_DESTINATION,
        earliestDeparture=start,
        latestDeparture=end,
        minStayDays=WATCH_STAY_NIGHTS,
        maxStayDays=WATCH_STAY_NIGHTS,
        maxPrice=None,
        cabin="BUSINESS",
        passengers=1,
        stopsFilter=None,
    )

    # All watched date pairs, even ones that return no data
    watched_pairs = generate_date_pairs(params, max_pairs=365)

    # Actual scan, may be capped by hard limits
    options = run_duffel_scan(params)
    
        # Find the last date pair that actually produced results in this scan
    scanned_pairs: List[Tuple[str, str]] = sorted(
        {(opt.departureDate, opt.returnDate) for opt in options}
    )
    if scanned_pairs:
        last_scanned_dep, last_scanned_ret = scanned_pairs[-1]
    else:
        last_scanned_dep = None
        last_scanned_ret = None

    grouped: Dict[Tuple[str, str], List[FlightOption]] = defaultdict(list)
    for opt in options:
        key = (opt.departureDate, opt.returnDate)
        grouped[key].append(opt)

    pairs_summary: List[Dict[str, Any]] = []
    any_under = False

    for dep, ret in watched_pairs:
        dep_str = dep.isoformat()
        ret_str = ret.isoformat()

        # Stop listing once we pass the last departure date that produced results
        if last_scanned_dep is not None and dep_str > last_scanned_dep:
            break

        flights = grouped.get((dep_str, ret_str), [])

        if not flights:
            status = "no_data"
            cheapest_price = None
            cheapest_currency = None
            cheapest_airline = None
            flights_under: List[Dict[str, Any]] = []
            total_flights = 0
            min_price = None
            max_price = None
        else:
            flights_sorted = sorted(flights, key=lambda f: f.price)
            total_flights = len(flights_sorted)
            min_price = flights_sorted[0].price
            max_price = flights_sorted[-1].price

            cheapest = flights_sorted[0]
            cheapest_price = cheapest.price
            cheapest_currency = cheapest.currency
            cheapest_airline = cheapest.airline

            if cheapest_price <= WATCH_MAX_PRICE:
                status = "under_threshold"
                any_under = True
            else:
                status = "above_threshold"

            flights_under = []
            if status == "under_threshold":
                # Keep only the single cheapest flight per pair for deep linking
                flyyv_link = build_flyyv_link(params, cheapest.departureDate, cheapest.returnDate)
                flights_under.append(
                    {
                        "airline": cheapest.airline,
                        "airlineCode": cheapest.airlineCode,
                        "price": cheapest.price,
                        "currency": cheapest.currency,
                        "origin": cheapest.origin,
                        "destination": cheapest.destination,
                        "departureDate": cheapest.departureDate,
                        "returnDate": cheapest.returnDate,
                        "flyyvLink": flyyv_link,
                        "airlineUrl": cheapest.url or "",
                    }
                )

        pairs_summary.append(
            {
                "departureDate": dep_str,
                "returnDate": ret_str,
                "status": status,
                "cheapestPrice": cheapest_price,
                "cheapestCurrency": cheapest_currency,
                "cheapestAirline": cheapest_airline,
                "totalFlights": total_flights,
                "minPrice": min_price,
                "maxPrice": max_price,
                "flightsUnderThreshold": flights_under,
            }
        )

    return {
        "origin": WATCH_ORIGIN,
        "destination": WATCH_DESTINATION,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "cabin": params.cabin,
        "passengers": params.passengers,
        "stay_nights": WATCH_STAY_NIGHTS,
        "max_price": WATCH_MAX_PRICE,
        "any_under_threshold": any_under,
        "last_scanned_departure": last_scanned_dep,
        "last_scanned_return": last_scanned_ret,
        "pairs": pairs_summary,
    }

# ===== END SECTION: PRICE WATCH HELPERS =====


# =======================================
# SECTION: EMAIL HELPERS
# =======================================

def send_test_alert_email() -> None:
    """
    Simple SMTP test using SMTP2Go.
    Uses environment variables:
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    ALERT_FROM_EMAIL, ALERT_TO_EMAIL
    """
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_TO_EMAIL):
        raise HTTPException(
            status_code=500,
            detail="SMTP settings are not fully configured on the server",
        )

    msg = EmailMessage()
    msg["Subject"] = "Flyyv test alert email"
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = ALERT_TO_EMAIL
    msg.set_content(
        "This is a test Flyyv alert sent via SMTP2Go.\n\n"
        "If you are reading this, SMTP is working."
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send test email: {e}",
        )


def send_daily_alert_email() -> None:
    """
    Build and send the price watch alert email for the configured window.
    Shows one line per date pair, with:
      - number of flights found
      - price range
      - cheapest fare and airline
      - Flyyv and airline links only for pairs under the threshold
    """
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_TO_EMAIL):
        raise HTTPException(
            status_code=500,
            detail="SMTP settings are not fully configured on the server",
        )

    watch = run_price_watch()
    threshold = watch["max_price"]
    pairs = watch["pairs"]
    any_under = watch["any_under_threshold"]

    start_dt = datetime.fromisoformat(watch["start_date"])
    end_dt = datetime.fromisoformat(watch["end_date"])
    start_label = start_dt.strftime("%d %B %Y")
    end_label = end_dt.strftime("%d %B %Y")

    if any_under:
        subject_suffix = f"fare found under £{int(threshold)}"
    else:
        subject_suffix = "no changes"

    subject = (
        f"Your {watch['origin']} to {watch['destination']} watch, "
        f"{subject_suffix}"
    )

    lines: List[str] = []

    # Header
    lines.append(
        f"Watch: {watch['origin']} \u2192 {watch['destination']}, "
        f"{watch['cabin'].title()} class, "
        f"{watch['stay_nights']} nights, {watch['passengers']} pax, max £{int(threshold)}"
    )
    lines.append(f"Date window: {start_label} to {end_label}")
    lines.append("")

    # Top section, only if there are any fares under threshold
    if any_under:
        lines.append(f"Deals found under your £{int(threshold)} limit:")
        lines.append("")

        for p in pairs:
            if p["status"] != "under_threshold":
                continue

            dep_iso = p["departureDate"]
            ret_iso = p["returnDate"]
            dep_dt = datetime.fromisoformat(dep_iso)
            ret_dt = datetime.fromisoformat(ret_iso)
            dep_label = dep_dt.strftime("%d %b")
            ret_label = ret_dt.strftime("%d %b")

            cheapest_price = p["cheapestPrice"]
            cheapest_airline = p["cheapestAirline"]
            total_flights = p.get("totalFlights") or 0
            min_price = p.get("minPrice")
            max_price = p.get("maxPrice")

            if cheapest_price is None or cheapest_airline is None:
                continue
            if min_price is None or max_price is None:
                min_price = cheapest_price
                max_price = cheapest_price

            flights_under = p.get("flightsUnderThreshold") or []
            primary = flights_under[0] if flights_under else None

            if primary:
                route = f"{primary['origin']} \u2192 {primary['destination']}"
                flyyv_link = primary["flyyvLink"]
                airline_url = primary.get("airlineUrl") or ""
            else:
                route = f"{watch['origin']} \u2192 {watch['destination']}"
                flyyv_link = ""
                airline_url = ""

            lines.append(
                f"{dep_label} \u2192 {ret_label}: {total_flights} flights, "
                f"range £{int(min_price)} to £{int(max_price)}, "
                f"cheapest £{int(cheapest_price)} with {cheapest_airline}"
            )
            lines.append(f"  Route: {route}")
            if flyyv_link:
                lines.append(f"  View in Flyyv: {flyyv_link}")
            if airline_url:
                lines.append(f"  Airline site: {airline_url}")
            lines.append("")
    else:
        lines.append(
            f"No fares under £{int(threshold)} were found for any watched dates."
        )
        lines.append("")

    # Summary of all watched date pairs
    lines.append("Summary of all watched dates:")
    lines.append("")

    for p in pairs:
        dep_iso = p["departureDate"]
        ret_iso = p["returnDate"]
        dep_dt = datetime.fromisoformat(dep_iso)
        ret_dt = datetime.fromisoformat(ret_iso)
        dep_label = dep_dt.strftime("%d %b")
        ret_label = ret_dt.strftime("%d %b")

                status = p["status"]
        total_flights = p.get("totalFlights") or 0
        min_price = p.get("minPrice")
        max_price = p.get("maxPrice")

        if status == "no_data":
            # No options were stored for this date pair in this scan,
            # usually due to an API timeout or hitting a cap.
            note = "no data captured in this scan"
        elif total_flights == 0 or min_price is None or max_price is None:
            note = "no flights returned"
        else:
            note = (
                f"{total_flights} flights, "
                f"range £{int(min_price)} to £{int(max_price)}"
            )

        lines.append(f"{dep_label} \u2192 {ret_label}: {note}")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = ALERT_TO_EMAIL
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send daily alert email: {e}",
        )

# ===== END SECTION: EMAIL HELPERS =====


# =======================================
# SECTION: ROOT, HEALTH AND ROUTES
# =======================================

@app.get("/")
def home():
    return {"message": "Flyyv backend is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/routes")
def list_routes():
    return [route.path for route in app.routes]


@app.get("/test-email-alert")
def test_email_alert():
    """
    Simple endpoint to verify SMTP2Go configuration.
    Open /test-email-alert in a browser and you should receive an email.
    """
    send_test_alert_email()
    return {"detail": "Test alert email sent"}


@app.get("/trigger-daily-alert")
def trigger_daily_alert():
    """
    Endpoint that sends the price watch alert email.
    This is what cron will call every N minutes.
    """
    if not ALERTS_ENABLED:
        return {"detail": "Alerts are currently disabled"}
    send_daily_alert_email()
    return {"detail": "Daily alert email sent"}

# ===== END SECTION: ROOT, HEALTH AND ROUTES =====


# =======================================
# SECTION: MAIN SEARCH ROUTES
# =======================================

@app.post("/search-business")
def search_business(params: SearchParams, background_tasks: BackgroundTasks):
    """
    Main search endpoint.

    One date pair only sync search, returns results immediately.
    Multiple date pairs always async, returns a jobId and lets the background
    worker do the heavy lifting so we avoid 504 timeouts.
    """
    if not DUFFEL_ACCESS_TOKEN:
        return {
            "status": "error",
            "source": "duffel_not_configured",
            "options": [],
        }

    max_passengers = get_config_int("MAX_PASSENGERS", 4)
    if params.passengers > max_passengers:
        params.passengers = max_passengers

    default_cabin = get_config_str("DEFAULT_CABIN", "BUSINESS") or "BUSINESS"
    if not params.cabin:
        params.cabin = default_cabin

    estimated_pairs = estimate_date_pairs(params)

    print(
        f"[search_business] estimated_pairs={estimated_pairs}, "
        f"fullCoverage={params.fullCoverage}"
    )

    if estimated_pairs <= 1:
        options = run_duffel_scan(params)
        return {
            "status": "ok",
            "mode": "sync",
            "source": "duffel",
            "options": [o.dict() for o in options],
        }

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
    JOB_RESULTS[job_id] = []

    background_tasks.add_task(run_search_job, job_id)

    return {
        "status": "ok",
        "mode": "async",
        "jobId": job_id,
        "message": "Search started",
    }

# ===== END SECTION: MAIN SEARCH ROUTES =====


# =======================================
# SECTION: SEARCH STATUS AND RESULTS ROUTES
# =======================================

@app.get("/search-status/{job_id}", response_model=SearchStatusResponse)
def get_search_status(job_id: str, preview_limit: int = 20):
    job = JOBS.get(job_id)

    if not job:
        print(f"[search-status] Job {job_id} not found. Known jobs: {list(JOBS.keys())}")

        return SearchStatusResponse(
            jobId=job_id,
            status=JobStatus.PENDING,
            processedPairs=0,
            totalPairs=0,
            progress=0.0,
            error="Job not found in memory yet",
            previewCount=0,
            previewOptions=[],
        )

    options = JOB_RESULTS.get(job_id, [])

    if preview_limit > 0:
        preview = options[:preview_limit]
    else:
        preview = []

    total_pairs = job.total_pairs or 0
    processed_pairs = job.processed_pairs or 0
    progress = float(processed_pairs) / float(total_pairs) if total_pairs > 0 else 0.0

    return SearchStatusResponse(
        jobId=job.id,
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
    job = JOBS.get(job_id)
    if not job:
        print(f"[search-results] Job {job_id} not found. Known jobs: {list(JOBS.keys())}")
        return SearchResultsResponse(
            jobId=job_id,
            status=JobStatus.PENDING,
            totalResults=0,
            offset=0,
            limit=limit,
            options=[],
        )

    options = JOB_RESULTS.get(job_id, [])

    offset = max(0, offset)
    limit = max(1, min(limit, 600))
    end = min(offset + limit, len(options))
    slice_ = options[offset:end]

    return SearchResultsResponse(
        jobId=job.id,
        status=job.status,
        totalResults=len(options),
        offset=offset,
        limit=limit,
        options=slice_,
    )

# ===== END SECTION: SEARCH STATUS AND RESULTS ROUTES =====


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
# SECTION: CONFIG DEBUG ENDPOINT
# =======================================

@app.get("/config-debug")
def config_debug(
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

        return {
        "MAX_OFFERS_TOTAL": get_config_int("MAX_OFFERS_TOTAL", 4000),
        "MAX_OFFERS_PER_PAIR": get_config_int("MAX_OFFERS_PER_PAIR", 80),
        "MAX_DATE_PAIRS": get_config_int("MAX_DATE_PAIRS", 60),
        "MAX_PASSENGERS": get_config_int("MAX_PASSENGERS", 4),
        "DEFAULT_CABIN": get_config_str("DEFAULT_CABIN", "BUSINESS") or "BUSINESS",
        "SEARCH_MODE": get_config_str("SEARCH_MODE", "AUTO") or "AUTO",
        "MAX_OFFERS_PER_PAIR_HARD": MAX_OFFERS_PER_PAIR_HARD,
        "MAX_OFFERS_TOTAL_HARD": MAX_OFFERS_TOTAL_HARD,
        "MAX_DATE_PAIRS_HARD": MAX_DATE_PAIRS_HARD,
        "PARALLEL_WORKERS": get_config_int("PARALLEL_WORKERS", PARALLEL_WORKERS),
        "MAX_AIRLINE_SHARE_PERCENT": get_config_int("MAX_AIRLINE_SHARE_PERCENT", 40),
    }

# ===== END SECTION: CONFIG DEBUG ENDPOINT =====


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
    if not DUFFEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Duffel not configured")

    max_passengers = get_config_int("MAX_PASSENGERS", 4)
    if passengers > max_passengers:
        passengers = max_passengers

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


# =======================================
# SECTION: PUBLIC CONFIG, USER SYNC, PROFILE, ALERTS
# =======================================

@app.get("/public-config", response_model=PublicConfig)
def public_config():
    # Open the UI search window to 2 months by default
    max_window = get_config_int("MAX_DEPARTURE_WINDOW_DAYS", 60)
    max_stay = get_config_int("MAX_STAY_NIGHTS", 30)
    min_stay = get_config_int("MIN_STAY_NIGHTS", 1)
    max_passengers = get_config_int("MAX_PASSENGERS", 4)

    return PublicConfig(
        maxDepartureWindowDays=max_window,
        maxStayNights=max_stay,
        minStayNights=min_stay,
        maxPassengers=max_passengers,
    )


@app.post("/user-sync")
def user_sync(payload: UserSyncPayload):
    """
    Accept user profile from Base44 and upsert into app_users.
    """
    db = SessionLocal()
    try:
        user = (
            db.query(AppUser)
            .filter(AppUser.external_id == payload.external_id)
            .first()
        )

        if user is None:
            user = AppUser(
                external_id=payload.external_id,
                email=payload.email,
                first_name=payload.first_name,
                last_name=payload.last_name,
                country=payload.country,
                marketing_consent=payload.marketing_consent,
                source=payload.source or "base44",
            )
            db.add(user)
        else:
            user.email = payload.email
            user.first_name = payload.first_name
            user.last_name = payload.last_name
            user.country = payload.country
            user.marketing_consent = payload.marketing_consent
            user.source = payload.source or user.source

        db.commit()
        db.refresh(user)

        return {"status": "ok", "id": user.id}
    finally:
        db.close()


@app.get("/profile", response_model=ProfileResponse)
def get_profile(
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    """
    Return profile summary for the profile page.
    """
    wallet_balance = USER_WALLETS.get(x_user_id, 0)

    db = SessionLocal()
    try:
        app_user = (
            db.query(AppUser)
            .filter(AppUser.external_id == x_user_id)
            .first()
        )
    finally:
        db.close()

    if app_user:
        name_parts: List[str] = []
        if app_user.first_name:
            name_parts.append(app_user.first_name)
        if app_user.last_name:
            name_parts.append(app_user.last_name)
        name = " ".join(name_parts) or "Flyyv user"
        email = app_user.email
    else:
        name = "Flyyv user"
        email = None

    profile_user = ProfileUser(
        id=x_user_id,
        name=name,
        email=email,
        plan="Free",
    )

    subscription = SubscriptionInfo(
        plan="Flyyv Free",
        billingCycle=None,
        renewalDate=None,
        monthlyCredits=0,
    )

    wallet = WalletInfo(
        balance=wallet_balance,
        currency="credits",
    )

    return ProfileResponse(
        user=profile_user,
        subscription=subscription,
        wallet=wallet,
    )


@app.get("/alerts")
def get_alerts(
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    """
    Return list of alerts for the current user.
    For now this returns an empty list so the frontend can integrate safely.
    """
    return []

# ===== END SECTION: PUBLIC CONFIG, USER SYNC, PROFILE, ALERTS =====
