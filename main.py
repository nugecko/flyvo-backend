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
from models import AdminConfig, AppUser, Alert, AlertRun

# =======================================
# SECTION: ALERT TOGGLES
# =======================================

def master_alerts_enabled() -> bool:
    """
    Hard master switch controlled by environment variable ALERTS_ENABLED.
    If this is set to 'false' (case insensitive), alerts are completely disabled.
    """
    value = os.getenv("ALERTS_ENABLED", "true")
    return value.lower() == "true"


def alerts_globally_enabled(db: Session) -> bool:
    """
    Global switch stored in admin_config with key = 'GLOBAL_ALERTS'.
    If the row does not exist, default to True.
    """
    config = (
        db.query(AdminConfig)
        .filter(AdminConfig.key == "GLOBAL_ALERTS")
        .first()
    )
    if not config:
        return True
    # if the column is missing for any reason, also default to True
    if not hasattr(config, "alerts_enabled"):
        return True
    return bool(config.alerts_enabled)


def user_allows_alerts(user: AppUser) -> bool:
    """
    Per user toggle, defaults to True if the column is missing.
    """
    if not hasattr(user, "email_alerts_enabled"):
        return True
    return bool(user.email_alerts_enabled)


def should_send_alert(db: Session, user: AppUser) -> bool:
    """
    Combined logic:
    1. Environment master toggle must be ON
    2. Global admin_config toggle must be ON
    3. User toggle must be ON
    """
    if not master_alerts_enabled():
        return False
    if not alerts_globally_enabled(db):
        return False
    if not user_allows_alerts(user):
        return False
    return True

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


def get_config_bool(key: str, default_value: bool) -> bool:
    """
    Read a config value from admin_config and cast to bool.
    Accepts values like 1, 0, true, false, yes, no, on, off.
    Falls back to default_value if missing or invalid.
    """
    raw = get_config_str(key, None)
    if raw is None:
        return default_value

    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
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

    outboundSegments: Optional[List[Dict[str, Any]]] = None
    returnSegments: Optional[List[Dict[str, Any]]] = None

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


class AlertUpdatePayload(BaseModel):
    alert_type: Optional[str] = None
    max_price: Optional[int] = None
    is_active: Optional[bool] = None
    departure_start: Optional[date] = None
    departure_end: Optional[date] = None
    return_start: Optional[date] = None
    return_end: Optional[date] = None
    # We will later use this to migrate alerts between single and smart
    mode: Optional[str] = None


class AlertStatusPayload(BaseModel):
    is_active: Optional[bool] = None
    isActive: Optional[bool] = None


class AlertBase(BaseModel):
    email: str
    origin: str
    destination: str
    cabin: str
    search_mode: Optional[str] = "flexible"

    departure_start: date
    departure_end: date
    return_start: Optional[date] = None
    return_end: Optional[date] = None

    alert_type: str
    max_price: Optional[int] = None

    # single = specific date pair
    # smart  = smart search / date window
    mode: Optional[str] = "single"


class AlertCreate(AlertBase):
    pass


class AlertOut(AlertBase):
    id: str
    times_sent: int
    is_active: bool
    last_price: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

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

MAX_OFFERS_PER_PAIR_HARD = 300
MAX_OFFERS_TOTAL_HARD = 4000
MAX_DATE_PAIRS_HARD = 60

SYNC_PAIR_THRESHOLD = 10
PARALLEL_WORKERS = 6

SMTP_HOST = os.getenv("SMTP_HOST", "mail-eu.smtp2go.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "2525"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", "price-alert@flyyv.com")
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")

FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://app.flyyv.com")

