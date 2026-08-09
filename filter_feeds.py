import os
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
import feedparser
from feedgen.feed import FeedGenerator
from google import genai
from google.genai import types
from pydantic import BaseModel

# Configuration
OPML_FILE = "feeds.opml"
INTERESTS_FILE = "interests.txt"
ARCHIVE_FILE = "archive.json"
PROXY_DB_FILE = "proxy_db.json"
OUTPUT_DIR = "public"
BATCH_SIZE = 5

# Fallback sequence
MODELS_TO_TRY = [
    "gemini-3.5-flash-lite", 
    "gemini-3.1-flash-lite", 
    "gemini-2.5-flash-lite"
]

class ArticleEvaluation(BaseModel):
    is_interesting: bool

class BatchEvaluation(BaseModel):
    results: list[ArticleEvaluation]

def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

# Global index to maintain round-robin rotation across batches
api_key_index = 0

def evaluate_batch(prompt, api_keys, expected_count):
    global api_key_index
    
    # Iterate through models in fallback order
    for model_name in MODELS_TO_TRY:
        # Try up to the number of available API keys for the current model
        for _ in range(len(api_keys)):
            current_key = api_keys[api_key_index]
            
            # Advance the global index for the next request
            api_key_index = (api_key_index + 1) % len(api_keys)
            
            client = genai.Client(api_key=current_key)
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=BatchEvaluation,
                        temperature=0.1
                    )
                )
                if response.parsed and len(response.parsed.results) == expected_count:
                    return response.parsed.results
            except Exception as e:
                error_msg = str(e).lower()
                # Intercept rate/quota limits
                if "429" in error_msg or "quota" in error_msg or "resource exhausted" in error_msg:
                    print(f"Rate limit hit on {model_name} with key ending in ...{current_key[-4:]}. Rotating key.")
                    continue
                else:
                    print(f"Error on {model_name}: {e}. Rotating key.")
                    continue
                    
        print(f"All {len(api_keys)} keys exhausted for {model_name}. Falling back to previous model.")
        
    print("All models and keys exhausted for this batch.")
    return None

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    with open(INTERESTS_FILE, 'r', encoding='utf-8') as f:
        interests = f.read()
        
    # Load and clean the comma-separated list of API keys
    keys_env = os.environ.get("GEMINI_API_KEYS", "")
    api_keys = [k.strip() for k in keys_env.split(',') if k.strip()]
    
    if not api_keys:
        raise ValueError("No API keys found in the GEMINI_API_KEYS environment variable.")
        
    archive = load_json(ARCHIVE_FILE, [])
    proxy_db = load_json(PROXY_DB_FILE, {})
    
    # 1. Extract feeds from OPML
    feeds = []
    tree = ET.parse(OPML_FILE)
    for outline in tree.getroot().iter('outline'):
        xml_url = outline.attrib.get('xmlUrl')
        if xml_url:
            title = outline.attrib.get('title') or outline.attrib.get('text') or "Unknown Feed"
            feeds.append({'title': title, 'url': xml_url})
            
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(hours=24)
    
    # 2. Process feeds
    for feed_info in feeds:
        url = feed_info['url']
        if url not in proxy_db:
            proxy_db[url] = {"title": feed_info['title'], "articles": []}
            
        parsed = feedparser.parse(url)
        to_process = []
        
        for entry in parsed.entries:
            entry_id = entry.get('id', entry.get('link', str(time.time())))
            if entry_id in archive:
                continue
                
            pub_date = entry.get('published') or entry.get('updated')
            if pub_date:
                try:
                    dt = date_parser.parse(pub_date)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < time_threshold:
                        continue
                except Exception:
                    pass
            
            to_process.append(entry)
            
        # 3. Batch and evaluate with rotation and fallback
        for i in range(0, len(to_process), BATCH_SIZE):
            batch = to_process[i:i+BATCH_SIZE]
            
            prompt = f"User interests:\n{interests}\n\nEvaluate if these {len(batch)} articles align with the user's interests. Return exactly {len(batch)} boolean values in the exact order of the articles provided.\n\n"
            for idx, art in enumerate(batch):
                prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nSummary: {art.get('summary', '')[:600]}\n\n"
                
            evaluations = evaluate_batch(prompt, api_keys, len(batch))
            
            if evaluations:
                for idx, eval_result in enumerate(evaluations):
                    art = batch[idx]
                    art_id = art.get('id', art.get('link'))
                    
                    # Log to archive regardless of outcome to prevent reprocessing
                    archive.append(art_id)
                    
                    if eval_result.is_interesting:
                        proxy_db[url]['articles'].append({
                            'id': art_id,
                            'title': art.get('title', 'No Title'),
                            'link': art.get('link', ''),
                            'description': art.get('summary', ''),
                            'published': art.get('published', art.get('updated', ''))
                        })
            
            # Brief pause to smooth out requests across the current key
            time.sleep(1)
            
        # Keep only the 50 most recent matched articles to prevent infinite database growth
        proxy_db[url]['articles'] = proxy_db[url]['articles'][-50:]

    # Save state files for the next run
    save_json(ARCHIVE_FILE, archive)
    save_json(PROXY_DB_FILE, proxy_db)
    
    # 4. Generate Proxy RSS Feeds
    for url, data in proxy_db.items():
        if not data['articles']:
            continue
            
        fg = FeedGenerator()
        fg.id(url)
        fg.title(f"Proxy - {data['title']}")
        fg.link(href=url, rel='alternate')
        fg.description(f"AI Filtered Feed for: {data['title']}")
        
        for art in data['articles']:
            fe = fg.add_entry()
            fe.id(art['id'])
            fe.title(art['title'])
            fe.link(href=art['link'])
            fe.description(art['description'])
            if art['published']:
                try:
                    dt = date_parser.parse(art['published'])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    fe.pubDate(dt)
                except Exception:
                    pass
                
        safe_title = "".join([c if c.isalnum() else "_" for c in data['title']])
        fg.rss_file(f"{OUTPUT_DIR}/{safe_title}.xml")

if __name__ == "__main__":
    main()
