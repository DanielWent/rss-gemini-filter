import os
import sys
import re
import json
import time
import base64
import datetime
from datetime import timezone
from typing import List, Dict, Any, Literal
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from feedgen.feed import FeedGenerator

# Force unbuffered output for live GitHub Actions logs
sys.stdout.reconfigure(line_buffering=True)

ROOT_FOLDER_IDS = [
    "1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5",
    "1Wycq7k8Wsh4bzZLociKkc_tMJmiIGsEX",
]

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

ARCHIVE_FILE = "bwcc_archive.json"
FEED_FILE = "bwcc_feed.xml"
FEED_TITLE = "Bearsden West Community Council"
FEED_LINK = "https://drive.google.com/drive/folders/1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5"
FEED_DESCRIPTION = "Automated AI summaries of Bearsden West Community Council minutes, budgets, and public documents."


class DocumentSummary(BaseModel):
    doc_type: Literal["minutes", "budget", "other"] = Field(
        description="Classification of the document."
    )
    title: str = Field(
        description="Strictly formatted title adhering to project rules."
    )
    summary_html: str = Field(
        description="Detailed summary in clean HTML (using <h3>, <p>, <ul>, <li>, <strong>)."
    )


class KeyManager:
    """Manages rotation and cooldown state across multiple Gemini API keys."""

    def __init__(self):
        raw_env = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or ""
        )
        self.keys = [
            k.strip().strip("'").strip('"')
            for k in raw_env.split(",")
            if k.strip().strip("'").strip('"')
        ]
        if not self.keys:
            raise ValueError("No valid API keys found in GEMINI_API_KEY / GOOGLE_API_KEY.")

        self.current_index = 0
        # Track when each key was rate-limited
        self.cooldowns: Dict[int, float] = {i: 0.0 for i in range(len(self.keys))}

    def get_current_key(self) -> str:
        return self.keys[self.current_index]

    def mark_rate_limited(self) -> None:
        """Mark the active key as throttled for 60 seconds."""
        self.cooldowns[self.current_index] = time.time() + 60.0

    def rotate_to_next_available(self) -> None:
        """Rotate to the next key that is not currently in a cooldown window."""
        now = time.time()
        for i in range(1, len(self.keys) + 1):
            idx = (self.current_index + i) % len(self.keys)
            if self.cooldowns[idx] <= now:
                self.current_index = idx
                return

        # If all keys are in cooldown, pick the one closest to expiring
        earliest_idx = min(self.cooldowns, key=self.cooldowns.get)
        self.current_index = earliest_idx

    def get_pool_wait_time(self) -> float:
        """Return the seconds remaining until at least one key leaves cooldown."""
        now = time.time()
        ready_keys = [idx for idx, expiry in self.cooldowns.items() if expiry <= now]
        if ready_keys:
            return 0.0
        earliest_expiry = min(self.cooldowns.values())
        return max(0.0, earliest_expiry - now)


def load_archive() -> Dict[str, Any]:
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_archive(archive: Dict[str, Any]) -> None:
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, indent=2, ensure_ascii=False)


def scrape_public_drive_folder(folder_id: str) -> List[Dict[str, Any]]:
    """Scrape item listings from a public Google Drive embedded folder view."""
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    entries = soup.find_all("div", class_=re.compile(r"flip-entry"))
    if not entries:
        entries = soup.find_all("tr", class_=re.compile(r"flip-entry"))

    for entry in entries:
        link_elem = entry.find("a", href=True)
        if not link_elem:
            continue

        href = link_elem["href"]
        name = link_elem.get_text(strip=True)

        folder_match = re.search(r"folders/([a-zA-Z0-9_-]+)", href) or re.search(r"id=([a-zA-Z0-9_-]+)", href)
        is_folder = "folder" in href or "folder" in entry.get("class", [])
        file_match = re.search(r"file/d/([a-zA-Z0-9_-]+)", href) or re.search(r"id=([a-zA-Z0-9_-]+)", href)

        if is_folder and folder_match:
            items.append({
                "id": folder_match.group(1),
                "name": name,
                "is_folder": True,
            })
        elif file_match:
            items.append({
                "id": file_match.group(1),
                "name": name,
                "is_folder": False,
                "webViewLink": f"https://drive.google.com/file/d/{file_match.group(1)}/view",
            })

    return items


