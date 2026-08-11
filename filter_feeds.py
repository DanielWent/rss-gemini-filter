import sys
print("[BOOT] Starting script execution...", flush=True)

import os
import json
import time
import math
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
import feedparser
from feedgen.feed import FeedGenerator
from google import genai
from google.genai import types
from pydantic import BaseModel

print("[BOOT] All modules imported successfully.", flush=True)

# Prevent any underlying socket from hanging infinitely
socket.setdefaulttimeout(30)

# Configuration
INTERESTS_FILE = "interests.txt"
ARCHIVE_FILE = "archive.json"
PROXY_DB_FILE = "proxy_db.json"
OUTPUT_DIR = "public"
BATCH_SIZE = 5
SINGLE_FEED_ID = "bbc_news_ai_filtered"
FEED_URL = "https://lincoln149.alwaysdata.net/freshrss/api/query.php?user=lincoln149&t=3yPwwxjIWkQrUzb9j75NA3&f=rss"

# Rate Limiting & Quota Management State
MODELS_TO_TRY = [
    "gemini-3.5-flash-lite", 
    "gemini-3.1-flash-lite", 
    "gemini-2.5-flash-lite"
]

MODEL_LIMITS = {
    "gemini-3.5-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-3.1-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-2.5-flash-lite": {"rpm": 14, "tpm": 240000}
}

key_states = {}
current_key_index = 0
api_keys_list = []

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

def get_available_key(model, estimated_tokens):
    global current_key_index, key_states, api_keys_list
    limits = MODEL_LIMITS.get(model, {"rpm": 14, "tpm": 240000})
    now = time.time() * 1000 
    minute_ago = now - 60000

    min_wait_time = float('inf')
    all_exhausted = True
    num_keys = len(api_keys_list)

    if num_keys == 0:
        return None, None, -1

    for i in range(num_keys):
        key_idx = (current_key_index + i) % num_keys
        key = api_keys_list[key_idx]
        state_id = f"{model}_{key}"
        
        if state_id not in key_states:
            key_states[state_id] = {
                'requests': [], 
                'tokens': [], 
                'status': 'active', 
                'cooldown_until': 0, 
                'consecutive_generic_429s': 0
            }
        state = key_states[state_id]

        if state['status'] == 'exhausted':
            continue
            
        all_exhausted = False

        state['requests'] = [ts for ts in state['requests'] if ts > minute_ago]
        state['tokens'] = [t for t in state['tokens'] if t['ts'] > minute_ago]

        if now < state['cooldown_until']:
            wait = state['cooldown_until'] - now
            if wait < min_wait_time: 
                min_wait_time = wait
            continue

        current_rpm = len(state['requests'])
        current_tpm = sum(t['count'] for t in state['tokens'])

        if current_rpm < limits['rpm'] and (current_tpm + estimated_tokens) < limits['tpm']:
            current_key_index = (key_idx + 1) % num_keys
            return key, state, 0
        else:
            wait = 60000
            if current_rpm >= limits['rpm'] and len(state['requests']) > 0:
                wait = (state['requests'][0] + 60000) - now
            elif len(state['tokens']) > 0:
                wait = (state['tokens'][0]['ts'] + 60000) - now
                
            if wait < min_wait_time: 
                min_wait_time = wait

    if all_exhausted:
        return None, None, -1
        
    return None, None, max(1000.0, min_wait_time)

def execute_with_retry(model, prompt_text, expected_count):
    estimated_tokens = int(len(prompt_text) / 4) + 1024
    
    while True:
        key, state, wait_time = get_available_key(model, estimated_tokens)
        
        if wait_time == -1:
            raise Exception("ALL_KEYS_EXHAUSTED")
            
        if wait_time > 0:
            print(f"[Rate Limit Pause] Local limits reached for model {model}. Pausing for {int(wait_time/1000)}s...", flush=True)
            time.sleep(wait_time / 1000.0)
            continue
            
        client = genai.Client(api_key=key)
        now_ms = time.time() * 1000
        state['requests'].append(now_ms)
        state['tokens'].append({'ts': now_ms, 'count': estimated_tokens})
        
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchEvaluation,
                    temperature=0.1
                )
            )
            
            if response.parsed and len(response.parsed.results) == expected_count:
                state['consecutive_generic_429s'] = 0
                return response.parsed.results
            else:
                raise Exception("API_EMPTY_RESPONSE")
                
        except Exception as e:
            error_msg = str(e).lower()
            now_ms = time.time() * 1000
            
            if "404" in error_msg or "not_found" in error_msg:
                state['status'] = 'exhausted'
                print(f"[Model Unavailable] 404 error. Key permanently disabled for model {model}.", flush=True)
                continue
                
            if "429" in error_msg or "quota" in error_msg or "resource exhausted" in error_msg:
                if "perday" in error_msg or ("freetier" in error_msg and "day" in error_msg):
                    state['status'] = 'exhausted'
                    print(f"[Quota Exhausted] Daily limit reached. Key permanently disabled for model {model}.", flush=True)
                    continue
                elif "perminute" in error_msg or "rpm" in error_msg:
                    state['cooldown_until'] = now_ms + 2000
                    print(f"[Rate Limit] RPM exceeded. Cooldown for 2s on key for model {model}.", flush=True)
                    continue
                else:
                    state['consecutive_generic_429s'] += 1
                    cooldown_ms = min(60000 * state['consecutive_generic_429s'], 300000)
                    state['cooldown_until'] = now_ms + cooldown_ms
                    print(f"[Rate Limit Hit] Generic 429 on key for model {model}. Cooldown set for {cooldown_ms/1000}s.", flush=True)
                    time.sleep(5)
                    continue

            if any(code in error_msg for code in ["500", "502", "503", "504"]):
                raise Exception("API_SERVER_ERROR")

            raise e

