# Invoice Data Entry (Streamlit)

Collects invoice details, auto-calculates GST and totals, converts the final
amount to Indian-format words, and appends each entry to `data/invoices.csv`.

## Folder structure

```
invoice-app/
├── app.py              # the whole app
├── requirements.txt
├── data/
│   └── invoices.csv    # created automatically on first run
└── .streamlit/
    └── secrets.toml    # only if you use the Google Sheets toggle
```

## Steps to run locally

1. **Create the folder and drop in `app.py` + `requirements.txt`.**

2. **Virtual environment**

   ```bash
   cd invoice-app
   python -m venv venv
   venv\Scripts\activate        # Windows
   source venv/bin/activate     # macOS / Linux
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Run**

   ```bash
   streamlit run app.py
   ```

   Opens at `http://localhost:8501`.

5. **Use it** — fill the form. Totals, GST and words update live as you type.
   Hit **Save invoice**; the row is appended to `data/invoices.csv` with the
   exact column headers you specified.

## What is automated for you

| Field | How it is filled |
|---|---|
| `Timestamp` | System time at save |
| `Invoice_No` | Auto-suggested as `DPC/26-27/001`, increments per financial year (editable) |
| `total_amount` | `Amount_1 + Amount_2` |
| `gst` | `total_amount × rate` (rate set in sidebar, default 18%) |
| `total_amount+ gst` | `total_amount + gst` |
| `Final Amount in words ` | Indian numbering — lakh / crore, with paise |

Extra safeguards:

- **GSTIN format check** (15-char pattern) plus a warning if the GSTIN's state
  code does not match the selected state.
- **CGST+SGST vs IGST** — decided by comparing client state to your business
  state in the sidebar (default Haryana).
- **Duplicate invoice number warning.**
- **Records tab** with search, total billed, and CSV download.

## Optional: also write to Google Sheets

1. Google Cloud Console → create a **service account** → enable the **Google
   Sheets API** → download the JSON key.
2. Share your Google Sheet with the service account email (Editor access).
3. Create `.streamlit/secrets.toml`:

   ```toml
   [gcp_service_account]
   type = "service_account"
   project_id = "your-project"
   private_key_id = "..."
   private_key = "-----BEGIN PRIVATE KEY-----\nMII...\n-----END PRIVATE KEY-----\n"
   client_email = "invoice-bot@your-project.iam.gserviceaccount.com"
   client_id = "..."
   token_uri = "https://oauth2.googleapis.com/token"
   ```

4. `pip install gspread google-auth`
5. Turn on **Also push to Google Sheets** in the sidebar, paste the Sheet ID
   (the long string in the sheet URL between `/d/` and `/edit`) and the
   worksheet name. Headers are written automatically if the sheet is empty.

## Deploying

**Streamlit Community Cloud** — push the folder to GitHub, connect the repo at
share.streamlit.io, set the main file to `app.py`, and paste the secrets into
the app's Secrets box. Note: the container filesystem is ephemeral, so on a
hosted deployment enable the Google Sheets push (or a database) rather than
relying on the local CSV.

**Docker**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

## Common issues

| Problem | Fix |
|---|---|
| `streamlit: command not found` | Virtual env not activated, or use `python -m streamlit run app.py` |
| Totals not updating | Do not wrap the widgets in `st.form` — forms only submit on button press |
| Records tab shows stale rows | `load_data` is cached for 5s; it clears itself on save |
| Address breaking the CSV | Newlines are converted to `, ` before writing |
