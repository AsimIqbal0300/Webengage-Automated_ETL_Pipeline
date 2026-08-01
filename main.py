import functions_framework
import imaplib, email, re, html, requests, io, zipfile
import pandas as pd
import base64
import json
import urllib.parse
from google.cloud import bigquery
from google.oauth2 import service_account

CONFIG = {
    # Gmail inbox that receives the WebEngage scheduled report
    "imap_email": "YOUR_GMAIL_ADDRESS_HERE",
    "imap_app_password": "YOUR_GMAIL_APP_PASSWORD_HERE",  # NOT your normal password — see README

    # Filters to find the right email — exact substring match against the
    # sender address and subject line
    "sender_filter": "YOUR_SENDER_FILTER_HERE",       # e.g. part of the From address
    "subject_filter": "Scheduled Report",             # e.g. part of the Subject line

    # BigQuery destination
    "gcp_project_id": "YOUR_GCP_PROJECT_ID_HERE",
    "bq_dataset": "YOUR_DATASET_NAME_HERE",
    "bq_table": "YOUR_TABLE_NAME_HERE",
    "credentials_path": "YOUR_SERVICE_ACCOUNT_JSON_FILENAME.json",

    # "append" = keep daily history (accumulates rows every run)
    # "replace" = wipe and reload fresh every run (only ever shows latest day)
    "write_mode": "append",
}


# ==========================================
# STEP 1: Find the right email
# ==========================================
def fetch_latest_report_email(imap_email, imap_password, sender_filter=None,
                               subject_filter=None, mailbox="INBOX"):
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(imap_email, imap_password)
    imap.select(mailbox)

    criteria = []
    if sender_filter:
        criteria.append(f'(FROM "{sender_filter}")')
    if subject_filter:
        criteria.append(f'(SUBJECT "{subject_filter}")')
    status, data = imap.search(None, " ".join(criteria) if criteria else "ALL")
    if status != "OK" or not data[0]:
        imap.logout()
        raise RuntimeError("No matching emails found.")

    latest_id = data[0].split()[-1]  # most recent match
    status, msg_data = imap.fetch(latest_id, "(RFC822)")
    msg = email.message_from_bytes(msg_data[0][1])

    body_html, body_text = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and body_html is None:
                body_html = part.get_payload(decode=True).decode(errors="ignore")
            elif ct == "text/plain" and body_text is None:
                body_text = part.get_payload(decode=True).decode(errors="ignore")
    else:
        body_text = msg.get_payload(decode=True).decode(errors="ignore")

    imap.logout()
    print(f"Fetched email — From: {msg.get('From')} | Subject: {msg.get('Subject')} | Date: {msg.get('Date')}")
    return {"from": msg.get("From"), "subject": msg.get("Subject"), "body": body_html or body_text or ""}


# ==========================================
# STEP 2: Find the real download link
# ==========================================
def extract_download_link(email_body, keywords=("storage.googleapis.com", "csv.zip", "download", "report")):
    clean_body = html.unescape(email_body)
    all_links = re.findall(r'https?://[^\s"\'<>]+', clean_body)
    if not all_links:
        raise RuntimeError("No links found in email body.")

    print(f"All links found in email ({len(all_links)}): {all_links}")

    candidates = []
    for link in all_links:
        # WebEngage wraps real destinations inside a base64-encoded "p" query
        # param on c.webengage.com/lw/ tracking-pixel URLs. Multiple tracking
        # links can share that same prefix while pointing at different
        # destinations (dashboard homepage vs. the actual report) — so decode
        # each one to see its true target instead of trusting the outer domain.
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        p_values = qs.get("p")
        if p_values:
            try:
                padded = p_values[0] + "=" * (-len(p_values[0]) % 4)
                decoded = json.loads(base64.b64decode(padded))
                to_url = decoded.get("toURL", "")
                print(f"Decoded link -> toURL: {to_url}")
                candidates.append((link, to_url))
                continue
            except Exception as e:
                print(f"Could not decode tracking link, skipping: {e}")
        candidates.append((link, link))

    for keyword in keywords:
        matches = [orig for orig, target in candidates if keyword in target.lower()]
        if matches:
            print(f"Selected link (matched '{keyword}'): {matches[0]}")
            return matches[0]

    print(f"No keyword matched — falling back to first link: {all_links[0]}")
    return all_links[0]