WATCH_ORIGIN = os.getenv("WATCH_ORIGIN", "LON")
WATCH_DESTINATION = os.getenv("WATCH_DESTINATION", "TLV")
WATCH_START_DATE = os.getenv("WATCH_START_DATE")
WATCH_END_DATE = os.getenv("WATCH_END_DATE")
WATCH_STAY_NIGHTS = int(os.getenv("WATCH_STAY_NIGHTS", "7"))
WATCH_MAX_PRICE = float(os.getenv("WATCH_MAX_PRICE", "720"))

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
    outbound_segments_json: List[dict] = []
    return_segments_json: List[dict] = []

    if len(slices) >= 1:
        outbound_segments_json = slices[0].get("segments", []) or []
    if len(slices) >= 2:
        return_segments_json = slices[1].get("segments", []) or []

    stops_outbound = max(0, len(outbound_segments_json) - 1)

    origin_code = None
    destination_code = None
    origin_airport = None
    destination_airport = None

    if outbound_segments_json:
        first_segment = outbound_segments_json[0]
        last_segment = outbound_segments_json[-1]

        origin_obj = first_segment.get("origin", {}) or {}
        dest_obj = last_segment.get("destination", {}) or {}

        origin_code = origin_obj.get("iata_code")
        destination_code = dest_obj.get("iata_code")
        origin_airport = origin_obj.get("name")
        destination_airport = dest_obj.get("name")

    outbound_segments_info: List[Dict[str, Any]] = []
    return_segments_info: List[Dict[str, Any]] = []
    aircraft_codes: List[str] = []
    aircraft_names: List[str] = []

    outbound_total_minutes = 0
    return_total_minutes = 0

    def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def process_segment_list(direction: str, seg_list: List[dict]) -> Tuple[List[Dict[str, Any]], int]:
        result: List[Dict[str, Any]] = []
        total_minutes = 0

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

            dep_at_str = seg.get("departing_at")
            arr_at_str = seg.get("arriving_at")

            dep_dt = parse_iso(dep_at_str)
            arr_dt = parse_iso(arr_at_str)

            duration_minutes_seg: Optional[int] = None
            if dep_dt and arr_dt:
                try:
                    duration_minutes_seg = int((arr_dt - dep_dt).total_seconds() // 60)
                except Exception:
                    duration_minutes_seg = None

            layover_minutes_to_next: Optional[int] = None
            if idx < len(seg_list) - 1:
                this_arr_str = seg.get("arriving_at")
                next_dep_str = seg_list[idx + 1].get("departing_at")
                this_arr_dt = parse_iso(this_arr_str)
                next_dep_dt = parse_iso(next_dep_str)
                if this_arr_dt and next_dep_dt:
                    try:
                        layover_minutes_to_next = int((next_dep_dt - this_arr_dt).total_seconds() // 60)
                    except Exception:
                        layover_minutes_to_next = None

            if duration_minutes_seg is None:
                duration_minutes_seg = 0

            total_minutes_local = duration_minutes_seg
            total_minutes += total_minutes_local

            result.append(
                {
                    "direction": direction,
                    "origin": o.get("iata_code"),
                    "originAirport": o.get("name"),
                    "destination": d.get("iata_code"),
                    "destinationAirport": d.get("name"),
                    "departingAt": dep_at_str,
                    "arrivingAt": arr_at_str,
                    "aircraftCode": aircraft_code,
                    "aircraftName": aircraft_name,
                    "durationMinutes": duration_minutes_seg,
                    "layoverMinutesToNext": layover_minutes_to_next,
                }
            )

        return result, total_minutes

    outbound_segments_info, outbound_total_minutes = process_segment_list("outbound", outbound_segments_json)
    return_segments_info, return_total_minutes = process_segment_list("return", return_segments_json)

    duration_minutes = outbound_total_minutes

    if outbound_total_minutes or return_total_minutes:
        total_duration_minutes = outbound_total_minutes + return_total_minutes
    else:
        total_duration_minutes = duration_minutes

    iso_duration = build_iso_duration(duration_minutes)

    stopover_codes: List[str] = []
    stopover_airports: List[str] = []
    if len(outbound_segments_json) > 1:
        for seg in outbound_segments_json[:-1]:
            dest_obj = seg.get("destination", {}) or {}
            code = dest_obj.get("iata_code")
            name = dest_obj.get("name")
            if code:
                stopover_codes.append(code)
            if name:
                stopover_airports.append(name)

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
        totalDurationMinutes=total_duration_minutes,
        duration=iso_duration,
        origin=origin_code,
        destination=destination_code,
        originAirport=origin_airport,
        destinationAirport=destination_airport,
        stopoverCodes=stopover_codes or None,
        stopoverAirports=stopover_airports or None,
        outboundSegments=outbound_segments_info or None,
        returnSegments=return_segments_info or None,
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
    config_max_pairs = get_config_int("MAX_DATE_PAIRS", 60)
    max_pairs = max(1, min(config_max_pairs, MAX_DATE_PAIRS_HARD))

    requested_per_pair = max(1, params.maxOffersPerPair)
    requested_total = max(1, params.maxOffersTotal)

    config_max_offers_pair = get_config_int("MAX_OFFERS_PER_PAIR", 80)
    config_max_offers_total = get_config_int("MAX_OFFERS_TOTAL", 4000)

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
    print(
        f"[search] run_duffel_scan START origin={params.origin} "
        f"dest={params.destination}"
    )

    max_pairs, max_offers_pair, max_offers_total = effective_caps(params)
    print(
        f"[search] caps max_pairs={max_pairs} "
        f"max_offers_pair={max_offers_pair} max_offers_total={max_offers_total}"
    )

    date_pairs = generate_date_pairs(params, max_pairs=max_pairs)
    print(f"[search] generated {len(date_pairs)} date pairs")

    if not date_pairs:
        print("[search] no date pairs generated, returning empty list")
        return []

    collected_offers: List[Tuple[dict, date, date]] = []
    total_count = 0

    for dep, ret in date_pairs:
        if total_count >= max_offers_total:
            print(
                f"[search] total_count {total_count} reached max_offers_total "
                f"{max_offers_total}, stopping"
            )
            break

        print(
            f"[search] querying Duffel for pair dep={dep} ret={ret} "
            f"current_total={total_count}"
        )

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
                print("[search] Duffel offer_request returned no id, skipping pair")
                continue

            per_pair_limit = min(max_offers_pair, max_offers_total - total_count)
            print(
                f"[search] listing offers for request_id={offer_request_id} "
                f"per_pair_limit={per_pair_limit}"
            )
            offers_json = duffel_list_offers(offer_request_id, limit=per_pair_limit)
        except HTTPException as e:
            print(
                f"[search] Duffel HTTPException for dep={dep} ret={ret}: {e.detail}"
            )
            continue
        except Exception as e:
            print(f"[search] Unexpected Duffel error for dep={dep} ret={ret}: {e}")
            continue

        num_offers = len(offers_json)
        print(
            f"[search] Duffel returned {num_offers} offers for dep={dep} ret={ret}"
        )

        for offer in offers_json:
            collected_offers.append((offer, dep, ret))
            total_count += 1
            if total_count >= max_offers_total:
                print(
                    f"[search] reached max_offers_total={max_offers_total} "
                    f"while collecting offers, breaking inner loop"
                )
                break

    print(
        f"[search] collected total {len(collected_offers)} offers across all pairs"
    )

    mapped: List[FlightOption] = [
        map_duffel_offer_to_option(offer, dep, ret)
        for offer, dep, ret in collected_offers
    ]
    print(f"[search] mapped {len(mapped)} offers to FlightOption")

    filtered = apply_filters(mapped, params)
    print(f"[search] filtered down to {len(filtered)} offers")

    balanced = balance_airlines(filtered, max_total=max_offers_total)
    print(f"[search] balance_airlines returned {len(balanced)} offers")
    print("[search] run_duffel_scan DONE")
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

    watched_pairs = generate_date_pairs(params, max_pairs=365)
    options = run_duffel_scan(params)

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
# SECTION: ALERT ENGINE HELPERS
# =======================================

def build_search_params_for_alert(alert: Alert) -> SearchParams:
    dep_start = alert.departure_start
    dep_end = alert.departure_end or alert.departure_start

    if alert.return_start and alert.return_end:
        min_stay = max(1, (alert.return_start - dep_start).days)
        max_stay = max(min_stay, (alert.return_end - dep_start).days)
    else:
        min_stay = 1
        max_stay = 21

    return SearchParams(
        origin=alert.origin,
        destination=alert.destination,
        earliestDeparture=dep_start,
        latestDeparture=dep_end,
        minStayDays=min_stay,
        maxStayDays=max_stay,
        maxPrice=alert.max_price,
        cabin=alert.cabin or "BUSINESS",
        passengers=1,
        stopsFilter=None,
    )


def send_alert_email_for_alert(alert: Alert, cheapest: FlightOption, params: SearchParams) -> None:
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_FROM_EMAIL):
        raise HTTPException(
            status_code=500,
            detail="SMTP settings are not fully configured on the server",
        )

    to_email = alert.user_email
    if not to_email:
        raise HTTPException(status_code=500, detail="Alert has no user_email")

    subject = (
        f"Flyyv alert: {alert.origin} \u2192 {alert.destination} "
        f"from £{int(cheapest.price)}"
    )

    dep_label = cheapest.departureDate
    ret_label = cheapest.returnDate

    lines: List[str] = []

    lines.append(
        f"Route: {alert.origin} \u2192 {alert.destination}, {alert.cabin.title()} class"
    )
    lines.append(f"Dates: {dep_label} to {ret_label}")
    lines.append("")
    lines.append(
        f"Cheapest fare found: £{int(cheapest.price)} "
        f"with {cheapest.airline} ({cheapest.airlineCode or ''})"
    )
    lines.append("")
    lines.append("To view this alert and explore more dates, go to your Flyyv dashboard:")
    lines.append("https://flyyv.com")
    lines.append("")
    lines.append("You are receiving this because you created a Flyyv price alert.")
    lines.append("To stop these alerts, delete the alert in your Flyyv profile.")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def send_smart_alert_email(alert: Alert, options: List[FlightOption], params: SearchParams) -> None:
    """
    Build a Smart Search style summary email over many date pairs.
    Uses the same Duffel options list that process_alert already fetched.
    """
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_FROM_EMAIL):
        raise HTTPException(
            status_code=500,
            detail="SMTP settings are not fully configured on the server",
        )

    to_email = alert.user_email
    if not to_email:
        raise HTTPException(status_code=500, detail="Alert has no user_email")

    threshold = alert.max_price
    origin = alert.origin
    destination = alert.destination

    # Group options by (departureDate, returnDate)
    grouped: Dict[Tuple[str, str], List[FlightOption]] = {}
    for opt in options:
        key = (opt.departureDate, opt.returnDate)
        grouped.setdefault(key, []).append(opt)

    # Sort pairs by departure then return
    sorted_keys = sorted(grouped.keys())

    any_under = False
    pairs_summary: List[Dict[str, Any]] = []

    for dep_iso, ret_iso in sorted_keys:
        flights = grouped[(dep_iso, ret_iso)]
        prices = [o.price for o in flights]
        if not prices:
            continue

        min_price = min(prices)
        max_price = max(prices)
        cheapest = min(flights, key=lambda o: o.price)

        flights_under: List[FlightOption] = []
        if threshold is not None:
            flights_under = [o for o in flights if o.price <= float(threshold)]
            if flights_under:
                any_under = True

        flyyv_link = build_flyyv_link(params, dep_iso, ret_iso)

        pairs_summary.append(
            {
                "departureDate": dep_iso,
                "returnDate": ret_iso,
                "totalFlights": len(flights),
                "minPrice": min_price,
                "maxPrice": max_price,
                "cheapestPrice": cheapest.price,
                "cheapestAirline": cheapest.airline,
                "flightsUnderThresholdCount": len(flights_under),
                "flyyvLink": flyyv_link,
            }
        )

    # Prepare header labels
    start_label = params.earliestDeparture.strftime("%d %B %Y")
    end_label = params.latestDeparture.strftime("%d %B %Y")

    if threshold is not None and any_under:
        subject_suffix = f"deals under £{int(threshold)}"
    elif threshold is not None:
        subject_suffix = f"no fares under £{int(threshold)}"
    else:
        subject_suffix = "summary update"

    subject = f"Flyyv smart alert: {origin} \u2192 {destination} {subject_suffix}"

    lines: List[str] = []

    nights_label = (
        f"{params.minStayDays} to {params.maxStayDays} nights"
        if params.minStayDays != params.maxStayDays
        else f"{params.minStayDays} nights"
    )

    if threshold is not None:
        lines.append(
            f"Smart watch: {origin} \u2192 {destination}, "
            f"{alert.cabin.title()} class, {nights_label}, "
            f"1 pax, max £{int(threshold)}"
        )
    else:
        lines.append(
            f"Smart watch: {origin} \u2192 {destination}, "
            f"{alert.cabin.title()} class, {nights_label}, 1 pax"
        )

    lines.append(f"Date window: {start_label} to {end_label}")
    lines.append("")

    # Short intro
    if threshold is not None:
        lines.append(f"Max: £{int(threshold)}, 1 pax, {nights_label}")
    else:
        lines.append(f"Trip details: 1 pax, {nights_label}")
    lines.append("")
    lines.append("We scanned many date combinations in this window and picked the best options for you.")
    lines.append("")

    # Build list of candidate pairs
    top_pairs = []
    for p in pairs_summary:
        if p.get("totalFlights", 0) <= 0:
            continue
        if p.get("cheapestPrice") is None:
            continue
        top_pairs.append(p)

    if not top_pairs:
        lines.append("No flights were found in this window in the latest scan.")
    else:
        MAX_RESULTS = 10
        top_pairs_sorted = sorted(
            top_pairs,
            key=lambda x: x["cheapestPrice"],
        )[:MAX_RESULTS]

        lines.append("Top flight deals in your window:")
        lines.append("")

        for p in top_pairs_sorted:
            dep_dt = datetime.fromisoformat(p["departureDate"])
            ret_dt = datetime.fromisoformat(p["returnDate"])
            dep_label = dep_dt.strftime("%d %b")
            ret_label = ret_dt.strftime("%d %b")
            nights = (ret_dt - dep_dt).days

            price_label = int(p["cheapestPrice"])
            airline_label = p.get("cheapestAirline") or "Multiple airlines"

            line = (
                f"£{price_label}, {dep_label} \u2192 {ret_label}, "
                f"{airline_label}, {nights} nights"
            )

            if threshold is not None and float(price_label) <= float(threshold):
                line += "  (within your limit)"

            lines.append(line)

    lines.append("")
    lines.append("View all your alerts:")
    lines.append("https://flyyv.com")
    lines.append("")
    lines.append("You are receiving this because you created a Flyyv smart price alert.")
    lines.append("To stop these alerts, delete the alert in your Flyyv profile.")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def process_alert(alert: Alert, db: Session) -> None:
    now = datetime.utcnow()
    print(
        f"[alerts] process_alert START id={alert.id} "
        f"email={alert.user_email} type={alert.alert_type} mode={alert.mode}"
    )

    # Find the user for this alert
    user = db.query(AppUser).filter(AppUser.email == alert.user_email).first()

    # If there is no user, record and skip
    if not user:
        print(f"[alerts] process_alert SKIP id={alert.id} reason=no_user_for_alert")
        run_row = AlertRun(
            id=str(uuid4()),
            alert_id=alert.id,
            run_at=now,
            price_found=None,
            sent=False,
            reason="no_user_for_alert",
        )
        db.add(run_row)

        alert.last_run_at = now
        alert.updated_at = now
        db.commit()
        return

    # Check master, global and per user switches
    if not should_send_alert(db, user):
        print(f"[alerts] process_alert SKIP id={alert.id} reason=alerts_disabled")
        run_row = AlertRun(
            id=str(uuid4()),
            alert_id=alert.id,
            run_at=now,
            price_found=None,
            sent=False,
            reason="alerts_disabled",
        )
        db.add(run_row)

        alert.last_run_at = now
        alert.updated_at = now
        db.commit()
        return

    # Search behaviour is shared for now, mode only affects formatting
    params = build_search_params_for_alert(alert)
    max_pairs, max_offers_pair, max_offers_total = effective_caps(params)
    print(
        f"[alerts] process_alert SEARCH id={alert.id} "
        f"origin={params.origin} dest={params.destination} "
        f"dep_window={params.earliestDeparture}..{params.latestDeparture} "
        f"mode={alert.mode} caps={max_pairs}/{max_offers_total}"
    )

    options = run_duffel_scan(params)

    if not options:
        print(f"[alerts] process_alert NO_RESULTS id={alert.id}")
        run_row = AlertRun(
            id=str(uuid4()),
            alert_id=alert.id,
            run_at=now,
            price_found=None,
            sent=False,
            reason="no_results",
        )
        db.add(run_row)

        alert.last_run_at = now
        alert.updated_at = now
        db.commit()
        return

    options_sorted = sorted(options, key=lambda o: o.price)
    cheapest = options_sorted[0]
    current_price = int(cheapest.price)

    should_send = False
    send_reason = None

    if alert.alert_type == "price_change":
        if alert.last_price is None:
            should_send = True
            send_reason = "initial"
        elif current_price != alert.last_price:
            should_send = True
            send_reason = "price_change"
        else:
            should_send = False
            send_reason = "no_change"
    elif alert.alert_type == "scheduled_3x":
        should_send = True
        send_reason = "scheduled"
    else:
        should_send = True
        send_reason = f"unknown_type_{alert.alert_type}"

    print(
        f"[alerts] process_alert DECISION id={alert.id} "
        f"should_send={should_send} reason={send_reason} "
        f"current_price={current_price} last_price={alert.last_price} mode={alert.mode}"
    )

    sent_flag = False

    if should_send:
        try:
            if alert.mode == "smart":
                # Smart alerts get a summary email with multiple date pairs
                send_smart_alert_email(alert, options_sorted, params)
            else:
                # Single mode keeps the simple one date pair email
                send_alert_email_for_alert(alert, cheapest, params)

            sent_flag = True
            print(f"[alerts] process_alert EMAIL_SENT id={alert.id} mode={alert.mode}")
        except Exception as e:
            print(f"[alerts] Failed to send email for alert {alert.id}: {e}")
            sent_flag = False
            send_reason = (send_reason or "send_attempt") + "_email_failed"

    run_row = AlertRun(
        id=str(uuid4()),
        alert_id=alert.id,
        run_at=now,
        price_found=current_price,
        sent=sent_flag,
        reason=send_reason,
    )
    db.add(run_row)

    alert.last_price = current_price
    alert.last_run_at = now
    alert.updated_at = now
    if sent_flag:
        alert.times_sent = (alert.times_sent or 0) + 1

    db.commit()

    print(
        f"[alerts] process_alert DONE id={alert.id} "
        f"sent={sent_flag} reason={send_reason} price={current_price} mode={alert.mode}"
    )


