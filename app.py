import streamlit as st
import csv
import io
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from scout import audit_website, generate_pitch

AUDIT_LIMIT = 400
MAX_BATCH_SIZE = 20
FREE_DEMO_LIMIT = 3
GLOBAL_DAILY_DEMO_CAP = 150
DEMO_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
GUMROAD_URL = "https://kashifhas32.gumroad.com/l/sitescout"

st.set_page_config(page_title="SiteScout", layout="wide")
st.title("SiteScout")
st.caption(
    "Batch audit business websites and generate personalized cold outreach pitches."
)


def is_valid_key_format(key: str) -> bool:
    """Ensures license keys only contain alphanumeric and hyphen characters."""
    return bool(re.match(r"^[a-zA-Z0-9\-]{8,64}$", key.strip()))


# ---------- Upstash Redis Helpers ----------
def _upstash_headers() -> dict:
    return {"Authorization": f"Bearer {st.secrets['UPSTASH_TOKEN']}"}


def upstash_get_int(redis_key: str) -> int | None:
    try:
        safe_key = quote(redis_key, safe="")
        url = f"{st.secrets['UPSTASH_URL']}/get/{safe_key}"
        resp = requests.get(url, headers=_upstash_headers(), timeout=5)
        if resp.status_code != 200:
            return None
        val = resp.json().get("result")
        return int(val) if val is not None else 0
    except Exception:
        return None


def upstash_incrby(
    redis_key: str, count: int, ttl_seconds: int | None = None
) -> int | None:
    if count <= 0:
        return upstash_get_int(redis_key)
    try:
        safe_key = quote(redis_key, safe="")
        url = f"{st.secrets['UPSTASH_URL']}/incrby/{safe_key}/{count}"
        resp = requests.get(url, headers=_upstash_headers(), timeout=5)
        if resp.status_code != 200:
            return None
        new_val = int(resp.json().get("result", 0))
        if ttl_seconds:
            exp_url = f"{st.secrets['UPSTASH_URL']}/expire/{safe_key}/{ttl_seconds}"
            requests.get(exp_url, headers=_upstash_headers(), timeout=5)
        return new_val
    except Exception:
        return None


def upstash_decrby(redis_key: str, count: int) -> None:
    if count <= 0:
        return
    try:
        safe_key = quote(redis_key, safe="")
        url = f"{st.secrets['UPSTASH_URL']}/decrby/{safe_key}/{count}"
        requests.get(url, headers=_upstash_headers(), timeout=5)
    except Exception:
        pass


# ---------- Paid License Quota ----------
def get_audits_used(key: str) -> int:
    clean_key = key.strip()
    if not is_valid_key_format(clean_key):
        return 0
    val = upstash_get_int(clean_key)
    return val if val is not None else 0


def increment_audits(key: str, count: int) -> int:
    clean_key = key.strip()
    if not is_valid_key_format(clean_key) or count <= 0:
        return 0
    val = upstash_incrby(clean_key, count)
    return val if val is not None else 0


# ---------- Server-Side Free Demo Protection ----------
def get_demo_identity_key() -> str:
    """Hashes the visitor's real IP (never stored raw) to enforce 3 free audits across refreshes/incognito."""
    ip = None
    try:
        ctx_ip = getattr(st.context, "ip_address", None)
        if ctx_ip:
            ip = str(ctx_ip).strip()
    except Exception:
        pass

    if not ip:
        try:
            headers = st.context.headers
            for header_name in (
                "X-Forwarded-For",
                "CF-Connecting-IP",
                "X-Real-Ip",
                "True-Client-Ip",
            ):
                val = headers.get(header_name)
                if val:
                    ip = val.split(",")[0].strip()
                    break
        except Exception:
            pass

    salt = st.secrets.get("UPSTASH_TOKEN", "sitescout-salt")
    if ip:
        raw_id = f"ip:{ip}"
    else:
        try:
            headers = st.context.headers
            ua = headers.get("User-Agent", "unknown")
            lang = headers.get("Accept-Language", "unknown")
            raw_id = f"ua:{ua}|{lang}"
        except Exception:
            raw_id = "fallback:shared"

    digest = hashlib.sha256(f"{salt}:{raw_id}".encode("utf-8")).hexdigest()[:24]
    return f"demo_id_{digest}"


def get_daily_demo_key() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"demo_daily_{today}"


