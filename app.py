"""
Invoice Data Collection App (Streamlit)
Collects invoice details, auto-computes GST + totals + amount in words,
appends each entry to a local CSV, and POSTs it to a Make.com webhook.
"""

import json
import os
import re
import time
import datetime as dt

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "invoices.csv")
PENDING_PATH = os.path.join(DATA_DIR, "webhook_pending.jsonl")

WEBHOOK_URL_DEFAULT = "https://hook.eu2.make.com/k9fkxrsr1xf9979ouo7yg1kgjv6fap6c"
WEBHOOK_TIMEOUT = 15
WEBHOOK_RETRIES = 3

# Branding. The logo is found by name, so the extension does not matter
# (.png / .svg / .jpg all work) and it can sit either next to app.py or in an
# assets/ subfolder.
LOGO_STEM = "new digipanda logo-02"
LOGO_EXTS = (".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif")
LOGO_DIRS = (".", "assets")
LOGO_INK_COLOR = "#1f2933"

# Column headers kept EXACTLY as requested (including the odd spacing) so the
# CSV and the webhook payload map 1:1 onto your existing sheet.
COLUMNS = [
    "Timestamp", "Date", "Invoice_No", "Client_Name", "Client_Address",
    "Client_GST", "State", "GST_Code",
    "Service_1", "Discription_1", "Amount_1",
    "Service_2", "Discription_2", "Amount_2",
    "total_amount", "gst", "total_amount+ gst",
    "Final Amount in words ", "Your Email Id",
    # Appended at the end so existing column positions do not shift.
    "HSN_1", "HSN_2",
]

# State -> GST state code. Stored zero-padded ("06", not "6") because that is
# what the first two digits of a real GSTIN look like, which lets us validate
# the client's GSTIN against the selected state.
# Want unpadded numbers in the sheet instead? Change the GST_Code line in the
# save block to:  int(STATE_CODES[state])
STATE_CODES = {
    "Andhra Pradesh": "37",
    "Arunachal Pradesh": "12",
    "Assam": "18",
    "Bihar": "10",
    "Chandigarh": "04",
    "Chhattisgarh": "22",
    "Delhi": "07",
    "Goa": "30",
    "Gujarat": "24",
    "Haryana": "06",
    "Himachal Pradesh": "02",
    "Jammu & Kashmir": "01",
    "Jharkhand": "20",
    "Karnataka": "29",
    "Kerala": "32",
    "Madhya Pradesh": "23",
    "Maharashtra": "27",
    "Odisha": "21",
    "Punjab": "03",
    "Rajasthan": "08",
    "Tamil Nadu": "33",
    "Telangana": "36",
    "Uttar Pradesh": "09",
    "Uttarakhand": "05",
    "West Bengal": "19",
}
STATES = sorted(STATE_CODES.keys())

# Service catalogue. Each category carries its SAC code, so picking a service
# fills HSN/SAC automatically.
#
# SAC codes for Social and ORM were not in the list you supplied - both are
# defaulted to 998361 (advertising services). The HSN/SAC field stays editable
# on every line, so confirm these two with your CA and override if needed.
NO_SERVICE = "— none —"

SERVICE_CATALOG = {
    "Website Development": {
        "sac": "998313",
        "options": [
            "Website development",
            "Website design - UI/UX only",
            "Website revamp",
            "Website changes",
        ],
    },
    "Performance Marketing": {
        "sac": "998361",
        "options": [
            "Meta ads",
            "Google ads",
            "Meta and Google",
            "LinkedIn",
            "Meta, Google and LinkedIn",
            "Google and LinkedIn",
        ],
    },
    "GEO Services": {
        "sac": "998313",
        "options": [
            "GEO - Launchpad",
            "GEO - Ascend",
            "GEO - Dominate",
        ],
    },
    "Social Media": {
        "sac": "998361",
        "options": [
            "SMM with shoots",
            "SMM without shoots",
        ],
    },
    "ORM": {
        "sac": "998361",
        "options": [
            "ORM - custom, platform wise",
            "ORM - Reddit and Quora",
        ],
    },
    "AI Voice Agent": {
        "sac": "998311",
        "options": ["AI Voice Agent - Svaarico"],
    },
    "AI Automation": {
        "sac": "998311",
        "options": ["AI Automation"],
    },
}

