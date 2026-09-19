import os
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import streamlit as st

# Safe client initialization (checks Streamlit secrets first, then OS env)
api_key = st.secrets.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY"))
client = OpenAI(api_key=api_key)

BLOCKED_TARGETS = ["localhost", "127.0.0.1", "169.254.169.254", "0.0.0.0"]


def audit_website(url: str) -> dict:
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    # SSRF Protection
    for blocked in BLOCKED_TARGETS:
        if blocked in url.lower():
            return {
                "url": url,
                "is_https": False,
                "load_time_sec": None,
                "has_mobile_viewport": False,
                "title": "Blocked URL",
                "status": "Failed: Invalid or restricted address",
            }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    report = {
        "url": url,
        "is_https": url.startswith("https://"),
        "load_time_sec": None,
        "has_mobile_viewport": False,
        "title": None,
        "status": "Success",
    }

    try:
        start_time = time.time()
        # Stream response to inspect headers without pulling massive payloads
        response = requests.get(url, headers=headers, timeout=10, stream=True)
        report["load_time_sec"] = round(time.time() - start_time, 2)

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            report["status"] = "Skipped: Target is not an HTML page"
            return report

        if response.status_code != 200:
            report["status"] = f"Failed with status code {response.status_code}"
            return report

        # Read only up to 500KB to protect RAM
        chunk = next(response.iter_content(512 * 1024), b"")
        content = chunk.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(content, "html.parser")

        # Safe title extraction
        if soup.title and soup.title.get_text(strip=True):
            report["title"] = soup.title.get_text(strip=True)[:80]
        else:
            report["title"] = "No title found"

        viewport = soup.find("meta", attrs={"name": "viewport"})
        if viewport:
            report["has_mobile_viewport"] = True

    except requests.exceptions.SSLError:
        report["is_https"] = False
        report["status"] = "SSL certificate invalid or missing"
    except requests.exceptions.Timeout:
        report["status"] = "Connection timed out"
    except requests.exceptions.ConnectionError:
        report["status"] = "Host unreachable or connection refused"
    except requests.exceptions.RequestException:
        report["status"] = "Connection failed"

    return report


def generate_pitch(audit_data: dict) -> str:
    if "Failed" in audit_data["status"] or "Skipped" in audit_data["status"]:
        return "Could not generate pitch: Target site is invalid or unreachable."

    flaws = []
    if not audit_data["is_https"]:
        flaws.append(
            "Site lacks an SSL certificate (shows as 'Not Secure' in browsers)"
        )
    if audit_data["load_time_sec"] and audit_data["load_time_sec"] > 2.5:
        flaws.append(f"Load time is {audit_data['load_time_sec']}s (ideal is under 2s)")
    if not audit_data["has_mobile_viewport"]:
        flaws.append(
            "Missing mobile viewport tag (renders zoomed out and broken on smartphones)"
        )

    if not flaws:
        flaws.append(
            f"Fast speed ({audit_data['load_time_sec']}s) but dated layout structure"
        )

    system_prompt = (
        "You are an independent freelance web designer sending a brief cold note. "
        "Strict rules:\n"
        "- Exactly 3 sentences. Under 55 words.\n"
        "- Tone: casual, understated, peer-to-peer.\n"
        "- No exclamation marks. No corporate buzzwords.\n"
        "- Never use phrases like 'Let's chat', 'boost your business', 'elevate', or 'I hope this finds you well'.\n"
        "- Sentence 1: Note the specific technical issue observed while checking their site on your phone.\n"
        "- Sentence 2: State the plain consequence (e.g., visitors leave before seeing the booking button).\n"
        "- Sentence 3: Low-friction closing asking permission to send a 30-second screen recording showing how to fix it."
    )

    user_prompt = f"""
    Target Site: {audit_data['title']} ({audit_data['url']})
    Detected Issues: {', '.join(flaws)}

    Write the 3-sentence note.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=90,
            temperature=0.5,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Pitch generation temporarily unavailable: {e}"