# ==========================================
# STEP 3: Download (handles ZIP or raw CSV)
# ==========================================
def download_report(url):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    content = resp.content

    if content[:2] == b"PK":  # ZIP magic bytes
        zf = zipfile.ZipFile(io.BytesIO(content))
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(f"No CSV found inside zip. Contents: {zf.namelist()}")
        if len(csv_names) > 1:
            print(f"Warning: multiple CSVs found in zip, using first: {csv_names}")
        return io.BytesIO(zf.read(csv_names[0]))

    return io.BytesIO(content)


# ==========================================
# STEP 4: Clean the data
# ==========================================
def sanitize_column_name(col):
    col = col.strip().lower()
    col = re.sub(r"[^0-9a-z]+", "_", col)
    return re.sub(r"_+", "_", col).strip("_")


PERCENT_PATTERN = re.compile(r"^-?\d+(\.\d+)?%$")


def clean_dataframe(df):
    df = df.copy()
    df.columns = [sanitize_column_name(c) for c in df.columns]

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"nan": None, "": None})

    for col in ("day", "start_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], dayfirst=True, errors="coerce")

    # Percentage columns detected by CONTENT (every non-null value matches a
    # pure "12.34%" pattern), not by column name — this avoids accidentally
    # mangling text columns (e.g. campaign names) that happen to contain "%".
    for col in df.columns:
        if df[col].dtype == object:
            non_null = df[col].dropna()
            if len(non_null) > 0 and non_null.astype(str).str.match(PERCENT_PATTERN).all():
                df[col] = df[col].astype(str).str.replace("%", "", regex=False).str.strip()
                df[col] = pd.to_numeric(df[col], errors="coerce")

    df["ingested_at"] = pd.Timestamp.utcnow().tz_localize(None)
    return df


# ==========================================
# STEP 5: Push to BigQuery
# ==========================================
def push_to_bigquery(df, project_id, dataset, table, credentials_path, if_exists="append"):
    creds = service_account.Credentials.from_service_account_file(credentials_path)
    client = bigquery.Client(project=project_id, credentials=creds)
    table_id = f"{project_id}.{dataset}.{table}"

    write_disposition = (
        bigquery.WriteDisposition.WRITE_APPEND
        if if_exists == "append"
        else bigquery.WriteDisposition.WRITE_TRUNCATE
    )

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        write_disposition=write_disposition,
        # Only let BigQuery redefine the schema when doing a full replace —
        # on append, autodetect can conflict with the existing table's types
        # (e.g. DATE vs DATETIME) and fail the whole load.
        autodetect=(if_exists == "replace"),
    )

    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    csv_bytes = io.BytesIO(csv_buffer.getvalue().encode("utf-8"))

    load_job = client.load_table_from_file(csv_bytes, table_id, job_config=job_config)
    load_job.result()

    print(f"Pushed {len(df)} rows to {table_id}")


# ==========================================
# Entry point
# ==========================================
@functions_framework.http
def run_pipeline(request):
    result = fetch_latest_report_email(
        CONFIG["imap_email"], CONFIG["imap_app_password"],
        sender_filter=CONFIG["sender_filter"], subject_filter=CONFIG["subject_filter"],
    )
    download_url = extract_download_link(result["body"])
    df_raw = pd.read_csv(download_report(download_url))
    df_clean = clean_dataframe(df_raw)
    push_to_bigquery(df_clean, CONFIG["gcp_project_id"], CONFIG["bq_dataset"],
                      CONFIG["bq_table"], CONFIG["credentials_path"], CONFIG["write_mode"])
    return f"OK — pushed {len(df_clean)} rows", 200
