import os
import json
import time
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
import feedparser
from feedgen.feed import FeedGenerator
from google import genai
from google.genai import types
from pydantic import BaseModel

# Configuration
INTERESTS_FILE = "interests.txt"
ARCHIVE_FILE = "archive.json"
PROXY_DB_FILE = "proxy_db.json"
OUTPUT_DIR = "public"
BATCH_SIZE = 5
SINGLE_FEED_ID = "bbc_news_ai_filtered"
FEED_URL = "https://lincoln149.alwaysdata.net/freshrss/api/query.php?user=lincoln149&t=3yPwwxjIWkQrUzb9j75NA3&f=rss"

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

api_key_index = 0

def evaluate_batch(prompt, api_keys, expected_count):
    global api_key_index
    
    for model_name in MODELS_TO_TRY:
        for _ in range(len(api_keys)):
            current_key = api_keys[api_key_index]
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
        
    # Updated to look for GEMINI_API_KEY
    keys_env = os.environ.get("GEMINI_API_KEY", "")
    api_keys = [k.strip() for k in keys_env.split(',') if k.strip()]
    
    if not api_keys:
        raise ValueError("No API keys found in the GEMINI_API_KEY environment variable.")
        
    archive = load_json(ARCHIVE_FILE, [])
    proxy_db = load_json(PROXY_DB_FILE, {})
    
    # Initialize the single feed structure if it doesn't exist
    if SINGLE_FEED_ID not in proxy_db:
        proxy_db[SINGLE_FEED_ID] = {
            "title": "BBC News AI Filtered",
            "link": "https://www.bbc.co.uk/news",
            "description": "AI Filtered Articles combined into a single feed.",
            "articles": []
        }
            
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(hours=24)
    
    # Parse the specific FreshRSS URL
    parsed = feedparser.parse(FEED_URL)
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
        
    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i:i+BATCH_SIZE]
        
        prompt = f"User filtering criteria:\n{interests}\n\nEvaluate if these {len(batch)} articles align with the user's interests. An article MUST be rejected (marked false) if it matches any of the NON-INTERESTS. Return exactly {len(batch)} boolean values in the exact order of the articles provided.\n\n"
        
        for idx, art in enumerate(batch):
            prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nSummary: {art.get('summary', '')}\n\n"
            
        evaluations = evaluate_batch(prompt, api_keys, len(batch))
        
        if evaluations:
            for idx, eval_result in enumerate(evaluations):
                art = batch[idx]
                art_id = art.get('id', art.get('link'))
                
                archive.append(art_id)
                
                if eval_result.is_interesting:
                    proxy_db[SINGLE_FEED_ID]['articles'].append({
                        'id': art_id,
                        'title': art.get('title', 'No Title'),
                        'link': art.get('link', ''),
                        'description': art.get('summary', ''),
                        'published': art.get('published', art.get('updated', ''))
                    })
        
        time.sleep(1)
            
    # Sort articles by publication date (newest first) and keep the 100 most recent
    def get_pub_time(article):
        pub_str = article.get('published', '')
        if pub_str:
            try:
                dt = date_parser.parse(pub_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    proxy_db[SINGLE_FEED_ID]['articles'].sort(key=get_pub_time, reverse=True)
    proxy_db[SINGLE_FEED_ID]['articles'] = proxy_db[SINGLE_FEED_ID]['articles'][:100]

    save_json(ARCHIVE_FILE, archive)
    save_json(PROXY_DB_FILE, proxy_db)
    
    # Generate the single Proxy RSS Feed
    feed_data = proxy_db[SINGLE_FEED_ID]
    if feed_data['articles']:
        fg = FeedGenerator()
        fg.id(feed_data['link'])
        fg.title(feed_data['title']) 
        fg.link(href=feed_data['link'], rel='alternate')
        fg.description(feed_data['description']) 
        
        for art in feed_data['articles']:
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
                
        fg.rss_file(f"{OUTPUT_DIR}/BBC_News_AI_Filtered.xml")

if __name__ == "__main__":
    main()