def get_demo_remaining(demo_key: str) -> tuple[int, bool]:
    """
    Returns (remaining_audits, service_ok).
    Fails CLOSED if Upstash is unreachable so free demos cannot be abused during outages.
    """
    used = upstash_get_int(demo_key)
    daily_used = upstash_get_int(get_daily_demo_key())
    if used is None or daily_used is None:
        return 0, False
    if daily_used >= GLOBAL_DAILY_DEMO_CAP:
        return 0, True
    session_used = st.session_state.get("demo_used", 0)
    effective_used = max(used, session_used)
    return max(0, FREE_DEMO_LIMIT - effective_used), True


def reserve_demo_quota(demo_key: str, count: int) -> tuple[bool, str, int]:
    """
    Atomically reserves free trial audits BEFORE running scraping/OpenAI.
    Prevents multi-tab race conditions and VPN flooding via per-IP + global daily caps.
    """
    daily_key = get_daily_demo_key()
    new_daily = upstash_incrby(daily_key, count, ttl_seconds=172800)
    if new_daily is None:
        return (
            False,
            "Free demo service is temporarily unavailable. Enter a license key to continue.",
            0,
        )
    if new_daily > GLOBAL_DAILY_DEMO_CAP:
        upstash_decrby(daily_key, count)
        return (
            False,
            f"Daily free demo limit reached across all visitors. Grab a Founding Member license ($15) for instant access: {GUMROAD_URL}",
            0,
        )

    new_ip_used = upstash_incrby(demo_key, count, ttl_seconds=DEMO_TTL_SECONDS)
    if new_ip_used is None:
        upstash_decrby(daily_key, count)
        return (
            False,
            "Free demo service is temporarily unavailable. Enter a license key to continue.",
            0,
        )
    if new_ip_used > FREE_DEMO_LIMIT:
        upstash_decrby(demo_key, count)
        upstash_decrby(daily_key, count)
        left = max(0, FREE_DEMO_LIMIT - (new_ip_used - count))
        return (
            False,
            f"You only have {left} free demo audit(s) remaining. Unlock 400 audits + CSV export: {GUMROAD_URL}",
            left,
        )

    # Track total cumulative demo traffic in Upstash for Day 2 KPIs
    upstash_incrby("free_demo_runs", count)
    st.session_state["demo_used"] = new_ip_used
    return True, "", max(0, FREE_DEMO_LIMIT - new_ip_used)