CATEGORIES = list(SERVICE_CATALOG.keys())

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
EMAIL_RE = re.compile(r"^[\w\.\+\-]+@[\w\-]+\.[\w\.\-]+$")

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Timestamps are pinned to IST rather than the machine clock, so the recorded
# time is right whether this runs on your laptop or on a UTC cloud host.
# Windows ships no tz database, so fall back to a fixed +05:30 offset if the
# optional `tzdata` package is not installed.
try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ImportError
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def parse_timestamps(series: pd.Series) -> pd.Series:
    """Parse the Timestamp column, tolerating rows written by older versions."""
    parsed = pd.to_datetime(series, format=TS_FORMAT, errors="coerce")
    missing = parsed.isna() & series.astype(str).str.strip().ne("")
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(
            series[missing], errors="coerce", dayfirst=True
        )
    return parsed


# ----------------------------------------------------------------------------
# NUMBER -> INDIAN WORDS
# ----------------------------------------------------------------------------

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
    "Sixteen", "Seventeen", "Eighteen", "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
    "Eighty", "Ninety",
]


def _two(n: int) -> str:
    """0-99 in words."""
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three(n: int) -> str:
    """0-999 in words."""
    if n < 100:
        return _two(n)
    head = _ONES[n // 100] + " Hundred"
    tail = _two(n % 100)
    return f"{head} {tail}".strip()


def amount_in_words(amount: float) -> str:
    """Convert a rupee amount to Indian-system words (crore / lakh / thousand)."""
    amount = round(float(amount) + 1e-9, 2)
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))

    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"

    parts = []
    crore = rupees // 10_000_000
    lakh = (rupees // 100_000) % 100
    thousand = (rupees // 1_000) % 100
    hundreds = rupees % 1_000

    if crore:
        # supports very large values by recursing on the crore count
        parts.append(f"{amount_in_words(crore).replace('Rupees ', '').replace(' Only', '')} Crore"
                     if crore > 999 else f"{_three(crore)} Crore")
    if lakh:
        parts.append(f"{_two(lakh)} Lakh")
    if thousand:
        parts.append(f"{_two(thousand)} Thousand")
    if hundreds:
        parts.append(_three(hundreds))

    words = "Rupees " + " ".join(p for p in parts if p).strip()
    if paise:
        words += f" and {_two(paise)} Paise"
    return words + " Only"


# ----------------------------------------------------------------------------
# STORAGE
# ----------------------------------------------------------------------------

def ensure_store() -> None:
    """Create the CSV if missing, and upgrade its header if the schema changed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        pd.DataFrame(columns=COLUMNS).to_csv(CSV_PATH, index=False)
        return

    # A CSV written by an older version has no GST_Code column. Appending new
    # rows to it would shift every field one place, so migrate it in place and
    # backfill GST_Code from the State already stored on each row.
    try:
        existing = pd.read_csv(CSV_PATH, dtype=str)
    except pd.errors.EmptyDataError:
        pd.DataFrame(columns=COLUMNS).to_csv(CSV_PATH, index=False)
        return

    if list(existing.columns) == COLUMNS:
        return

    for col in COLUMNS:
        if col not in existing.columns:
            existing[col] = ""
    if "State" in existing.columns:
        blank = existing["GST_Code"].isna() | (existing["GST_Code"].astype(str).str.strip() == "")
        existing.loc[blank, "GST_Code"] = (
            existing.loc[blank, "State"].map(STATE_CODES).fillna("")
        )
    existing[COLUMNS].to_csv(CSV_PATH, index=False)


@st.cache_data(ttl=5)
def load_data() -> pd.DataFrame:
    ensure_store()
    try:
        return pd.read_csv(CSV_PATH, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=COLUMNS)


def one_line(value: str) -> str:
    """Collapse newlines and tabs so one record is always one line in the CSV.

    Embedded newlines are legal CSV, but they break the moment anything other
    than pandas touches the file (Excel, Notepad, a OneDrive conflict merge, a
    naive line-by-line reader), which silently loses or splits records.
    """
    return re.sub(r"\s*[\r\n\t]+\s*", ", ", str(value)).strip().strip(",").strip()


def append_row(row: dict) -> None:
    """Append one record, then confirm it is really on disk.

    A silent write failure - file locked by Excel, OneDrive mid-sync, no disk
    permission - would otherwise look like a successful save while the record
    vanishes.
    """
    ensure_store()
    clean = {c: one_line(row.get(c, "")) for c in COLUMNS}

    try:
        pd.DataFrame([clean], columns=COLUMNS).to_csv(
            CSV_PATH, mode="a", header=False, index=False
        )
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot write {CSV_PATH} - it is open in another program "
            f"(Excel?). Close it and save again. ({exc})"
        ) from exc

    load_data.clear()

    check = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    if clean["Invoice_No"] not in set(check["Invoice_No"].astype(str).str.strip()):
        raise RuntimeError(
            f"{clean['Invoice_No']} did not persist to {CSV_PATH}. "
            "If this folder is inside OneDrive or Dropbox, move the project to "
            "a local path such as C:\\dev\\invoice-app."
        )


def fy_label(d: dt.date) -> str:
    """Indian financial year label, e.g. 26-27 for a date in Aug 2026."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{str(start)[2:]}-{str(start + 1)[2:]}"


def last_sequence(prefix: str = "", fy: str = "") -> int:
    """Highest sequence number already used.

    Reads the trailing number of every saved Invoice_No, so it keeps working
    even if the prefix or financial year changed part-way through. Pass
    prefix/fy to count only invoices from that series (used when numbering
    restarts each financial year).
    """
    df = load_data()
    if df.empty or "Invoice_No" not in df.columns:
        return 0

    series = df["Invoice_No"].astype(str)
    if prefix or fy:
        series = series[series.str.startswith(f"{prefix}/{fy}/")]

    seq = 0
    for val in series:
        m = re.search(r"(\d+)\s*$", val.strip())
        if m:
            seq = max(seq, int(m.group(1)))
    return seq


def next_invoice_no(prefix: str, d: dt.date, reset_each_fy: bool = False) -> str:
    """Always one higher than the last invoice saved. Never reuses a number."""
    fy = fy_label(d)
    if reset_each_fy:
        seq = last_sequence(prefix, fy)
    else:
        seq = last_sequence()
    return f"{prefix}/{fy}/{seq + 1:03d}"


# ----------------------------------------------------------------------------
# BRANDING
# ----------------------------------------------------------------------------

def find_logo() -> str | None:
    """Locate the logo file without caring about its extension or casing."""
    # 1. exact stem + known extension
    for folder in LOGO_DIRS:
        for ext in LOGO_EXTS:
            candidate = os.path.join(folder, LOGO_STEM + ext)
            if os.path.exists(candidate):
                return candidate

    # 2. same stem but different casing / stray spaces
    target = LOGO_STEM.replace(" ", "").lower()
    for folder in LOGO_DIRS:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(name)
            if ext.lower() in LOGO_EXTS and stem.replace(" ", "").lower() == target:
                return os.path.join(folder, name)

    # 3. last resort: anything with "logo" in the filename
    for folder in LOGO_DIRS:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            stem, ext = os.path.splitext(name)
            if ext.lower() in LOGO_EXTS and "logo" in stem.lower():
                return os.path.join(folder, name)
    return None


def ink_variant(svg_path: str) -> str | None:
    """A white logo is invisible on a light background. For SVGs we can fix
    that automatically by recolouring the fills; returns None if the artwork
    was not white to begin with (or is not an SVG)."""
    if not svg_path.lower().endswith(".svg"):
        return None

    out_path = f"{os.path.splitext(svg_path)[0]}-ink.svg"
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(svg_path):
        return out_path

    try:
        with open(svg_path, encoding="utf-8") as fh:
            svg = fh.read()
    except (OSError, UnicodeDecodeError):
        return None

    whites = ('"#fff"', '"#ffffff"', '"#FFF"', '"#FFFFFF"', '"white"')
    if not any(f"fill={w}" in svg for w in whites):
        return None  # already dark, nothing to do

    for white in whites:
        svg = svg.replace(f"fill={white}", f'fill="{LOGO_INK_COLOR}"')
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(svg)
    except OSError:
        return None
    return out_path


def resolve_logo() -> str | None:
    """The file to render: white original on dark themes, ink copy on light."""
    logo = find_logo()
    if not logo:
        return None
    try:
        is_dark = st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001 - older Streamlit lacks st.context.theme
        is_dark = False
    if is_dark:
        return logo
    return ink_variant(logo) or logo


# ----------------------------------------------------------------------------
# WEBHOOK DELIVERY
# ----------------------------------------------------------------------------

def build_payload(row: dict, gst_rate: float, intra: bool, gst_amount: float) -> dict:
    """Flat keys match the sheet columns 1:1; `meta` carries the derived extras.

    NOTE: Make.com locks a webhook's data structure to the shape of the first
    payload it receives. Keep this shape stable, or click "Redetermine data
    structure" in the Make scenario after changing it.
    """
    half = round(gst_amount / 2, 2)
    return {
        **{k: str(v) for k, v in row.items() if not k.startswith("_")},
        "meta": {
            "gst_rate": gst_rate,
            "supply_type": "Intra-state" if intra else "Inter-state",
            "cgst": f"{half:.2f}" if intra else "0.00",
            "sgst": f"{gst_amount - half:.2f}" if intra else "0.00",
            "igst": "0.00" if intra else f"{gst_amount:.2f}",
            "source": "streamlit-invoice-app",
            "category_1": row.get("_cat_1", ""),
            "category_2": row.get("_cat_2", ""),
        },
    }


def post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    """POST JSON with retries. Returns (ok, message)."""
    if not url.strip():
        return False, "No webhook URL configured."

    last_error = "unknown error"
    for attempt in range(WEBHOOK_RETRIES):
        try:
            resp = requests.post(
                url.strip(),
                json=payload,
                timeout=WEBHOOK_TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code < 300:
                body = (resp.text or "").strip()[:120]
                return True, body or f"HTTP {resp.status_code}"
            last_error = f"HTTP {resp.status_code}: {(resp.text or '')[:120]}"
        except requests.exceptions.Timeout:
            last_error = f"timed out after {WEBHOOK_TIMEOUT}s"
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)[:200]

        if attempt < WEBHOOK_RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))  # simple backoff

    return False, last_error