def run_all_alerts_cycle() -> None:
    # Hard master switch from environment
    if not master_alerts_enabled():
        print("[alerts] ALERTS_ENABLED is false, skipping alerts cycle")
        return

    if not DUFFEL_ACCESS_TOKEN:
        print("[alerts] DUFFEL_ACCESS_TOKEN not set, skipping alerts cycle")
        return

    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_FROM_EMAIL):
        print("[alerts] SMTP not fully configured, skipping alerts cycle")
        return

    db = SessionLocal()
    try:
        # Global switch from admin_config
        if not alerts_globally_enabled(db):
            print("[alerts] Global alerts disabled in admin_config, skipping alerts cycle")
            return

        alerts = db.query(Alert).filter(Alert.is_active == True).all()  # noqa: E712
        print(f"[alerts] Running alerts cycle for {len(alerts)} alerts")

        for alert in alerts:
            print(
                f"[alerts] CYCLE processing alert id={alert.id} "
                f"email={alert.user_email} mode={alert.mode}"
            )
            try:
                process_alert(alert, db)
            except Exception as e:
                print(f"[alerts] Error processing alert {alert.id}: {e}")
    finally:
        db.close()

# ===== END SECTION: ALERT ENGINE HELPERS =====

# =======================================
# SECTION: EMAIL HELPERS
# =======================================

