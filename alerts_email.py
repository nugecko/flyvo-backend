import os
from datetime import datetime
from typing import List, Dict, Tuple, Any

from fastapi import HTTPException

# Local types are only used for type hints, so we do not need hard imports here.
# The caller passes real Alert, FlightOption and SearchParams objects.
# from models import Alert
# from main import FlightOption, SearchParams

# Basic SMTP config, read from environment
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.smtp2go.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "alert@flyyv.com")

def send_alert_email_for_alert(alert, cheapest, params) -> None:
    """
    One off alert email:
    single date pair, using the same behaviour as before.
    """
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_FROM_EMAIL):
        raise HTTPException(
            status_code=500,
            detail="SMTP settings are not fully configured on the server",
        )

    to_email = alert.user_email
    if not to_email:
        raise HTTPException(status_code=500, detail="Alert has no user_email")

    subject = (
        f"Flyyv Alert: {alert.origin} \u2192 {alert.destination} "
        f"from £{int(cheapest.price)}"
    )

    dep_dt = datetime.fromisoformat(cheapest.departureDate)
    ret_dt = datetime.fromisoformat(cheapest.returnDate)
    dep_label = dep_dt.strftime("%d-%m-%Y")
    ret_label = ret_dt.strftime("%d-%m-%Y")

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

# FRONTEND_BASE_URL must come from environment,
# same pattern used in the main file.
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "https://flyyv.com")


def build_flyyv_link(params, departure: str, return_date: str) -> str:
    base = FRONTEND_BASE_URL.rstrip("/")
    return (
        f"{base}/?origin={params.origin}"
        f"&destination={params.destination}"
        f"&departure={departure}"
        f"&return={return_date}"
        f"&cabin={params.cabin}"
        f"&passengers={params.passengers}"
    )


def send_smart_alert_email(alert, options: List, params) -> None:
    """
    FlyyvFlex Monitor email:
    scans multiple date pairs in a flexible window and produces a summary message.
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
    grouped: Dict[Tuple[str, str], List] = {}
    for opt in options:
        key = (opt.departureDate, opt.returnDate)
        grouped.setdefault(key, []).append(opt)

    # Sort pairs by date
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

        flights_under: List = []
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

    # Date labels
    start_label = params.earliestDeparture.strftime("%d %B %Y")
    end_label = params.latestDeparture.strftime("%d %B %Y")

    # Nights label, based on stay range in params
    nights_text = None
    min_nights = getattr(params, "minStayDays", None)
    max_nights = getattr(params, "maxStayDays", None)
    if isinstance(min_nights, int) and isinstance(max_nights, int):
        if min_nights == max_nights:
            nights_text = f"{min_nights}"
        else:
            nights_text = f"{min_nights} to {max_nights}"

    combinations_checked = len(pairs_summary)

    # Cheapest price across all options
    cheapest_price_overall = None
    if options:
        cheapest_overall = min(options, key=lambda o: o.price)
        try:
            cheapest_price_overall = int(cheapest_overall.price)
        except Exception:
            cheapest_price_overall = None

    # Subject
    if cheapest_price_overall is not None:
        subject = (
            f"FlyyvFlex Alert: {origin} \u2192 {destination} "
            f"from £{cheapest_price_overall}"
        )
    elif threshold is not None and any_under:
        subject = (
            f"FlyyvFlex Alert: {origin} \u2192 {destination} "
            f"fares under £{int(threshold)}"
        )
    elif threshold is not None:
        subject = (
            f"FlyyvFlex Alert: {origin} \u2192 {destination} "
            f"no fares under £{int(threshold)}"
        )
    else:
        subject = f"FlyyvFlex Alert: {origin} \u2192 {destination} update"

    lines: List[str] = []

    # Header
    lines.append(
        f"FlyyvFlex Monitor: {origin} → {destination}, "
        f"{alert.cabin.title()} class"
    )
    if nights_text:
        lines.append(f"Nights: {nights_text}")
    lines.append(f"Date window: {start_label} to {end_label}")
    lines.append(f"Possible combinations checked: {combinations_checked}")
    lines.append("")

    # Intro
    if threshold is not None:
        lines.append(f"Max budget: £{int(threshold)}")
    lines.append("")
    lines.append(
        "We scanned all matching date combinations in your selected window "
        "and highlighted the best fares available right now."
    )
    lines.append("")

    # Select top options
    top_pairs = [
        p for p in pairs_summary
        if p.get("totalFlights", 0) > 0 and p.get("cheapestPrice") is not None
    ]

    if not top_pairs:
        lines.append("No flights were found in this window in the latest scan.")
    else:
        MAX_RESULTS = 10
        top_pairs_sorted = sorted(top_pairs, key=lambda x: x["cheapestPrice"])[:MAX_RESULTS]

        lines.append("Top flight deals in your FlyyvFlex window:")
        lines.append("")

        for p in top_pairs_sorted:
            dep_dt = datetime.fromisoformat(p["departureDate"])
            ret_dt = datetime.fromisoformat(p["returnDate"])
            dep_label = dep_dt.strftime("%d %b")
            ret_label = ret_dt.strftime("%d %b")

            price_label = int(p["cheapestPrice"])
            airline_label = p.get("cheapestAirline") or "Multiple airlines"

            line = f"£{price_label}, {dep_label} → {ret_label}, {airline_label}"

            if threshold is not None and float(price_label) <= float(threshold):
                line += "  (within your limit)"

            lines.append(line)

    lines.append("")
    lines.append("View and manage your alerts:")
    lines.append("https://flyyv.com")
    lines.append("")
    lines.append("You are receiving this email because you created a FlyyvFlex Monitor alert.")
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
