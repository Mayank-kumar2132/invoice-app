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

# Anchored to app.py's own folder, NOT the working directory. With a plain
# relative "data" path the CSV lands wherever the terminal happened to be when
# `streamlit run` was typed, so launching from a different folder silently
# starts a fresh, empty history.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
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
LOGO_DIRS = (BASE_DIR, os.path.join(BASE_DIR, "assets"))
LOGO_INK_COLOR = "#1f2933"

# Exactly the columns in your Google Sheet, in sheet order. Reading, display
# and the flat webhook payload all key off this list, so it must stay in sync
# with the sheet's header row.
COLUMNS = [
    "Invoice_No", "Date", "Client_Name", "Client_Address", "Client_GST", "State",
    "Service_1", "Discription_1", "Amount_1",
    "Service_2", "Discription_2", "Amount_2",
    "Service_3", "Amount_3",
    "total_amount", "gst", "total",
    "Amount_In_Words", "Email", "Phone", "Trigger",
]

# Computed values the sheet has no column for. Sent flat in the payload anyway,
# so they can be mapped in Make the day you add columns for them.
EXTRA_PAYLOAD_FIELDS = [
    "Timestamp", "GST_Code", "HSN_1", "HSN_2", "HSN_3",
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


def parse_timestamps(df: pd.DataFrame) -> pd.Series:
    """Best available creation time for each row.

    The sheet has no Timestamp column, so fall back to the Date column
    (dd-mm-yyyy) - that still gives correct ordering, just no clock time.
    """
    if "Timestamp" in df.columns:
        raw = df["Timestamp"].astype(str)
        parsed = pd.to_datetime(raw, format=TS_FORMAT, errors="coerce")
    else:
        parsed = pd.Series(pd.NaT, index=df.index)

    missing = parsed.isna()
    if missing.any() and "Date" in df.columns:
        parsed.loc[missing] = pd.to_datetime(
            df.loc[missing, "Date"], format="%d-%m-%Y", errors="coerce"
        )

    missing = parsed.isna()
    if missing.any() and "Date" in df.columns:
        parsed.loc[missing] = pd.to_datetime(
            df.loc[missing, "Date"], errors="coerce", dayfirst=True
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

# ----------------------------------------------------------------------------
# STORAGE
#
# Two backends behind one interface. Google Sheets is the real store: Streamlit
# Cloud wipes its disk on every restart, so a local CSV cannot survive there.
# The CSV backend stays for offline / local-only use.
# ----------------------------------------------------------------------------

SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Your sheet. Overridable from secrets or the sidebar.
SHEET_ID_DEFAULT = "1KnDTMb00AaL8JBDS3UpXdRutJLzoPtTN37gHtE7RIRE"
WORKSHEET_DEFAULT = "Streamlit"


def extract_sheet_id(text: str) -> str:
    """Accept either a bare ID or a full Google Sheets URL."""
    text = (text or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    return match.group(1) if match else text


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def sheet_export_url(sheet_id: str, worksheet: str) -> str:
    """Read-only CSV export of one tab. Works with no credentials as long as
    the sheet is shared 'Anyone with the link - Viewer'."""
    from urllib.parse import quote

    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={quote(worksheet)}"
    )


@st.cache_data(ttl=30, show_spinner=False)
def load_sheet_public(sheet_id: str, worksheet: str) -> pd.DataFrame:
    """Read the tab over plain HTTP. No service account needed.

    Make writes the rows; this only reads them back for the Records tab and
    for working out the next invoice number.
    """
    import io

    resp = requests.get(sheet_export_url(sheet_id, worksheet), timeout=20)

    # A sheet that is not link-shared returns a sign-in HTML page, not CSV.
    text = resp.text or ""
    if resp.status_code != 200 or text.lstrip()[:1] == "<":
        raise RuntimeError(
            "Sheet is not readable by link. Open it → Share → General access → "
            "'Anyone with the link' → Viewer. Also check the tab name."
        )

    try:
        df = pd.read_csv(io.StringIO(text), dtype=str)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=COLUMNS)

    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    # Sheets often have trailing empty header cells; pandas names them
    # "Unnamed: 21" and they would otherwise show up as junk columns.
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("Unnamed")]]
    if "Invoice_No" not in df.columns:
        raise RuntimeError(
            f"Tab '{worksheet}' has no Invoice_No column. Header row must match "
            "the app's column names."
        )
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[df["Invoice_No"].astype(str).str.strip() != ""]
    return df[COLUMNS]