# ---------- Gumroad License Validation ----------
def verify_gumroad_license(license_key: str) -> tuple[bool, str]:
    key = license_key.strip()
    if not key:
        return False, "Please enter a license key."
    if not is_valid_key_format(key):
        return False, "Invalid license key format."

    url = "https://api.gumroad.com/v2/licenses/verify"
    payload = {
        "product_id": st.secrets["GUMROAD_PRODUCT_ID"],
        "license_key": key,
        "increment_uses_count": "false",
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        data = response.json()

        if not data.get("success"):
            return False, data.get(
                "message", "Invalid or inactive license key."
            )

        purchase = data.get("purchase", {})
        if purchase.get("refunded") or purchase.get("disputed"):
            return False, "License key has been refunded or revoked."

        return True, "License verified successfully."
    except Exception as e:
        return False, f"Verification service unavailable: {e}"


# ---------- Sidebar Authentication ----------
st.sidebar.title("Account")
input_key = st.sidebar.text_input("Enter License Key", type="password")

# URL Query Param Auto-Login
params = st.query_params
if "key" in params and not st.session_state.get("authenticated", False):
    valid, msg = verify_gumroad_license(params["key"])
    if valid:
        st.session_state["authenticated"] = True
        st.session_state["license_key"] = params["key"].strip()

# Manual Verification
if st.sidebar.button("Verify License"):
    valid, msg = verify_gumroad_license(input_key)
    if valid:
        clean_key = input_key.strip()
        st.session_state["authenticated"] = True
        st.session_state["license_key"] = clean_key
        st.query_params["key"] = clean_key
        st.rerun()
    else:
        st.session_state["authenticated"] = False
        st.session_state["license_key"] = ""
        st.sidebar.error(msg)

is_paid = st.session_state.get("authenticated", False)
demo_identity_key = None if is_paid else get_demo_identity_key()

st.sidebar.divider()
quota_display = st.sidebar.empty()

if is_paid:
    st.sidebar.success("License active.")
    current_key = st.session_state.get("license_key", "")
    used_count = get_audits_used(current_key)
    remaining = max(0, AUDIT_LIMIT - used_count)
    batch_limit = MAX_BATCH_SIZE
    quota_display.metric("Audits Remaining", f"{remaining} / {AUDIT_LIMIT}")
else:
    remaining, demo_ok = get_demo_remaining(demo_identity_key)
    batch_limit = FREE_DEMO_LIMIT
    quota_display.metric("Free Demo Audits Left", f"{remaining} / {FREE_DEMO_LIMIT}")
    st.sidebar.markdown(f"[Unlock 400 Audits ($15 Founding Price)]({GUMROAD_URL})")

    if not demo_ok:
        st.error(
            "Free demo is temporarily unavailable. Enter a valid license key in the sidebar to continue."
        )
    elif remaining > 0:
        st.info(
            f"Free Trial Mode: Test up to {remaining} website(s) free right now (no signup needed). "
            f"20-URL batching and CSV export unlock with a Founding Member license ($15): "
            f"[Get Full Access]({GUMROAD_URL})"
        )
    else:
        st.warning(
            f"You have used all {FREE_DEMO_LIMIT} free trial audits (or today's free demo pool is full). "
            f"[Get 400 Audits + CSV Export ($15 Founding Price)]({GUMROAD_URL})"
        )

# ---------- Main Input ----------
urls_input = st.text_area(
    f"Target Website URLs (one per line, max {batch_limit} per run)",
    placeholder="berkshirehathaway.com\nstripe.com\nexample.com",
    height=140,
)


def process_target(url: str) -> dict:
    """Worker function for concurrent thread pool execution."""
    audit_data = audit_website(url)
    status = audit_data["status"]
    issues = audit_data.get("issues", [])

    if "Failed" in status or "Skipped" in status:
        pitch = "N/A - Site unreachable"
    elif status == "Strong Site - Skip":
        pitch = "Strong Site - Skip"
    else:
        pitch = generate_pitch(audit_data)

    copyright_year = audit_data.get("copyright_year")
    if copyright_year is None:
        copyright_display = "Not Found"
    elif audit_data.get("is_outdated_copyright"):
        copyright_display = f"{copyright_year} (Outdated)"
    else:
        copyright_display = str(copyright_year)

    return {
        "URL": audit_data["url"],
        "Title": audit_data["title"] or "N/A",
        "Status": status,
        "Issues Count": len(issues),
        "Issues Found": " | ".join(issues) if issues else "None",
        "Load Time (s)": (
            audit_data["load_time_sec"]
            if audit_data["load_time_sec"] is not None
            else "N/A"
        ),
        "SSL Secure": "Yes" if audit_data["is_https"] else "No",
        "Mobile Ready": "Yes" if audit_data["has_mobile_viewport"] else "No",
        "Meta Description": "Yes" if audit_data["has_meta_description"] else "No",
        "H1 Tag": "Yes" if audit_data["has_h1"] else "No",
        "Contact Link": "Yes" if audit_data["has_contact_link"] else "No",
        "Copyright Year": copyright_display,
        "Generated Pitch": pitch,
    }


button_label = "Run Batch Audit" if is_paid else "Run Free Trial Audit"
run_disabled = (not is_paid) and (remaining <= 0)

if st.button(button_label, type="primary", disabled=run_disabled):
    seen = set()
    raw_urls = []
    for line in urls_input.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        normalized = (
            cleaned.lower().replace("https://", "").replace("http://", "").rstrip("/")
        )
        if normalized not in seen:
            seen.add(normalized)
            raw_urls.append(cleaned)

    if not raw_urls:
        st.warning("Please enter at least one URL.")
    elif len(raw_urls) > batch_limit:
        st.error(f"Batch limit exceeded. Max {batch_limit} URLs per run.")
    elif len(raw_urls) > remaining:
        if is_paid:
            st.error(
                f"Quota exceeded. You only have {remaining} audits remaining on your license."
            )
        else:
            st.error(
                f"Free trial limit exceeded. You only have {remaining} free audit(s) left. "
                f"Enter fewer URLs or grab a license key: {GUMROAD_URL}"
            )
    else:
        can_proceed = True
        if not is_paid:
            ok, err_msg, new_demo_rem = reserve_demo_quota(
                demo_identity_key, len(raw_urls)
            )
            if not ok:
                st.error(err_msg)
                can_proceed = False
            else:
                quota_display.metric(
                    "Free Demo Audits Left", f"{new_demo_rem} / {FREE_DEMO_LIMIT}"
                )

        if can_proceed:
            total_urls = len(raw_urls)
            ordered_results = [None] * total_urls
            progress_bar = st.progress(0)
            status_text = st.empty()

            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_idx = {
                    executor.submit(process_target, url): (idx, url)
                    for idx, url in enumerate(raw_urls)
                }
                completed = 0
                for future in as_completed(future_to_idx):
                    idx, url = future_to_idx[future]
                    completed += 1
                    status_text.text(f"Processed {completed}/{total_urls}: {url}")
                    progress_bar.progress(completed / total_urls)
                    try:
                        ordered_results[idx] = future.result()
                    except Exception as e:
                        ordered_results[idx] = {
                            "URL": url,
                            "Title": "Error",
                            "Status": f"Failed: {e}",
                            "Issues Count": 0,
                            "Issues Found": "None",
                            "Load Time (s)": "N/A",
                            "SSL Secure": "No",
                            "Mobile Ready": "No",
                            "Meta Description": "No",
                            "H1 Tag": "No",
                            "Contact Link": "No",
                            "Copyright Year": "N/A",
                            "Generated Pitch": "N/A - Internal execution error",
                        }

            results = [r for r in ordered_results if r is not None]

            if is_paid:
                new_total = increment_audits(current_key, len(results))
                new_remaining = max(0, AUDIT_LIMIT - new_total)
                quota_display.metric(
                    "Audits Remaining", f"{new_remaining} / {AUDIT_LIMIT}"
                )

            status_text.empty()
            progress_bar.empty()

            st.session_state["last_results"] = results

# ---------- Display Results & Controls ----------
if "last_results" in st.session_state:
    col_header, col_reset = st.columns([5, 1])
    with col_header:
        st.subheader("Audit Results")
    with col_reset:
        if st.button("Clear Results"):
            del st.session_state["last_results"]
            st.rerun()

    for idx, row in enumerate(st.session_state["last_results"]):
        is_lead = row["Status"].startswith("Lead Found")
        with st.expander(f"{row['URL']} - {row['Status']}", expanded=is_lead):
            r1 = st.columns(4)
            load_val = (
                f"{row['Load Time (s)']}s"
                if row["Load Time (s)"] != "N/A"
                else "N/A"
            )
            r1[0].metric("Load Time", load_val)
            r1[1].metric("SSL Secure", row["SSL Secure"])
            r1[2].metric("Mobile Ready", row["Mobile Ready"])
            r1[3].metric("Copyright", row["Copyright Year"])

            r2 = st.columns(4)
            r2[0].metric("Meta Description", row["Meta Description"])
            r2[1].metric("H1 Tag", row["H1 Tag"])
            r2[2].metric("Contact Link", row["Contact Link"])
            r2[3].metric("Issues Found", row["Issues Count"])

            if row["Status"] == "Strong Site - Skip":
                st.success(
                    "Strong Site - Skip: Passed all technical checks. Move on to weaker leads."
                )
            elif "Failed" in row["Status"] or "Skipped" in row["Status"]:
                st.warning(row["Status"])
            else:
                st.markdown("**Detected Flaws:**")
                for flaw in row["Issues Found"].split(" | "):
                    st.markdown(f"- {flaw}")
                st.text_area(
                    "Cold Pitch",
                    value=row["Generated Pitch"],
                    height=95,
                    key=f"pitch_{idx}_{row['URL']}",
                )

    if is_paid:
        csv_buffer = io.StringIO()
        fieldnames = [
            "URL",
            "Title",
            "Status",
            "Issues Count",
            "Issues Found",
            "Load Time (s)",
            "SSL Secure",
            "Mobile Ready",
            "Meta Description",
            "H1 Tag",
            "Contact Link",
            "Copyright Year",
            "Generated Pitch",
        ]
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(st.session_state["last_results"])

        st.download_button(
            label="Download Results as CSV",
            data=csv_buffer.getvalue(),
            file_name="sitescout_leads.csv",
            mime="text/csv",
        )
    else:
        st.button("Download Results as CSV (Locked in Free Trial)", disabled=True)
        st.markdown(
            f"Like the output? [Unlock 400 audits, 20-URL batching, and CSV export for $15 (Founding Member)]({GUMROAD_URL})"
        )