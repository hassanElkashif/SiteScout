import streamlit as st
import csv
import io
from scout import audit_website, generate_pitch
import requests

st.set_page_config(page_title="SiteScout", layout="wide")

st.title("SiteScout")
st.caption(
    "Batch audit business websites and generate personalized cold outreach pitches."
)

# Multi-line input for bulk URLs
urls_input = st.text_area(
    "Target Website URLs (one per line)",
    placeholder="berkshirehathaway.com\nstripe.com\nexample.com",
    height=140,
)


def verify_license_key(license_key):
    if not license_key:
        return False, "Please enter a license key."

    # Bypass for admin/testing
    if license_key == "ADMIN-TEST-PASS":
        return True, "Admin bypass active"

    url = "https://api.lemonsqueezy.com/v1/licenses/activate"
    payload = {"license_key": license_key, "instance_name": "SiteScout Web User"}
    try:
        response = requests.post(url, data=payload, timeout=10)
        data = response.json()
        if data.get("activated"):
            return True, "License active"
        else:
            return False, data.get("error", "Invalid or inactive license key.")
    except Exception:
        return False, "License server unreachable."


# Sidebar Access Control
st.sidebar.title("Account")
license_key = st.sidebar.text_input("Enter License Key", type="password")

if st.sidebar.button("Verify License"):
    valid, msg = verify_license_key(license_key)
    if valid:
        st.session_state["authenticated"] = True
        st.sidebar.success("Access granted.")
    else:
        st.session_state["authenticated"] = False
        st.sidebar.error(msg)

if not st.session_state.get("authenticated", False):
    st.info("Please enter a valid license key in the sidebar to use SiteScout.")
    st.stop()


if st.button("Run Batch Audit", type="primary"):
    raw_urls = [line.strip() for line in urls_input.splitlines() if line.strip()]

    if not raw_urls:
        st.warning("Please enter at least one URL.")
    else:
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        for idx, url in enumerate(raw_urls):
            status_text.text(f"Auditing {idx + 1}/{len(raw_urls)}: {url}")

            audit_data = audit_website(url)

            if "Failed" in audit_data["status"]:
                pitch = "N/A - Site unreachable"
            else:
                pitch = generate_pitch(audit_data)

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

        status_text.empty()
        progress_bar.empty()

        st.subheader("Audit Results")

        # Display results in cards
        for row in results:
            with st.expander(f"{row['URL']} - {row['Status']}", expanded=True):
                col1, col2, col3 = st.columns(3)
                col1.metric("Load Time", f"{row['Load Time (s)']}")
                col2.metric("SSL Secure", row["SSL Secure"])
                col3.metric("Mobile Ready", row["Mobile Ready"])

                st.text_area(
                    "Cold Pitch",
                    value=row["Generated Pitch"],
                    height=90,
                    key=f"pitch_{row['URL']}",
                )

        # CSV Export Generator
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
        writer.writerows(results)

        st.download_button(
            label="Download Results as CSV",
            data=csv_buffer.getvalue(),
            file_name="sitescout_leads.csv",
            mime="text/csv",
        )