def one_line(value: str) -> str:
    """Collapse newlines and tabs so one record is always one row.

    Embedded newlines are legal CSV, but they break the moment anything other
    than pandas touches the file (Excel, Notepad, a OneDrive conflict merge, a
    naive line-by-line reader), which silently loses or splits records.
    """
    return re.sub(r"\s*[\r\n\t]+\s*", ", ", str(value)).strip().strip(",").strip()


def secret(key: str, default: str = "") -> str:
    """Read a secret without exploding when no secrets file exists at all."""
    try:
        return str(st.secrets.get(key, default))
    except Exception:  # noqa: BLE001 - StreamlitSecretNotFoundError
        return default


def has_service_account() -> bool:
    try:
        return "gcp_service_account" in st.secrets
    except Exception:  # noqa: BLE001
        return False


def storage_cfg() -> dict:
    """Backend settings, written by the sidebar before anything reads them."""
    return st.session_state.get("_storage", {"mode": "csv"})


# ---- Google Sheets backend --------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_worksheet(sheet_id: str, worksheet_name: str):
    """Authorised worksheet handle. Cached so each rerun does not re-auth."""
    import gspread
    from google.oauth2.service_account import Credentials

    if not has_service_account():
        raise RuntimeError(
            "No [gcp_service_account] block in secrets. Add the service account "
            "JSON to .streamlit/secrets.toml (or the Secrets box on Streamlit "
            "Cloud)."
        )

    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SHEET_SCOPES
    )
    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open_by_key(sheet_id)
    except gspread.SpreadsheetNotFound as exc:
        raise RuntimeError(
            f"Sheet {sheet_id} not found. Check the ID, and share the sheet "
            "with the service account email as Editor."
        ) from exc
    except gspread.exceptions.APIError as exc:
        raise RuntimeError(f"Google API error: {exc}") from exc

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            worksheet_name, rows=2000, cols=max(len(COLUMNS), 26)
        )

    # Make sure the header row exists and covers every column we write.
    header = worksheet.row_values(1)
    if not header:
        worksheet.update([COLUMNS], "A1")
    else:
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            worksheet.update([header + missing], "A1")

    return worksheet


@st.cache_data(ttl=60, show_spinner=False)
def load_sheet(sheet_id: str, worksheet_name: str) -> pd.DataFrame:
    """Every row from the sheet. Cached for 60s because Streamlit reruns on
    every keystroke and the Sheets API allows only 60 reads/minute."""
    worksheet = get_worksheet(sheet_id, worksheet_name)
    values = worksheet.get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=COLUMNS)

    header = values[0]
    width = len(header)
    rows = [r + [""] * (width - len(r)) for r in values[1:]]
    df = pd.DataFrame(rows, columns=header)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    # Drop fully blank rows - people leave gaps at the bottom of sheets.
    df = df[df["Invoice_No"].astype(str).str.strip() != ""]
    return df[COLUMNS].fillna("")


def append_sheet(clean: dict, sheet_id: str, worksheet_name: str) -> None:
    worksheet = get_worksheet(sheet_id, worksheet_name)
    header = worksheet.row_values(1)
    # Follow the sheet's own column order, not ours, so a reordered sheet
    # cannot shift every value one cell across.
    worksheet.append_row(
        [clean.get(h, "") for h in header], value_input_option="USER_ENTERED"
    )