def send_test_alert_email() -> None:
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

    lines.append(
        f"Watch: {watch['origin']} \u2192 {watch['destination']}, "
        f"{watch['cabin'].title()} class, "
        f"{watch['stay_nights']} nights, {watch['passengers']} pax, max £{int(threshold)}"
    )
    lines.append(f"Date window: {start_label} to {end_label}")
    lines.append("")

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
            max_price = p.get("MaxPrice") if "MaxPrice" in p else p.get("maxPrice")

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
        max_price = p.get("MaxPrice") if "MaxPrice" in p else p.get("maxPrice")

        if status == "no_data":
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
    send_test_alert_email()
    return {"detail": "Test alert email sent"}


@app.get("/trigger-daily-alert")
def trigger_daily_alert(background_tasks: BackgroundTasks):
    if not ALERTS_ENABLED:
        return {"detail": "Alerts are currently disabled via environment"}

    system_enabled = get_config_bool("ALERTS_SYSTEM_ENABLED", True)
    if not system_enabled:
        return {"detail": "Alerts are currently disabled in admin config"}

    background_tasks.add_task(run_all_alerts_cycle)
    return {"detail": "Alerts cycle queued"}

# ===== END SECTION: ROOT, HEALTH AND ROUTES =====


# =======================================
# SECTION: MAIN SEARCH ROUTES
# =======================================

