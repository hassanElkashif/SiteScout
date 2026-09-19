import streamlit as st
import csv
import io
import requests
from scout import audit_website, generate_pitch

AUDIT_LIMIT = 400
MAX_BATCH_SIZE = 20

st.set_page_config(page_title="SiteScout", layout="wide")
st.title("SiteScout")
st.caption(
    "Batch audit business websites and generate personalized cold outreach pitches."
)


# Upstash Quota Tracking
def get_audits_used(key: str) -> int:
    if key == "ADMIN-TEST-PASS":
        return 0
    try:
        url = f"{st.secrets['UPSTASH_URL']}/get/{key}"
        headers = {"Authorization": f"Bearer {st.secrets['UPSTASH_TOKEN']}"}
        r = requests.get(url, headers=headers, timeout=5).json()
        val = r.get("result")
        return int(val) if val is not None else 0
    except Exception:
        return 0


def increment_audits(key: str, count: int) -> int:
    if key == "ADMIN-TEST-PASS":
        return 0
    try:
        url = f"{st.secrets['UPSTASH_URL']}/incrby/{key}/{count}"
        headers = {"Authorization": f"Bearer {st.secrets['UPSTASH_TOKEN']}"}
        r = requests.get(url, headers=headers, timeout=5).json()
        return int(r.get("result", 0))
    except Exception:
        return 0


# License Key Validation
def verify_lemon_license(license_key: str) -> tuple[bool, str]:
    key = license_key.strip()
    if not key:
        return False, "Please enter a license key."
    if key == "ADMIN-TEST-PASS":
        return True, "Admin bypass granted."

    url = "https://api.lemonsqueezy.com/v1/licenses/validate"
    headers = {"Accept": "application/json"}
    payload = {"license_key": key}

    try:
        response = requests.post(url, data=payload, headers=headers, timeout=10)
        data = response.json()

        if not data.get("valid"):
            return False, data.get("error", "Invalid or inactive license key.")

        status = data.get("license_key", {}).get("status")
        if status not in ["active", "inactive"]:
            return False, f"License is {status}."

        return True, "License verified successfully."
    except Exception as e:
        return False, f"Verification service unavailable: {e}"


# Sidebar Authentication
st.sidebar.title("Account")
input_key = st.sidebar.text_input("Enter License Key", type="password")

# URL Query Param Auto-Login
params = st.query_params
if "key" in params and not st.session_state.get("authenticated", False):
    valid, msg = verify_lemon_license(params["key"])
    if valid:
        st.session_state["authenticated"] = True
        st.session_state["license_key"] = params["key"].strip()

# Manual Verification
if st.sidebar.button("Verify License"):
    valid, msg = verify_lemon_license(input_key)
    if valid:
        st.session_state["authenticated"] = True
        st.session_state["license_key"] = input_key.strip()
        st.query_params["key"] = input_key.strip()
        st.sidebar.success(msg)
    else:
        st.session_state["authenticated"] = False
        st.session_state["license_key"] = ""
        st.sidebar.error(msg)

# Gate Unauthenticated Users
if not st.session_state.get("authenticated", False):
    st.info("Please enter a valid license key in the sidebar to use SiteScout.")
    st.markdown(
        "[Get a License Key ($25)](https://sitescout-app.lemonsqueezy.com/checkout/buy/4bbf5eb6-c7d8-4a7f-bc76-8b0dd0bbae3b)"
    )
    st.stop()

# Sidebar Metric Placeholder
current_key = st.session_state.get("license_key", "")
used_count = get_audits_used(current_key)
remaining = max(0, AUDIT_LIMIT - used_count)

st.sidebar.divider()
quota_display = st.sidebar.empty()
quota_display.metric("Audits Remaining", f"{remaining} / {AUDIT_LIMIT}")

# Main Input
urls_input = st.text_area(
    "Target Website URLs (one per line)",
    placeholder="berkshirehathaway.com\nstripe.com\nexample.com",
    height=140,
)

if st.button("Run Batch Audit", type="primary"):
    raw_urls = list(
        dict.fromkeys(
            [line.strip() for line in urls_input.splitlines() if line.strip()]
        )
    )

    if not raw_urls:
        st.warning("Please enter at least one URL.")
    elif len(raw_urls) > MAX_BATCH_SIZE:
        st.error(f"Batch limit exceeded. Max {MAX_BATCH_SIZE} URLs per run.")
    elif len(raw_urls) > remaining:
        st.error(
            f"Quota exceeded. You only have {remaining} audits remaining on your license."
        )
    else:
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        for idx, url in enumerate(raw_urls):
            status_text.text(f"Auditing {idx + 1}/{len(raw_urls)}: {url}")
            audit_data = audit_website(url)

            pitch = (
                "N/A - Site unreachable"
                if "Failed" in audit_data["status"]
                else generate_pitch(audit_data)
            )

            results.append(
                {
                    "URL": audit_data["url"],
                    "Title": audit_data["title"] or "N/A",
                    "Load Time (s)": audit_data["load_time_sec"] or "N/A",
                    "SSL Secure": "Yes" if audit_data["is_https"] else "No",
                    "Mobile Ready": (
                        "Yes" if audit_data["has_mobile_viewport"] else "No"
                    ),
                    "Status": audit_data["status"],
                    "Generated Pitch": pitch,
                }
            )
            progress_bar.progress((idx + 1) / len(raw_urls))

        # Save to database and update state
        new_total = increment_audits(current_key, len(raw_urls))
        new_remaining = max(0, AUDIT_LIMIT - new_total)
        quota_display.metric("Audits Remaining", f"{new_remaining} / {AUDIT_LIMIT}")

        status_text.empty()
        progress_bar.empty()

        st.session_state["last_results"] = results

# Display Persistent Results
if "last_results" in st.session_state:
    st.subheader("Audit Results")
    for row in st.session_state["last_results"]:
        with st.expander(f"{row['URL']} - {row['Status']}", expanded=True):
            c1, c2, c3 = st.columns(3)
            c1.metric("Load Time", f"{row['Load Time (s)']}")
            c2.metric("SSL Secure", row["SSL Secure"])
            c3.metric("Mobile Ready", row["Mobile Ready"])
            st.text_area(
                "Cold Pitch",
                value=row["Generated Pitch"],
                height=90,
                key=f"pitch_{row['URL']}",
            )

    csv_buffer = io.StringIO()
    fieldnames = [
        "URL",
        "Title",
        "Load Time (s)",
        "SSL Secure",
        "Mobile Ready",
        "Status",
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