def delete_sheet_rows(invoice_nos: list[str], sheet_id: str, worksheet_name: str) -> int:
    worksheet = get_worksheet(sheet_id, worksheet_name)
    values = worksheet.get_all_values()
    if not values:
        return 0
    header = values[0]
    if "Invoice_No" not in header:
        raise RuntimeError("Sheet has no Invoice_No column.")
    col = header.index("Invoice_No")
    targets = {n.strip() for n in invoice_nos}

    # Sheet rows are 1-indexed and row 1 is the header. Delete bottom-up so
    # earlier deletions do not shift the indices of later ones.
    hits = [
        i + 1
        for i, row in enumerate(values)
        if i > 0 and len(row) > col and row[col].strip() in targets
    ]
    for row_number in sorted(hits, reverse=True):
        worksheet.delete_rows(row_number)
    return len(hits)


# ---- CSV backend ------------------------------------------------------------

def ensure_store() -> None:
    """Create the CSV if missing, and upgrade its header if the schema changed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        pd.DataFrame(columns=COLUMNS).to_csv(CSV_PATH, index=False)
        return

    # A CSV written by an older version is missing newer columns. Appending to
    # it would shift every field one place, so migrate in place.
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
    existing[COLUMNS].to_csv(CSV_PATH, index=False)


@st.cache_data(ttl=5, show_spinner=False)
def load_csv() -> pd.DataFrame:
    ensure_store()
    try:
        return pd.read_csv(CSV_PATH, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=COLUMNS)


def append_csv(clean: dict) -> None:
    ensure_store()
    try:
        pd.DataFrame([clean], columns=COLUMNS).to_csv(
            CSV_PATH, mode="a", header=False, index=False
        )
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot write {CSV_PATH} - it is open in another program "
            f"(Excel?). Close it and save again. ({exc})"
        ) from exc


def delete_csv_rows(invoice_nos: list[str]) -> int:
    ensure_store()
    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    targets = {n.strip() for n in invoice_nos}
    keep = ~df["Invoice_No"].astype(str).str.strip().isin(targets)
    removed = int((~keep).sum())

    tmp = f"{CSV_PATH}.tmp"
    df[keep][COLUMNS].to_csv(tmp, index=False)
    os.replace(tmp, CSV_PATH)  # atomic, so a crash cannot truncate the file
    return removed


# ---- dispatch ---------------------------------------------------------------

def refresh_store() -> None:
    load_csv.clear()
    load_sheet.clear()
    load_sheet_public.clear()


def load_data() -> pd.DataFrame:
    cfg = storage_cfg()
    mode = cfg.get("mode")
    if mode == "sheet_link":
        return load_sheet_public(cfg["sheet_id"], cfg["worksheet"])
    if mode == "sheet":
        return load_sheet(cfg["sheet_id"], cfg["worksheet"])
    return load_csv()


def issued_this_session() -> set[str]:
    """Invoice numbers already sent to Make but perhaps not yet visible.

    Make writes to the sheet asynchronously, so for a few seconds after saving
    the sheet still shows the old maximum. Without this the next invoice would
    reuse the number that was just issued.
    """
    return set(st.session_state.get("_issued", []))


def mark_issued(invoice_no: str) -> None:
    st.session_state.setdefault("_issued", [])
    if invoice_no not in st.session_state["_issued"]:
        st.session_state["_issued"].append(invoice_no)


def wait_for_row(invoice_no: str, tries: int = 3, delay: float = 2.0) -> bool:
    """Poll the store briefly so Records reflects the new row right away."""
    for attempt in range(tries):
        time.sleep(delay if attempt else 1.0)
        refresh_store()
        try:
            df = load_data()
        except Exception:  # noqa: BLE001
            continue
        if invoice_no in set(df["Invoice_No"].astype(str).str.strip()):
            return True
    return False


def append_row(row: dict) -> None:
    """Write one record directly, then confirm it really landed.

    Not used in link mode - there the webhook is the only writer.
    """
    cfg = storage_cfg()
    clean = {c: one_line(row.get(c, "")) for c in COLUMNS}

    if cfg.get("mode") == "sheet":
        append_sheet(clean, cfg["sheet_id"], cfg["worksheet"])
    else:
        append_csv(clean)

    refresh_store()

    saved = load_data()
    if clean["Invoice_No"] not in set(
        saved["Invoice_No"].astype(str).str.strip()
    ):
        where = "the Google Sheet" if cfg.get("mode") == "sheet" else CSV_PATH
        raise RuntimeError(
            f"{clean['Invoice_No']} did not persist to {where}. Nothing was "
            "saved - check the connection and try again."
        )


def delete_rows(invoice_nos: list[str]) -> int:
    cfg = storage_cfg()
    mode = cfg.get("mode")
    if mode == "sheet_link":
        raise RuntimeError(
            "Link mode is read-only. Delete the row in the Google Sheet "
            "itself, then press Refresh."
        )
    if mode == "sheet":
        removed = delete_sheet_rows(invoice_nos, cfg["sheet_id"], cfg["worksheet"])
    else:
        removed = delete_csv_rows(invoice_nos)
    refresh_store()
    return removed


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
    numbers = list(df["Invoice_No"].astype(str)) if not df.empty else []
    # Include numbers already sent to Make: the sheet lags a second or two
    # behind, and reusing a just-issued number would be a GST problem.
    numbers += list(issued_this_session())

    if prefix or fy:
        numbers = [n for n in numbers if n.startswith(f"{prefix}/{fy}/")]

    seq = 0
    for val in numbers:
        m = re.search(r"(\d+)\s*$", str(val).strip())
        if m:
            seq = max(seq, int(m.group(1)))
    return seq


def next_invoice_no(
    prefix: str, d: dt.date, reset_each_fy: bool = False, min_seq: int = 0
) -> str:
    """Always one higher than the last invoice saved. Never reuses a number.

    `min_seq` is a floor for hosts with throwaway storage (Streamlit Cloud
    wipes the disk on every restart, so the local CSV cannot be trusted to
    remember how far the numbering got).
    """
    fy = fy_label(d)
    if reset_each_fy:
        seq = last_sequence(prefix, fy)
    else:
        seq = last_sequence()
    return f"{prefix}/{fy}/{max(seq, min_seq) + 1:03d}"


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

    # Must run before anything calls load_data().
    with st.expander("Storage", expanded=False):
        default_sheet = secret("sheet_id", SHEET_ID_DEFAULT)
        default_ws = secret("worksheet", WORKSHEET_DEFAULT)
        creds_ready = has_service_account()

        modes = {
            "Google Sheet (Make writes it)": "sheet_link",
            "Google Sheet (app writes it)": "sheet",
            "Local CSV": "csv",
        }
        mode_label = st.radio(
            "Records come from",
            list(modes),
            index=0,
            help="Default: Make posts each invoice into the sheet and the app "
                 "reads it back. No Google credentials needed.",
        )
        mode = modes[mode_label]

        if mode == "sheet" and not creds_ready:
            st.warning(
                "App-writes mode needs a `[gcp_service_account]` block in "
                "secrets. Use the first option instead."
            )

        if mode in ("sheet_link", "sheet"):
            sheet_id = extract_sheet_id(
                st.text_input(
                    "Sheet ID or URL",
                    value=default_sheet,
                    help="Paste the whole URL if easier — the ID is extracted.",
                )
            )
            worksheet_name = st.text_input("Worksheet (tab) name", value=default_ws)
            st.session_state["_storage"] = {
                "mode": mode,
                "sheet_id": sheet_id,
                "worksheet": worksheet_name.strip() or WORKSHEET_DEFAULT,
            }
            if mode == "sheet_link":
                st.caption(
                    "Sheet must be shared: **Anyone with the link → Viewer**. "
                    "Writing is done by your Make scenario."
                )
            else:
                svc_email = ""
                try:
                    svc_email = st.secrets["gcp_service_account"].get("client_email", "")
                except Exception:  # noqa: BLE001
                    pass
                if svc_email:
                    st.caption(f"Sheet must be shared with:\n\n`{svc_email}`")

            if st.button("🔌 Test sheet connection", width="stretch"):
                try:
                    rows = len(load_data())
                    st.success(f"Connected. {rows} record(s) readable.")
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
            st.caption(f"[Open sheet]({sheet_url(sheet_id)})")
        else:
            st.session_state["_storage"] = {"mode": "csv"}
            st.caption(f"`{CSV_PATH}`")

    # Always visible, so "my records vanished" is answerable at a glance.
    _cfg = storage_cfg()
    try:
        _saved = load_data()
        if _cfg["mode"] in ("sheet", "sheet_link"):
            st.caption(f"📊 {len(_saved)} record(s) in Google Sheet `{_cfg['worksheet']}`")
        else:
            st.caption(f"📁 {len(_saved)} record(s) in\n\n`{CSV_PATH}`")
    except Exception as exc:  # noqa: BLE001 - surface any backend failure
        _saved = pd.DataFrame(columns=COLUMNS)
        st.error(f"Storage unavailable: {exc}")

    if _cfg["mode"] == "csv" and not os.access(DATA_DIR, os.W_OK):
        st.error("This folder is not writable — invoices cannot be saved.")

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
    min_seq = st.number_input(
        "Never number below",
        min_value=0,
        value=0,
        step=1,
        help="Safety floor. On Streamlit Cloud the disk is wiped on restart, "
             "so set this to the last number you actually issued.",
    )
    st.divider()
    st.subheader("Make.com webhook")
    send_webhook = st.toggle("Send each invoice to webhook", value=True)
    webhook_url = st.text_input(
        "Webhook URL", value=WEBHOOK_URL_DEFAULT, type="default"
    )
    trigger_value = st.text_input(
        "Trigger column value",
        value="",
        help="Whatever your Make scenario expects in the sheet's Trigger "
             "column. Leave blank if Make fills it itself.",
    ).strip()

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

# If the store cannot be read, stop here. Carrying on with an empty dataset
# would restart numbering at 001 and issue duplicate invoice numbers.
try:
    load_data()
except Exception as exc:  # noqa: BLE001 - any backend failure
    st.error(f"Cannot reach the record store: {exc}")
    _c = storage_cfg()
    if _c["mode"] == "sheet_link":
        st.markdown(
            f"""