@app.post("/search-business")
def search_business(params: SearchParams, background_tasks: BackgroundTasks):
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
        "ALERTS_SYSTEM_ENABLED": get_config_bool("ALERTS_SYSTEM_ENABLED", True),
        "ALERTS_ENABLED_ENV": ALERTS_ENABLED,
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

# =====================================================
# MODELS FOR ALERT DATE SUMMARY ENDPOINT
# =====================================================

class AlertDateSummaryItem(BaseModel):
    departureDate: date
    returnDate: date
    flightCount: int
    minPrice: int
    maxPrice: int


class AlertDateSummaryResponse(BaseModel):
    dates: List[AlertDateSummaryItem]


# ===== BEGIN: LATEST ALERT RUN ENDPOINT =====

@app.get("/alerts/{alert_id}/latest-run")
def get_latest_alert_run(
    alert_id: str,
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    db = SessionLocal()
    try:
        resolved_email: Optional[str] = None
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if not resolved_email:
            raise HTTPException(
                status_code=400,
                detail="Email is required either as query parameter or via an AppUser mapped to X-User-Id",
            )

        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id)
            .filter(Alert.user_email == resolved_email)
            .first()
        )
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        latest_run = (
            db.query(AlertRun)
            .filter(AlertRun.alert_id == alert.id)
            .order_by(AlertRun.run_at.desc())
            .first()
        )

        base_alert_payload = {
            "origin": alert.origin,
            "destination": alert.destination,
            "cabin": alert.cabin,
            "alertType": alert.alert_type,
            "maxPrice": alert.max_price,
            "isActive": alert.is_active,
            "timesSentTotal": alert.times_sent,
            "lastPriceStored": alert.last_price,
            "departureStart": alert.departure_start.isoformat(),
            "departureEnd": alert.departure_end.isoformat(),
            "returnStart": alert.return_start.isoformat() if alert.return_start else None,
            "returnEnd": alert.return_end.isoformat() if alert.return_end else None,
        }

        if not latest_run:
            return {
                "alertId": alert.id,
                "hasRun": False,
                "alert": base_alert_payload,
                "run": None,
            }

        price_part = (
            f"£{latest_run.price_found}"
            if latest_run.price_found is not None
            else "no price captured"
        )
        reason_part = latest_run.reason or "no reason stored"

        summary_text = (
            f"Route: {alert.origin} \u2192 {alert.destination}, "
            f"{alert.cabin.title()} class, "
            f"last run at {latest_run.run_at.isoformat()}, "
            f"latest price {price_part}, reason: {reason_part}"
        )

        return {
            "alertId": alert.id,
            "hasRun": True,
            "alert": base_alert_payload,
            "run": {
                "runId": latest_run.id,
                "runAt": latest_run.run_at.isoformat(),
                "emailSent": latest_run.sent,
                "sendReason": latest_run.reason,
                "priceFound": latest_run.price_found,
                "currency": "GBP",
                "summaryText": summary_text,
            },
        }
    finally:
        db.close()