def queue_pending(payload: dict) -> None:
    """Park a failed payload on disk so nothing is lost when Make is down."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PENDING_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def pending_count() -> int:
    if not os.path.exists(PENDING_PATH):
        return 0
    with open(PENDING_PATH, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def flush_pending(url: str) -> tuple[int, int]:
    """Retry every queued payload. Returns (sent, still_failing)."""
    if not os.path.exists(PENDING_PATH):
        return 0, 0

    with open(PENDING_PATH, encoding="utf-8") as fh:
        items = [json.loads(line) for line in fh if line.strip()]

    sent, still_failing = 0, []
    for payload in items:
        ok, _ = post_webhook(url, payload)
        if ok:
            sent += 1
        else:
            still_failing.append(payload)

    with open(PENDING_PATH, "w", encoding="utf-8") as fh:
        for payload in still_failing:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return sent, len(still_failing)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

# find_logo() and ink_variant() are pure file IO, so they are safe to run
# before set_page_config(), which must be the first Streamlit command.
_logo_file = find_logo()
_favicon = (ink_variant(_logo_file) or _logo_file) if _logo_file else "🧾"

st.set_page_config(
    page_title="Invoice Data Entry",
    page_icon=_favicon,
    layout="wide",
)
ensure_store()

BRAND_LOGO = resolve_logo()
if BRAND_LOGO:
    # Pinned to the top-left, above the sidebar. Stays visible on every tab and
    # even when the sidebar is collapsed.
    st.logo(BRAND_LOGO, size="large")
else:
    st.sidebar.warning(
        f'Logo not found. Put "{LOGO_STEM}.png" (or .svg) next to app.py.'
    )

with st.sidebar:
    st.header("Settings")
    home_state = st.selectbox(
        "Your business state (place of supply origin)",
        STATES,
        index=STATES.index("Haryana"),
        help="Same state as client -> CGST + SGST. Different state -> IGST.",
    )
    gst_rate = st.number_input("GST rate (%)", 0.0, 28.0, 18.0, step=0.5)
    inv_prefix = st.text_input("Invoice prefix", value="DP")
    reset_each_fy = st.toggle(
        "Restart numbering each financial year",
        value=False,
        help="Off = numbers keep climbing forever. On = back to 001 every April.",
    )
    st.divider()
    st.subheader("Make.com webhook")
    send_webhook = st.toggle("Send each invoice to webhook", value=True)
    webhook_url = st.text_input(
        "Webhook URL", value=WEBHOOK_URL_DEFAULT, type="default"
    )

    if st.button("🔌 Send test payload", width="stretch"):
        ok, msg = post_webhook(
            webhook_url,
            {
                **{c: "" for c in COLUMNS},
                "Invoice_No": "TEST/0000",
                "Client_Name": "Webhook Test",
                "meta": {"source": "streamlit-invoice-app", "test": True},
            },
        )
        (st.success if ok else st.error)(
            f"{'Webhook reachable' if ok else 'Webhook failed'} — {msg}"
        )

    queued = pending_count()
    if queued:
        st.warning(f"{queued} invoice(s) not yet delivered.")
        if st.button(f"↻ Retry {queued} queued", width="stretch"):
            sent, failed = flush_pending(webhook_url)
            st.success(f"Sent {sent}." + (f" {failed} still failing." if failed else ""))
            st.rerun()

st.title("Invoice Data Entry")

tab_entry, tab_records = st.tabs(["New entry", "Records"])

with tab_entry:
    # Anything that writes widget state must run before those widgets exist,
    # hence the flag-then-rerun pattern used throughout this block.
    if st.session_state.pop("_advance_invoice", False):
        ref_date = st.session_state.get("inv_date", dt.date.today())
        st.session_state["invoice_no"] = next_invoice_no(
            inv_prefix, ref_date, reset_each_fy
        )

    flash = st.session_state.pop("_flash", None)
    if flash:
        st.success(flash)

    c1, c2, c3 = st.columns(3)
    with c1:
        inv_date = st.date_input(
            "Date", value=dt.date.today(), format="DD/MM/YYYY", key="inv_date"
        )
    with c2:
        auto_no = next_invoice_no(inv_prefix, inv_date, reset_each_fy)
        st.session_state.setdefault("invoice_no", auto_no)
        invoice_no = st.text_input(
            "Invoice No",
            key="invoice_no",
            help="Pre-filled with the next number in sequence. Edit it if you "
                 "need a custom one; it re-advances automatically after saving.",
        )
        if invoice_no.strip() != auto_no:
            if st.button(f"↻ Back to auto ({auto_no})", width="stretch"):
                st.session_state["_advance_invoice"] = True
                st.rerun()
    with c3:
        email = st.text_input("Your Email Id *", placeholder="you@company.com")

    st.subheader("Billing details")
    st.caption("Fields marked * are mandatory.")
    c1, c2 = st.columns([2, 1])
    with c1:
        billing_name = st.text_input("Billing name *")
        billing_address = st.text_area("Billing address *", height=100)
    with c2:
        state = st.selectbox("State (place of supply)", STATES, index=STATES.index("Haryana"))
        gst_code = STATE_CODES[state]
        st.text_input(
            "GST_Code (auto)",
            value=gst_code,
            disabled=True,
            help="Filled automatically from the selected state.",
        )
        client_gst = st.text_input(
            "Client GST *",
            placeholder=f"{gst_code}ABCDE1234F1Z5",
            help="Mandatory. 15-character GSTIN.",
        ).strip().upper()

    st.subheader("Line items")
    l1, l2 = st.columns(2)
    with l1:
        st.markdown("**Item 1**")
        cat_1 = st.selectbox("Category *", CATEGORIES, key="cat_1")
        service_1 = st.selectbox(
            "Service_1 *", SERVICE_CATALOG[cat_1]["options"], key="svc_1"
        )
        desc_1 = st.text_area("Discription_1", height=90, key="d1")
        amount_1 = st.number_input("Amount_1", min_value=0.0, step=500.0, format="%.2f")
    with l2:
        st.markdown("**Item 2** *(optional)*")
        cat_2 = st.selectbox("Category", [NO_SERVICE] + CATEGORIES, key="cat_2")
        if cat_2 == NO_SERVICE:
            service_2 = ""
            st.selectbox("Service_2", ["—"], disabled=True, key="svc_2_off")
            desc_2 = ""
            st.text_area("Discription_2", value="", height=90, disabled=True,
                         key="d2_off")
            amount_2 = 0.0
            st.number_input("Amount_2", value=0.0, disabled=True, key="amt_2_off",
                            format="%.2f")
        else:
            service_2 = st.selectbox(
                "Service_2", SERVICE_CATALOG[cat_2]["options"], key="svc_2"
            )
            desc_2 = st.text_area("Discription_2", height=90, key="d2")
            amount_2 = st.number_input(
                "Amount_2", min_value=0.0, step=500.0, format="%.2f"
            )

    # ---- HSN / SAC, derived from the chosen category ----
    st.subheader("HSN / SAC")
    st.caption(
        "Filled automatically from the service category. Edit if a particular "
        "invoice needs a different code."
    )
    h1, h2 = st.columns(2)
    with h1:
        hsn_1 = st.text_input(
            f"HSN/SAC — Item 1 ({cat_1})",
            value=SERVICE_CATALOG[cat_1]["sac"],
            key=f"hsn_1_{cat_1}",  # key includes the category so it refreshes
            max_chars=8,
        ).strip()
    with h2:
        if cat_2 == NO_SERVICE:
            hsn_2 = ""
            st.text_input("HSN/SAC — Item 2", value="", disabled=True,
                          key="hsn_2_off")
        else:
            hsn_2 = st.text_input(
                f"HSN/SAC — Item 2 ({cat_2})",
                value=SERVICE_CATALOG[cat_2]["sac"],
                key=f"hsn_2_{cat_2}",
                max_chars=8,
            ).strip()

    # ---- live calculation ----
    total_amount = round(amount_1 + amount_2, 2)
    gst_amount = round(total_amount * gst_rate / 100, 2)
    grand_total = round(total_amount + gst_amount, 2)
    words = amount_in_words(grand_total)
    intra = state == home_state

    st.subheader("Calculated")
    m1, m2, m3 = st.columns(3)
    m1.metric("Total amount", f"₹ {total_amount:,.2f}")
    m2.metric(f"GST @ {gst_rate:g}%", f"₹ {gst_amount:,.2f}")
    m3.metric("Total + GST", f"₹ {grand_total:,.2f}")

    if intra:
        half = round(gst_amount / 2, 2)
        st.caption(
            f"Intra-state supply → CGST ₹{half:,.2f} + SGST ₹{gst_amount - half:,.2f}"
        )
    else:
        st.caption(f"Inter-state supply → IGST ₹{gst_amount:,.2f}")

    st.info(f"**In words:** {words}")

    # ---- validation ----
    errors, warnings = [], []
    if not billing_name.strip():
        errors.append("Billing name is required.")
    if not billing_address.strip():
        errors.append("Billing address is required.")
    if not invoice_no.strip():
        errors.append("Invoice No is required.")
    else:
        saved = load_data()
        if not saved.empty and invoice_no.strip() in set(
            saved["Invoice_No"].astype(str).str.strip()
        ):
            errors.append(
                f"Invoice No {invoice_no.strip()} already exists. "
                "GST requires unique invoice numbers."
            )
    if total_amount <= 0:
        errors.append("Enter at least one amount greater than 0.")
    if not hsn_1 or not hsn_1.isdigit():
        errors.append("HSN/SAC for Item 1 must be numeric.")
    if cat_2 != NO_SERVICE:
        if amount_2 <= 0:
            errors.append(
                f"Item 2 is set to '{service_2}' but Amount_2 is 0. "
                "Set an amount or switch Category back to none."
            )
        if not hsn_2 or not hsn_2.isdigit():
            errors.append("HSN/SAC for Item 2 must be numeric.")
    if not email.strip():
        errors.append("Your Email Id is required.")
    elif not EMAIL_RE.match(email.strip()):
        errors.append("Email Id looks invalid.")
    if not client_gst:
        errors.append("Client GST is required.")
    elif not GSTIN_RE.match(client_gst):
        errors.append("Client GST is not a valid 15-character GSTIN.")
    elif client_gst[:2] != gst_code:
        warnings.append(
            f"GSTIN starts with {client_gst[:2]} but {state} is code "
            f"{gst_code}. Check the state."
        )

    for w in warnings:
        st.warning(w)

    if st.button("💾 Save invoice", type="primary", width="stretch"):
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {
                "Timestamp": now_ist().strftime(TS_FORMAT),
                "Date": inv_date.strftime("%d-%m-%Y"),
                "Invoice_No": invoice_no.strip(),
                "Client_Name": billing_name.strip(),
                "Client_Address": billing_address.strip(),
                "Client_GST": client_gst,
                "State": state,
                "GST_Code": gst_code,
                "Service_1": service_1.strip(),
                "HSN_1": hsn_1,
                "Discription_1": desc_1.strip(),
                "Amount_1": f"{amount_1:.2f}",
                "Service_2": service_2.strip(),
                "HSN_2": hsn_2,
                "Discription_2": desc_2.strip(),
                "Amount_2": f"{amount_2:.2f}",
                "total_amount": f"{total_amount:.2f}",
                "gst": f"{gst_amount:.2f}",
                "total_amount+ gst": f"{grand_total:.2f}",
                "Final Amount in words ": words,
                "Your Email Id": email.strip(),
            }
            try:
                append_row(row)
            except RuntimeError as exc:
                st.error(str(exc))
                st.stop()
            msgs = [f"Saved **{row['Invoice_No']}** for {row['Client_Name']}."]

            if send_webhook:
                payload = build_payload(
                    {**row, "_cat_1": cat_1,
                     "_cat_2": "" if cat_2 == NO_SERVICE else cat_2},
                    gst_rate, intra, gst_amount,
                )
                ok, detail = post_webhook(webhook_url, payload)
                if ok:
                    msgs.append(f"Sent to webhook ({detail}).")
                else:
                    # CSV already holds the row, and the payload is queued, so
                    # nothing is lost if Make is unreachable.
                    queue_pending(payload)
                    msgs.append(
                        f"⚠️ Webhook failed: {detail}. Queued for retry — use "
                        "the sidebar once it is back."
                    )

            # Messages have to survive the rerun, so stash them instead of
            # calling st.success() here.
            st.session_state["_flash"] = "  \n".join(msgs)
            st.session_state["_advance_invoice"] = True
            st.rerun()

with tab_records:
    df = load_data()
    if df.empty:
        st.info("No invoices saved yet.")
    else:
        # Split the stored Timestamp into readable date and time columns for
        # display. Timestamp itself stays untouched in the CSV and the webhook.
        df = df.reset_index(drop=True)
        ts = parse_timestamps(df["Timestamp"])
        df = df.assign(
            **{
                "Created Date": ts.dt.strftime("%d-%m-%Y").fillna(""),
                "Created Time": ts.dt.strftime("%H:%M:%S").fillna(""),
                "_ts": ts,
                # Position in the file breaks ties when several invoices share
                # the same second, so the genuinely newest row stays on top.
                "_pos": range(len(df)),
            }
        )

        first_cols = ["Created Date", "Created Time", "Invoice_No", "Client_Name"]
        hidden = {"_ts", "_pos"}
        ordered = first_cols + [
            c for c in df.columns if c not in first_cols and c not in hidden
        ]

        top = st.columns([4, 1], vertical_alignment="bottom")
        with top[0]:
            q = st.text_input("Search billing name / invoice no / GST")
        with top[1]:
            if st.button("↻ Refresh", width="stretch"):
                load_data.clear()
                st.rerun()

        view = df
        if q:
            mask = (
                df["Client_Name"].str.contains(q, case=False, na=False)
                | df["Invoice_No"].str.contains(q, case=False, na=False)
                | df["Client_GST"].str.contains(q, case=False, na=False)
            )
            view = df[mask]

        # Newest first: by creation time, then by file position for same-second
        # saves. Rows with an unparseable Timestamp sink to the bottom.
        view = view.sort_values(
            ["_ts", "_pos"], ascending=[False, False], na_position="last"
        )

        m1, m2, m3, m4 = st.columns(4)
        billed = pd.to_numeric(view["total_amount+ gst"], errors="coerce").sum()
        gst_collected = pd.to_numeric(view["gst"], errors="coerce").sum()
        m1.metric("Invoices shown", f"{len(view)} of {len(df)}")
        m2.metric("Total billed", f"₹ {billed:,.0f}")
        m3.metric("GST collected", f"₹ {gst_collected:,.0f}")
        valid_ts = df["_ts"].dropna()
        m4.metric(
            "First entry",
            valid_ts.min().strftime("%d-%m-%Y") if not valid_ts.empty else "—",
        )

        st.dataframe(
            view[ordered],
            width="stretch",
            height=520,
            hide_index=True,
        )

        st.download_button(
            "⬇️ Download all records (CSV)",
            data=df.sort_values(["_ts", "_pos"], ascending=[False, False],
                                na_position="last")[ordered]
            .to_csv(index=False)
            .encode("utf-8"),
            file_name=f"invoices_{dt.date.today():%Y%m%d}.csv",
            mime="text/csv",
            help="Every record ever saved, not just the filtered view.",
        )

        with st.expander("Storage check"):
            physical = sum(1 for _ in open(CSV_PATH, encoding="utf-8")) - 1
            st.write(
                {
                    "file": os.path.abspath(CSV_PATH),
                    "records parsed": len(df),
                    "data lines in file": physical,
                    "unparseable timestamps": int(df["_ts"].isna().sum()),
                }
            )
            if physical != len(df):
                st.warning(
                    "Line count does not match record count — the CSV was "
                    "probably edited outside this app."
                )
            conflicts = [
                f for f in os.listdir(DATA_DIR)
                if f.startswith("invoices") and f != os.path.basename(CSV_PATH)
            ]
            if conflicts:
                st.error(
                    "Sync conflict copies found in data/: "
                    + ", ".join(conflicts)
                    + ". Records may have been split across them. Move this "
                    "project out of OneDrive."
                )
                