def scan_drive_folders_recursively(folder_ids: List[str]) -> List[Dict[str, Any]]:
    """Recursively traverse public Google Drive folders and gather all unique PDF files."""
    discovered_pdfs = {}
    folders_to_scan = list(folder_ids)
    scanned_folders = set()

    while folders_to_scan:
        current_folder = folders_to_scan.pop(0)
        if current_folder in scanned_folders:
            continue
        scanned_folders.add(current_folder)

        try:
            items = scrape_public_drive_folder(current_folder)
            for item in items:
                if item["is_folder"]:
                    if item["id"] not in scanned_folders:
                        folders_to_scan.append(item["id"])
                elif item["name"].lower().endswith(".pdf"):
                    discovered_pdfs[item["id"]] = item
        except Exception as e:
            print(f"Error scraping folder {current_folder}: {e}")

    return list(discovered_pdfs.values())


def download_public_drive_pdf(file_id: str) -> bytes:
    """Download binary content of a publicly shared Google Drive file."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    resp = session.get(url, allow_redirects=True)
    resp.raise_for_status()

    if "confirm=" not in resp.url and "Google Drive - Virus scan warning" in resp.text:
        match = re.search(r'confirm=([0-9A-Za-z_]+)', resp.text)
        if match:
            confirm_code = match.group(1)
            confirm_url = f"{url}&confirm={confirm_code}"
            resp = session.get(confirm_url, allow_redirects=True)
            resp.raise_for_status()

    return resp.content


def summarize_pdf_with_gemini(km: KeyManager, pdf_bytes: bytes, filename: str) -> DocumentSummary:
    """Process PDF binary with persistent rate-limit waiting, never dropping documents on 429s."""
    prompt = f"""
You are analyzing a public document from the Bearsden West Community Council ("{filename}").

STRICT TITLE CONVENTIONS:
1. Meeting Minutes: MUST be formatted exactly as: "Bearsden West CC Minutes - <Month YYYY>"
   (e.g., "Bearsden West CC Minutes - June 2025")
2. Budget / Accounts / Financial statements: MUST be formatted exactly as: "Bearsden West CC Budget - <YYYY/YY>"
   (e.g., "Bearsden West CC Budget - 2025/26")
3. Other documents: MUST be formatted as: "Bearsden West CC - <Document Topic> - <Month YYYY or Date>"