# ===== END: LATEST ALERT RUN ENDPOINT =====


# ===== BEGIN: ALERT DATE SUMMARY ENDPOINT =====

@app.get("/alerts/{alert_id}/date-summary", response_model=AlertDateSummaryResponse)
def get_alert_date_summary(
    alert_id: str,
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    db = SessionLocal()
    try:
        # resolve email exactly the same way as latest-run
        resolved_email: Optional[str] = None
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if not resolved_email:
            raise HTTPException(
                status_code=400,
                detail="Email is required either as query parameter or via an AppUser mapped to X-User-Id",
            )

        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id)
            .filter(Alert.user_email == resolved_email)
            .first()
        )
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        # placeholder for now, frontend expects this shape
        return AlertDateSummaryResponse(dates=[])
    finally:
        db.close()

# ===== END: ALERT DATE SUMMARY ENDPOINT =====

# =======================================
# SECTION: ALERT ROUTES
# =======================================

@app.post("/alerts", response_model=AlertOut)
def create_alert(payload: AlertCreate):
    db = SessionLocal()
    try:
        alert_id = str(uuid4())
        now = datetime.utcnow()

        # Decide mode from payload, default to "single"
        mode_value = payload.mode or "single"
        if mode_value not in ("single", "smart"):
            raise HTTPException(
                status_code=400,
                detail="Invalid alert mode, expected 'single' or 'smart'",
            )

        alert = Alert(
            id=alert_id,
            user_email=payload.email,
            origin=payload.origin,
            destination=payload.destination,
            cabin=payload.cabin,
            search_mode=payload.search_mode or "flexible",
            departure_start=payload.departure_start,
            departure_end=payload.departure_end,
            return_start=payload.return_start,
            return_end=payload.return_end,
            alert_type=payload.alert_type,
            max_price=payload.max_price,
            mode=mode_value,
            last_price=None,
            last_run_at=None,
            times_sent=0,
            is_active=True,
            created_at=now,
            updated_at=now,
        )

        db.add(alert)
        db.commit()
        db.refresh(alert)

        return AlertOut(
    id=alert.id,
    email=alert.user_email,
    origin=alert.origin,
    destination=alert.destination,
    cabin=alert.cabin,
    search_mode=alert.search_mode,
    departure_start=alert.departure_start,
    departure_end=alert.departure_end,
    return_start=alert.return_start,
    return_end=alert.return_end,
    alert_type=alert.alert_type,
    max_price=alert.max_price,
    mode=alert.mode,
    times_sent=alert.times_sent,
    is_active=alert.is_active,
    last_price=alert.last_price,
    created_at=alert.created_at,
    updated_at=alert.updated_at,
)
    finally:
        db.close()


