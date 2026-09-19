import os
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# Initialize OpenAI client
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def audit_website(url):
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

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
        response = requests.get(url, headers=headers, timeout=10)
        report["load_time_sec"] = round(time.time() - start_time, 2)

        if response.status_code != 200:
            report["status"] = f"Failed with status code {response.status_code}"
            return report

        soup = BeautifulSoup(response.text, "html.parser")
        report["title"] = soup.title.string.strip() if soup.title else "No title found"

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


def generate_pitch(audit_data):
    if "Failed" in audit_data["status"]:
        return "Could not generate pitch: Site unreachable."

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


def main():
    target = input("Enter website URL: ").strip()
    results = audit_website(target)

    print("\n--- Audit Results ---")
    for key, value in results.items():
        print(f"{key}: {value}")

    print("\n--- Generated Pitch ---")
    pitch = generate_pitch(results)
    print(pitch)


if __name__ == "__main__":
    main()
