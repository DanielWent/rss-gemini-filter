import os
import re
import json
import time
import tempfile
import datetime
from datetime import timezone
from typing import List, Dict, Any, Literal
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from feedgen.feed import FeedGenerator

ROOT_FOLDER_IDS = [
    "1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5",
    "1Wycq7k8Wsh4bzZLociKkc_tMJmiIGsEX",
]

ARCHIVE_FILE = "bwcc_archive.json"
FEED_FILE = "bwcc_feed.xml"
FEED_TITLE = "Bearsden West Community Council"
FEED_LINK = "https://drive.google.com/drive/folders/1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5"
FEED_DESCRIPTION = "Automated AI summaries of Bearsden West Community Council minutes, budgets, and public documents."


class DocumentSummary(BaseModel):
    doc_type: Literal["minutes", "budget", "other"] = Field(
        description="The classification of the document."
    )
    title: str = Field(
        description="Strictly formatted title adhering to naming rules."
    )
    summary_html: str = Field(
        description="Comprehensive summary formatted in clean HTML (using <h3>, <p>, <ul>, <li>, <strong>)."
    )


def get_genai_client() -> genai.Client:
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment.")
    
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta"),
    )


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


def summarize_pdf_with_gemini(client: genai.Client, pdf_bytes: bytes, filename: str) -> DocumentSummary:
    """Upload PDF to Gemini Files API and analyze with structured Pydantic schema."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    uploaded_file = None
    try:
        uploaded_file = client.files.upload(
            file=tmp_path,
            config=types.UploadFileConfig(
                display_name=filename,
                mime_type="application/pdf"
            )
        )

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

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DocumentSummary,
            ),
        )

        return response.parsed

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


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
    client = get_genai_client()
    archive = load_archive()
    print(f"Archive loaded: {len(archive)} items previously processed.")

    print("Querying Google Drive folders recursively via public view...")
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
            parsed_summary: DocumentSummary = summarize_pdf_with_gemini(client, pdf_bytes, filename)

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
            
            # Persist archive incrementally after each successful item
            save_archive(archive)
            time.sleep(1)  # Modest delay between consecutive calls
        except Exception as e:
            print(f"Failed to process {filename}: {e}")

    if new_count > 0 or not os.path.exists(FEED_FILE):
        update_rss_feed(archive)

    if new_count > 0:
        print(f"Successfully processed {new_count} new document(s) and refreshed {FEED_FILE}.")
    else:
        print("No new documents detected. State and feed files are up to date.")


if __name__ == "__main__":
    main()