@app.get("/alerts", response_model=List[AlertOut])
def get_alerts(
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    include_inactive: bool = False,
):
    """
    Return list of alerts for a user.
    Priority:
      1. email query parameter
      2. email resolved from X-User-Id via AppUser

    include_inactive:
      false (default): return only active alerts
      true: return all alerts for the user
    """

    resolved_email: Optional[str] = None

    db = SessionLocal()
    try:
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if not resolved_email:
            raise HTTPException(
                status_code=400,
                detail="Email is required either as query parameter or via an AppUser mapped to X-User-Id",
            )

        query = db.query(Alert).filter(Alert.user_email == resolved_email)

        if not include_inactive:
            query = query.filter(Alert.is_active == True)  # noqa: E712

                alerts = query.order_by(Alert.created_at.desc()).all()

        return [
            AlertOut(
                id=a.id,
                email=a.user_email,
                origin=a.origin,
                destination=a.destination,
                cabin=a.cabin,
                search_mode=a.search_mode,
                departure_start=a.departure_start,
                departure_end=a.departure_end,
                return_start=a.return_start,
                return_end=a.return_end,
                alert_type=a.alert_type,
                max_price=a.max_price,
                mode=a.mode,
                times_sent=a.times_sent,
                is_active=a.is_active,
                last_price=a.last_price,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            for a in alerts
        ]
    finally:
        db.close()

@app.patch("/alerts/{alert_id}")
def update_alert(
    alert_id: str,
    payload: AlertUpdatePayload,
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    db = SessionLocal()
    try:
        resolved_email: Optional[str] = None
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if not resolved_email:
            raise HTTPException(
                status_code=400,
                detail="Email is required either as query parameter or via an AppUser mapped to X-User-Id",
            )

        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id)
            .filter(Alert.user_email == resolved_email)
            .first()
        )
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        if payload.alert_type is not None:
            alert.alert_type = payload.alert_type

        if payload.max_price is not None:
            alert.max_price = payload.max_price

        if payload.is_active is not None:
            alert.is_active = payload.is_active

        if payload.departure_start is not None:
            alert.departure_start = payload.departure_start

        if payload.departure_end is not None:
            alert.departure_end = payload.departure_end

        if payload.return_start is not None:
            alert.return_start = payload.return_start

        if payload.return_end is not None:
            alert.return_end = payload.return_end

        if payload.mode is not None:
            if payload.mode not in ("single", "smart"):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid alert mode, expected 'single' or 'smart'",
                )
            alert.mode = payload.mode

        alert.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(alert)

        return {
            "status": "ok",
            "alert": AlertOut(
                id=alert.id,
                email=alert.user_email,
                origin=alert.origin,
                destination=alert.destination,
                cabin=alert.cabin,
                departure_start=alert.departure_start,
                departure_end=alert.departure_end,
                return_start=alert.return_start,
                return_end=alert.return_end,
                alert_type=alert.alert_type,
                max_price=alert.max_price,
                mode=alert.mode,
                times_sent=alert.times_sent,
                is_active=alert.is_active,
                last_price=alert.last_price,
                created_at=alert.created_at,
                updated_at=alert.updated_at,
            ),
        }
    finally:
        db.close()