**Checklist**

1. Open the sheet → **Share** → General access → **Anyone with the link** →
   **Viewer**. Without this Google returns a sign-in page instead of data.
2. A tab named exactly `{_c['worksheet']}` exists.
3. Its first row is a header row containing `Invoice_No`.

[Open sheet]({sheet_url(_c['sheet_id'])}) · Sidebar → Storage →
**Test sheet connection** reports the exact failure. You can switch to
**Local CSV** there to keep working offline.
"""
        )
    elif _c["mode"] == "sheet":
        st.markdown(
            f"""
**Checklist**

1. `[gcp_service_account]` block present in secrets (Streamlit Cloud →
   Settings → Secrets, or `.streamlit/secrets.toml` locally). Paste the JSON
   key exactly — the `private_key` must keep its `\\n` escapes.
2. Google Sheets API enabled in that Cloud project.
3. Sheet shared with the service account email as **Editor**.
4. Tab named `{_c['worksheet']}` exists in the sheet.

Sidebar → Storage → **Test sheet connection** reports the exact failure.
You can switch to **Local CSV** there to keep working offline.
"""
        )
    st.stop()

tab_entry, tab_records = st.tabs(["New entry", "Records"])

with tab_entry:
    # Anything that writes widget state must run before those widgets exist,
    # hence the flag-then-rerun pattern used throughout this block.
    if st.session_state.pop("_advance_invoice", False):
        ref_date = st.session_state.get("inv_date", dt.date.today())
        st.session_state["invoice_no"] = next_invoice_no(
            inv_prefix, ref_date, reset_each_fy, min_seq
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
        auto_no = next_invoice_no(inv_prefix, inv_date, reset_each_fy, min_seq)
        # Follow the auto number when the prefix, date or floor changes - but
        # only if the field still holds the previous auto value, so a manually
        # typed number is never overwritten. Runs before the widget exists,
        # which is the only point session state may be written.
        prev_auto = st.session_state.get("_last_auto")
        if "invoice_no" not in st.session_state:
            st.session_state["invoice_no"] = auto_no
        elif prev_auto is not None and st.session_state["invoice_no"] == prev_auto:
            st.session_state["invoice_no"] = auto_no
        st.session_state["_last_auto"] = auto_no
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
        email = st.text_input("Email *", placeholder="you@company.com")
        phone = st.text_input("Phone", placeholder="9876543210", max_chars=15)

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
    l1, l2, l3 = st.columns(3)
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
        cat_2 = st.selectbox("Category ", [NO_SERVICE] + CATEGORIES, key="cat_2")
        if cat_2 == NO_SERVICE:
            service_2, desc_2, amount_2 = "", "", 0.0
            st.selectbox("Service_2", ["—"], disabled=True, key="svc_2_off")
            st.text_area("Discription_2", value="", height=90, disabled=True,
                         key="d2_off")
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
    with l3:
        # The sheet has Service_3 and Amount_3 but no Discription_3.
        st.markdown("**Item 3** *(optional)*")
        cat_3 = st.selectbox("Category  ", [NO_SERVICE] + CATEGORIES, key="cat_3")
        if cat_3 == NO_SERVICE:
            service_3, amount_3 = "", 0.0
            st.selectbox("Service_3", ["—"], disabled=True, key="svc_3_off")
            st.number_input("Amount_3", value=0.0, disabled=True, key="amt_3_off",
                            format="%.2f")
        else:
            service_3 = st.selectbox(
                "Service_3", SERVICE_CATALOG[cat_3]["options"], key="svc_3"
            )
            amount_3 = st.number_input(
                "Amount_3", min_value=0.0, step=500.0, format="%.2f"
            )

    # ---- HSN / SAC, derived from the chosen category ----
    st.subheader("HSN / SAC")
    st.caption(
        "Filled automatically from the service category. Edit if a particular "
        "invoice needs a different code."
    )
    h1, h2, h3 = st.columns(3)
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
    with h3:
        if cat_3 == NO_SERVICE:
            hsn_3 = ""
            st.text_input("HSN/SAC — Item 3", value="", disabled=True,
                          key="hsn_3_off")
        else:
            hsn_3 = st.text_input(
                f"HSN/SAC — Item 3 ({cat_3})",
                value=SERVICE_CATALOG[cat_3]["sac"],
                key=f"hsn_3_{cat_3}",
                max_chars=8,
            ).strip()

    # ---- live calculation ----
    total_amount = round(amount_1 + amount_2 + amount_3, 2)
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
    if cat_3 != NO_SERVICE:
        if amount_3 <= 0:
            errors.append(
                f"Item 3 is set to '{service_3}' but Amount_3 is 0. "
                "Set an amount or switch Category back to none."
            )
        if not hsn_3 or not hsn_3.isdigit():
            errors.append("HSN/SAC for Item 3 must be numeric.")
    digits = re.sub(r"\D", "", phone)
    if phone.strip() and len(digits) not in (10, 11, 12):
        errors.append("Phone should be a 10-digit number (optionally with 91).")
    if not email.strip():
        errors.append("Email is required.")
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
                "Invoice_No": invoice_no.strip(),
                "Date": inv_date.strftime("%d-%m-%Y"),
                "Client_Name": billing_name.strip(),
                "Client_Address": billing_address.strip(),
                "Client_GST": client_gst,
                "State": state,
                "Service_1": service_1.strip(),
                "Discription_1": desc_1.strip(),
                "Amount_1": f"{amount_1:.2f}",
                "Service_2": service_2.strip(),
                "Discription_2": desc_2.strip(),
                "Amount_2": f"{amount_2:.2f}",
                "Service_3": service_3.strip(),
                "Amount_3": f"{amount_3:.2f}",
                "total_amount": f"{total_amount:.2f}",
                "gst": f"{gst_amount:.2f}",
                "total": f"{grand_total:.2f}",
                "Amount_In_Words": words,
                "Email": email.strip(),
                "Phone": phone.strip(),
                "Trigger": trigger_value,
                # Not columns in the sheet, but sent so Make can map them if
                # you add columns later.
                "Timestamp": now_ist().strftime(TS_FORMAT),
                "GST_Code": gst_code,
                "HSN_1": hsn_1,
                "HSN_2": hsn_2,
                "HSN_3": hsn_3,
            }
            mode = storage_cfg()["mode"]

            if mode == "sheet_link":
                # Make is the only writer here, so a failed post means the
                # invoice was NOT saved anywhere. Never report success.
                if not send_webhook:
                    st.error(
                        "Webhook is switched off, and in this mode the webhook "
                        "is what writes to the sheet. Turn it on in the sidebar."
                    )
                    st.stop()

                payload = build_payload(
                    {**row, "_cat_1": cat_1,
                     "_cat_2": "" if cat_2 == NO_SERVICE else cat_2},
                    gst_rate, intra, gst_amount,
                )
                ok, detail = post_webhook(webhook_url, payload)
                if not ok:
                    queue_pending(payload)
                    st.error(
                        f"Not saved — webhook failed: {detail}. The invoice is "
                        "queued; retry from the sidebar once Make is reachable."
                    )
                    st.stop()

                mark_issued(row["Invoice_No"])
                msgs = [
                    f"Sent **{row['Invoice_No']}** for {row['Client_Name']} "
                    f"to Make ({detail})."
                ]
                with st.spinner("Waiting for the row to appear in the sheet…"):
                    landed = wait_for_row(row["Invoice_No"])
                msgs.append(
                    "Row is in the sheet." if landed else
                    "Make has accepted it; the sheet may take a few more "
                    "seconds. Press Refresh in Records."
                )
            else:
                try:
                    append_row(row)
                except RuntimeError as exc:
                    st.error(str(exc))
                    st.stop()
                mark_issued(row["Invoice_No"])
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
                        # The record is already stored, and the payload is
                        # queued, so nothing is lost if Make is unreachable.
                        queue_pending(payload)
                        msgs.append(
                            f"⚠️ Webhook failed: {detail}. Queued for retry — "
                            "use the sidebar once it is back."
                        )

            # Messages have to survive the rerun, so stash them instead of
            # calling st.success() here.
            st.session_state["_flash"] = "  \n".join(msgs)
            st.session_state["_advance_invoice"] = True
            st.rerun()

with tab_records:
    # Delete happens in this tab, so its confirmation belongs here too.
    _rf = st.session_state.pop("_flash", None)
    if _rf:
        st.success(_rf)
    _rfe = st.session_state.pop("_flash_error", None)
    if _rfe:
        st.error(_rfe)

    try:
        df = load_data()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read records: {exc}")
        st.stop()
    if df.empty:
        st.info("No invoices saved yet.")
    else:
        # Split the stored Timestamp into readable date and time columns for
        # display. Timestamp itself stays untouched in the CSV and the webhook.
        df = df.reset_index(drop=True)
        ts = parse_timestamps(df)
        # Only rows with a real Timestamp have a clock time. Rows dated from
        # the Date column alone would otherwise all read "00:00:00".
        if "Timestamp" in df.columns:
            clock = pd.to_datetime(df["Timestamp"], format=TS_FORMAT, errors="coerce")
        else:
            clock = pd.Series(pd.NaT, index=df.index)
        df = df.assign(
            **{
                "Created Date": ts.dt.strftime("%d-%m-%Y").fillna(""),
                "Created Time": clock.dt.strftime("%H:%M:%S").fillna(""),
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
                refresh_store()
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

        def col_sum(frame: pd.DataFrame, *cols: str) -> float:
            """Numeric total across columns, ignoring blanks and stray text."""
            total = 0.0
            for col in cols:
                if col in frame.columns:
                    total += pd.to_numeric(frame[col], errors="coerce").fillna(0).sum()
            return float(total)

        amount_total = col_sum(view, "Amount_1", "Amount_2", "Amount_3")
        gst_total = col_sum(view, "gst")
        grand = amount_total + gst_total

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total invoices created", f"{len(view)}")
        m2.metric("Total amount", f"₹ {amount_total:,.0f}")
        m3.metric("Total GST", f"₹ {gst_total:,.0f}")
        m4.metric("Total amount + GST", f"₹ {grand:,.0f}")

        # The sheet carries its own `total` column. If it disagrees with the
        # arithmetic above, some row was written or edited badly.
        sheet_total = col_sum(view, "total")
        if sheet_total and abs(sheet_total - grand) > 1:
            st.caption(
                f"⚠️ The sheet's own `total` column adds up to "
                f"₹{sheet_total:,.0f}, which does not match ₹{grand:,.0f}. "
                "Check for a row with a missing or hand-edited amount."
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

        # ---- delete records ----
        with st.expander("🗑️ Delete records"):
            st.caption(
                "Under GST an invoice that has already been issued should be "
                "cancelled with a credit note, not deleted. Use this for test "
                "entries and mistakes made before the invoice went out."
            )
            if storage_cfg()["mode"] == "sheet_link":
                st.info(
                    "This mode reads the sheet over a public link, so it "
                    "cannot delete. Delete the row in the sheet, then press "
                    "Refresh above.\n\n"
                    f"[Open sheet]({sheet_url(storage_cfg()['sheet_id'])})"
                )
            to_delete = st.multiselect(
                "Select invoices to delete",
                options=list(view["Invoice_No"].astype(str)),
                help="Deletes the row from the record store.",
                disabled=storage_cfg()["mode"] == "sheet_link",
            )
            if to_delete:
                st.warning(
                    f"About to permanently delete {len(to_delete)} record(s): "
                    + ", ".join(to_delete)
                )
                confirm = st.checkbox("Yes, delete them permanently")
                if st.button("Delete selected", type="primary", disabled=not confirm):
                    try:
                        removed = delete_rows(to_delete)
                        st.session_state["_flash"] = (
                            f"Deleted {removed} record(s)."
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.session_state["_flash_error"] = f"Delete failed: {exc}"
                    st.rerun()

        with st.expander("Storage check"):
            _c = storage_cfg()
            if _c["mode"] in ("sheet", "sheet_link"):
                st.write(
                    {
                        "backend": ("Google Sheet (Make writes)"
                                    if _c["mode"] == "sheet_link"
                                    else "Google Sheet (app writes)"),
                        "sheet id": _c["sheet_id"],
                        "worksheet": _c["worksheet"],
                        "records": len(df),
                        "unparseable timestamps": int(df["_ts"].isna().sum()),
                        "cache": "60s — press Refresh after editing the sheet "
                                 "by hand",
                    }
                )
            else:
                physical = sum(1 for _ in open(CSV_PATH, encoding="utf-8")) - 1
                st.write(
                    {
                        "backend": "Local CSV",
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