CONTENT SUMMARY REQUIREMENTS:
- Provide a detailed, comprehensive, and well-structured HTML summary using <h3>, <p>, <ul>, <li>, and <strong> tags.
- Detail key discussions, decisions, planning applications, local council updates, police reports, and financial expenditures.
"""

    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": b64_pdf
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "doc_type": {
                        "type": "STRING",
                        "enum": ["minutes", "budget", "other"]
                    },
                    "title": {
                        "type": "STRING"
                    },
                    "summary_html": {
                        "type": "STRING"
                    }
                },
                "required": ["doc_type", "title", "summary_html"]
            }
        }
    }

    headers = {"Content-Type": "application/json"}

    # Loop indefinitely on 429 rate limits until the file succeeds
    while True:
        wait_needed = km.get_pool_wait_time()
        if wait_needed > 0:
            print(f"All keys throttled. Pausing {int(wait_needed) + 2}s for rolling window reset...")
            time.sleep(wait_needed + 2.0)

        current_key = km.get_current_key()
        key_num = km.current_index + 1

        for model_name in MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={current_key}"
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    result_json = resp.json()
                    raw_text = result_json["candidates"][0]["content"]["parts"][0]["text"]
                    parsed_dict = json.loads(raw_text)
                    return DocumentSummary(**parsed_dict)

                if resp.status_code == 429:
                    print(f"Key #{key_num} hit 429 on {model_name}. Rotating...")
                    km.mark_rate_limited()
                    km.rotate_to_next_available()
                    time.sleep(1)
                    break  # Break model loop, try next key

                elif resp.status_code in [401, 403]:
                    print(f"Key #{key_num} auth error {resp.status_code}. Rotating...")
                    km.rotate_to_next_available()
                    time.sleep(1)
                    break

                elif resp.status_code == 404:
                    continue  # Try next model in list

                else:
                    # Non-recoverable client error (e.g. 400 Bad Request / corrupted PDF)
                    print(f"Permanent HTTP {resp.status_code} on {model_name}: {resp.text[:120]}")
                    raise RuntimeError(f"Unrecoverable API error {resp.status_code}: {resp.text[:100]}")

            except requests.exceptions.RequestException as e:
                print(f"Network exception on key #{key_num}: {e}. Rotating...")
                km.rotate_to_next_available()
                time.sleep(1)
                break


def update_rss_feed(archive: Dict[str, Any]) -> None:
    """Generate or overwrite bwcc_feed.xml from all archived items."""
    fg = FeedGenerator()
    fg.title(FEED_TITLE)
    fg.link(href=FEED_LINK, rel="alternate")
    fg.description(FEED_DESCRIPTION)
    fg.language("en-gb")

    sorted_items = sorted(
        archive.values(),
        key=lambda x: x.get("processed_at", ""),
        reverse=True,
    )

    for item in sorted_items:
        fe = fg.add_entry()
        fe.id(item.get("webViewLink") or f"bwcc-{item['id']}")
        fe.title(item["title"])
        fe.link(href=item.get("webViewLink") or FEED_LINK)
        fe.description(item["summary_html"])

        if item.get("processed_at"):
            pub_date = datetime.datetime.fromisoformat(item["processed_at"])
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            fe.pubDate(pub_date)

    fg.rss_file(FEED_FILE, pretty=True)


def main():
    km = KeyManager()
    print(f"Loaded {len(km.keys)} Gemini API keys into key pool.")
    print(f"Active model hierarchy: {MODELS}")

    archive = load_archive()
    print(f"Archive loaded: {len(archive)} items previously processed.")

    print("Querying Google Drive folders recursively...")
    all_pdfs = scan_drive_folders_recursively(ROOT_FOLDER_IDS)
    print(f"Found {len(all_pdfs)} unique PDF files across target directories.")

    new_count = 0

    for pdf in all_pdfs:
        file_id = pdf["id"]
        filename = pdf.get("name", "Document.pdf")

        if file_id in archive:
            continue

        print(f"Processing new file: {filename} ({file_id})")
        try:
            pdf_bytes = download_public_drive_pdf(file_id)
            parsed_summary = summarize_pdf_with_gemini(km, pdf_bytes, filename)

            archive[file_id] = {
                "id": file_id,
                "name": filename,
                "title": parsed_summary.title,
                "doc_type": parsed_summary.doc_type,
                "summary_html": parsed_summary.summary_html,
                "webViewLink": pdf.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view"),
                "processed_at": datetime.datetime.now(timezone.utc).isoformat(),
            }
            new_count += 1
            print(f"Added: {parsed_summary.title}")

            # Persist progress to disk after every single file
            save_archive(archive)
            
            # Step to next key and add brief delay to smooth out TPM usage
            km.rotate_to_next_available()
            time.sleep(2.5)

        except Exception as e:
            # Only genuinely unrecoverable file errors (corrupted binary, 400 Bad Request) land here
            print(f"Permanently skipping unparseable file {filename}: {e}")

    if new_count > 0 or not os.path.exists(ARCHIVE_FILE):
        save_archive(archive)

    if new_count > 0 or not os.path.exists(FEED_FILE):
        update_rss_feed(archive)

    if new_count > 0:
        print(f"Successfully processed {new_count} new document(s) and refreshed {FEED_FILE}.")
    else:
        print("No new documents detected. State and feed files are up to date.")


if __name__ == "__main__":
    main()