@app.patch("/alerts/{alert_id}/status")
def update_alert_status(
    alert_id: str,
    payload: AlertStatusPayload,
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """
    Simple status toggle endpoint.
    Accepts JSON body with is_active or isActive.
    """
    db = SessionLocal()
    try:
        incoming_is_active = payload.is_active
        if incoming_is_active is None:
            incoming_is_active = payload.isActive

        if incoming_is_active is None:
            raise HTTPException(
                status_code=400,
                detail="is_active is required in body as is_active or isActive",
            )

        resolved_email: Optional[str] = None
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if not resolved_email:
            raise HTTPException(
                status_code=400,
                detail="Email is required either as query parameter or via an AppUser mapped to X-User-Id",
            )

        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id)
            .filter(Alert.user_email == resolved_email)
            .first()
        )
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        alert.is_active = incoming_is_active
        alert.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(alert)

        return {
            "status": "ok",
            "id": alert.id,
            "is_active": alert.is_active,
        }
    finally:
        db.close()


@app.delete("/alerts/{alert_id}")
def delete_alert(
    alert_id: str,
    email: Optional[str] = None,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    db = SessionLocal()
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        resolved_email: Optional[str] = None
        if email is not None:
            resolved_email = email
        elif x_user_id:
            app_user = (
                db.query(AppUser)
                .filter(AppUser.external_id == x_user_id)
                .first()
            )
            if app_user and app_user.email:
                resolved_email = app_user.email

        if resolved_email and alert.user_email != resolved_email:
            raise HTTPException(status_code=403, detail="Alert does not belong to this user")

        alert.is_active = False
        alert.updated_at = datetime.utcnow()
        db.commit()

        return {"status": "ok", "id": alert_id}
    finally:
        db.close()

# ===== END SECTION: ALERT ROUTES =====