def evaluate_batch(prompt, expected_count):
    for model in MODELS_TO_TRY:
        attempts = 0
        max_attempts = 3
        
        while attempts < max_attempts:
            try:
                results = execute_with_retry(model, prompt, expected_count)
                return results, model 
            except Exception as err:
                err_msg = str(err)
                
                if err_msg == "ALL_KEYS_EXHAUSTED":
                    print(f"[Fallback] Model {model} is out of quota on all keys. Pivoting to next model.", flush=True)
                    break 
                    
                if "API_SERVER_ERROR" in err_msg or "API_EMPTY_RESPONSE" in err_msg:
                    attempts += 1
                    if attempts < max_attempts:
                        wait_time = 2000 * (2 ** attempts)
                        print(f"[Retry] Model {model} returned transient error. Backing off {wait_time}ms...", flush=True)
                        time.sleep(wait_time / 1000.0)
                else:
                    print(f"[Fatal] Model {model} returned unrecoverable error: {err_msg}. Skipping model.", flush=True)
                    break
                    
    print("All models and keys exhausted for this batch.", flush=True)
    return None, None

def main():
    global api_keys_list
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    with open(INTERESTS_FILE, 'r', encoding='utf-8') as f:
        interests = f.read()
        
    keys_env = os.environ.get("GEMINI_API_KEY", "")
    api_keys_list = [k.strip() for k in keys_env.split(',') if k.strip()]
    
    if not api_keys_list:
        raise ValueError("No API keys found in the GEMINI_API_KEY environment variable.")
        
    archive = load_json(ARCHIVE_FILE, [])
    proxy_db = load_json(PROXY_DB_FILE, {})
    
    if SINGLE_FEED_ID not in proxy_db:
        proxy_db[SINGLE_FEED_ID] = {
            "title": "BBC News AI Filtered",
            "link": "https://www.bbc.co.uk/news",
            "description": "AI Filtered Articles combined into a single feed.",
            "articles": []
        }
            
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(hours=24)
    
    print("--- Starting Feed Fetch ---", flush=True)
    
    try:
        req = urllib.request.Request(
            FEED_URL, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
        )
        print("Sending network request to FreshRSS...", flush=True)
        with urllib.request.urlopen(req, timeout=15) as response:
            raw_rss_data = response.read()
            
        print("Data received. Parsing XML...", flush=True)
        parsed = feedparser.parse(raw_rss_data)
        
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to fetch or parse RSS feed. {e}", flush=True)
        return
        
    to_process = []
    skipped_count = 0
    
    for entry in parsed.entries:
        entry_id = entry.get('id', entry.get('link', str(time.time())))
        entry_title = entry.get('title', '').strip()
        
        # Memory Check: Skip if ID OR Title is already evaluated
        if entry_id in archive or entry_title in archive:
            skipped_count += 1
            continue
            
        pub_date = entry.get('published') or entry.get('updated')
        if pub_date:
            try:
                dt = date_parser.parse(pub_date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < time_threshold:
                    skipped_count += 1
                    continue
            except Exception:
                pass
        
        to_process.append(entry)
        
    print(f"Total articles fetched: {len(parsed.entries)}", flush=True)
    print(f"Articles skipped (old or already evaluated): {skipped_count}", flush=True)
    print(f"Articles queued for AI evaluation: {len(to_process)}", flush=True)
    
    if not to_process:
         print("No new articles to process. Exiting.", flush=True)
         return
         
    print("--- Starting AI Filtering ---", flush=True)
        
    total_batches = math.ceil(len(to_process) / BATCH_SIZE)
        
    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i:i+BATCH_SIZE]
        batch_number = (i // BATCH_SIZE) + 1
        
        prompt = f"User filtering criteria:\n{interests}\n\nEvaluate if these {len(batch)} articles align with the user's interests. An article MUST be rejected (marked false) if it matches any of the NON-INTERESTS. Return exactly {len(batch)} boolean values in the exact order of the articles provided.\n\n"
        
        for idx, art in enumerate(batch):
            prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nSummary: {art.get('summary', '')}\n\n"
            
        evaluations, used_model = evaluate_batch(prompt, len(batch))
        
        if evaluations:
            included_count = 0
            for idx, eval_result in enumerate(evaluations):
                art = batch[idx]
                art_id = art.get('id', art.get('link'))
                art_title = art.get('title', '').strip()
                
                # Append both the ID and the Title to memory to catch future duplicates
                if art_id not in archive:
                    archive.append(art_id)
                if art_title and art_title not in archive:
                    archive.append(art_title)
                
                if eval_result.is_interesting:
                    included_count += 1
                    proxy_db[SINGLE_FEED_ID]['articles'].append({
                        'id': art_id,
                        'title': art.get('title', 'No Title'),
                        'link': art.get('link', ''),
                        'description': art.get('summary', ''),
                        'published': art.get('published', art.get('updated', ''))
                    })
            print(f"[Batch {batch_number}/{total_batches}] Successfully processed via {used_model}. Selected {included_count}/{len(batch)} articles.", flush=True)
        else:
            print(f"[Batch {batch_number}/{total_batches}] FAILED to process after exhausting all models and keys.", flush=True)
            
    print("--- Sorting & Pruning Database ---", flush=True)
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
    
    print("--- Generating Final RSS File ---", flush=True)
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
        
    print(f"Run complete. Filtered RSS feed updated with {len(feed_data['articles'])} total stored articles.", flush=True)

if __name__ == "__main__":
    main()
