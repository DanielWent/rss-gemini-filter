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
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel

print("[BOOT] All modules imported successfully.", flush=True)

# Prevent any underlying socket from hanging infinitely
socket.setdefaulttimeout(30)

# =========================================================================
# CONFIGURATION
# =========================================================================
BROAD_INTERESTS_FILE = "interests_broad.txt"
STRICT_INTERESTS_FILE = "interests_strict.txt"
LENIENT_INTERESTS_FILE = "interests_lenient.txt"
ARCHIVE_FILE = "archive.json"
PROXY_DB_FILE = "proxy_db.json"
OUTPUT_DIR = "public"
BATCH_SIZE = 5

SINGLE_FEED_ID = "bbc_news_ai_filtered"
GOOGLE_FEED_ID = "google_blog_ai_filtered"
GOOGLE_SPORTS_FEED_ID = "google_blog_sports_filtered"
DCRAINMAKER_FEED_ID = "dcrainmaker_ai_filtered"
THE5KRUNNER_FEED_ID = "the5krunner_ai_filtered"

# =========================================================================
# PROMPTS & FILTER CRITERIA (EASY EDITING SECTION)
# =========================================================================

# Google Blog Primary Criteria
GOOGLE_BLOG_INTERESTS = """
INCLUDE CRITERIA:
1. New software releases (play store updates, android OS updates, pixel feature drops) that are specifically relevant to the Pixel 9a phone.
2. New Google Nest / Google Home product releases.
3. Google Nest / Google Home product updates.
4. Updates to the consumer Android Gemini App.
5. Updates to the consumer Gemini subscription plans.
6. Updates to the consumer "Google One" subscription plans.
7. Updates to the consumer "Google Home" subscription plans.
8. New Gemini model releases and developments.
9. Changes to consumer Gemini AI Studio API plans and rate limits.
10. Technological and product updates on consumer biosensors.
11. Google product updates that only impact UK consumers.
12. Policy updates (e.g. support lifespans) for the Pixel 9a phone.
13. New product release announcements for Pixel A series phones.
14. New product release announcmements regarding Pixel Tablets.

REJECT CRITERIA:
- ALWAYS REJECT articles that do not explicitly match at least one of the exact INCLUDE criteria above.
"""

# Sports, Fitness & Wearables Criteria
SPORTS_HEALTH_INTERESTS = """
INCLUDE CRITERIA:
1. Articles about the Forerunner series of sports watches.
2. Articles containing updates on Garmin as a company.
3. Articles containing updates on Strava as a company.
4. Articles about the Garmin Elevate Sensor family.
5. Articles about biosensor technology in general.
6. Articles about running data metrics in general.
7. Articles about health data metrics in general.
8. Articles about updates to the Garmin Connect platform.
9. Articles about updates to the Strava app or website.
10. Articles about updates to the Garmin app or website.
11. Articles contaning updates on Garmin subscriptions.
12. Articles containing updates on Strava subscriptions.
13. Articles about novel hardware and software releases and updates
14. Articles on non wrist-based Garmin hardware 
15. Articles containing rumours or theories about new GPS chips, HR monitors, biosensors or data metrics.
16. Articles covering developments in GPS technology, biosensors, health data metrics or running data metrics.

REJECT CRITERIA:
- ALWAYS REJECT articles that do not explicitly match at least one of the exact INCLUDE criteria above.
- ALWAYS REJECT articles that are primarily about Apple software or hardware
"""

# Base AI Prompt Templates
BROAD_PROMPT_TEMPLATE = """You are a first-pass content filter. Review the following articles against the user's broad interests.

USER'S BROAD INTERESTS:
{broad_interests_text}

INSTRUCTIONS FOR STAGE 1 EVALUATION:
For each article, determine if it has ANY potential relevance to the broad interests. If the article is even tangentially related, or if you are unsure, default to true (matches_broad_interest) to allow it through to the next stage.
Output a boolean (matches_broad_interest) and a brief justification (reason).

Return exactly {batch_len} evaluations in the exact order of the articles provided.

"""

STRICT_PROMPT_TEMPLATE = """You are an expert content curator. Review the following articles against the user's criteria.

USER CRITERIA:
{interests_text}

INSTRUCTIONS FOR TWO-PASS EVALUATION (STRICT MODE):
For each article, you must perform a 2-pass check IN THIS EXACT ORDER.
Pass 1 (triggers_exclusion): Be strictly objective. Does the article trigger ANY of the "REJECT" rules (either within a specific category OR within the ALWAYS REJECT / MACRO-EXCLUSIONS list)? Output a boolean (true if a reject rule is triggered). In your reasoning (exclusion_reason), explicitly quote the exact REJECT rule matched. If no REJECT criteria were matched, state "None".
Pass 2 (primary_subject_match): Does the CORE SUBJECT of the article satisfy at least ONE of the explicit "INCLUDE" criteria? Output a boolean. In your reasoning (match_reason), explicitly quote the exact INCLUDE category and rule matched. If no INCLUDE criteria were matched, state "None".
Final Decision (is_interesting): This MUST be true ONLY IF (triggers_exclusion is false) AND (primary_subject_match is true).

Return exactly {batch_len} evaluations in the exact order of the articles provided.

"""

LENIENT_PROMPT_TEMPLATE = """You are an expert content curator. Review the following articles from a trusted main news feed against the user's criteria.

USER CRITERIA:
{interests_text}

INSTRUCTIONS FOR TWO-PASS EVALUATION (LENIENT MODE):
Mainstream news outlets often report on scientific, environmental, technological, and lifestyle domains through everyday lenses (such as consumer demand, public event preparation, safety logistics, or retail trends). Look at the underlying event.

For each article, you must perform a 2-pass check IN THIS EXACT ORDER.
Pass 1 (triggers_exclusion): Be strictly objective. Does the article trigger ANY of the "REJECT" rules (either within a specific category OR within the ALWAYS REJECT / MACRO-EXCLUSIONS list)? Output a boolean (true if a reject rule is triggered). In your reasoning (exclusion_reason), explicitly quote the exact REJECT rule matched. If no REJECT criteria were matched, state "None".
Pass 2 (primary_subject_match): Does the underlying event or core subject satisfy at least ONE of the explicit "INCLUDE" criteria? Output a boolean. In your reasoning (match_reason), explicitly quote the exact INCLUDE category and rule matched. If no INCLUDE criteria were matched, state "None".
Final Decision (is_interesting): This MUST be true ONLY IF (triggers_exclusion is false) AND (primary_subject_match is true).

Return exactly {batch_len} evaluations in the exact order of the articles provided.

"""

# =========================================================================
# SYSTEM CONFIGURATION & DEFINITIONS
# =========================================================================

# Feed Definitions
FEEDS = [
    {
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
        "mode": "lenient"
    },
    {
        "url": "https://lincoln149.alwaysdata.net/freshrss/api/query.php?user=lincoln149&t=3yPwwxjIWkQrUzb9j75NA3&f=rss",
        "mode": "strict"
    },
    {
        "url": "https://blog.google/products-and-platforms/products/google-health/rss/",
        "mode": "google"
    },
    {
        "url": "https://blog.google/innovation-and-ai/models-and-research/gemini-models/rss/",
        "mode": "google"
    },
    {
        "url": "https://blog.google/innovation-and-ai/products/gemini-app/rss/",
        "mode": "google"
    },
    {
        "url": "https://blog.google/products-and-platforms/platforms/android/rss/",
        "mode": "google"
    },
    {
        "url": "https://blog.google/products-and-platforms/devices/pixel/rss/",
        "mode": "google"
    },
    {
        "url": "https://blog.google/products-and-platforms/devices/google-nest/rss/",
        "mode": "google"
    },
    {
        "url": "https://danielwent.github.io/rss-gemini-filter/Google_Blog_AI_Filtered.xml",
        "mode": "google_sports"
    },
    {
        "url": "https://www.dcrainmaker.com/feed",
        "mode": "dcrainmaker"
    },
    {
        "url": "https://the5krunner.com/feed/",
        "mode": "the5krunner"
    }
]

# Rate Limiting & Quota Management State
STAGE1_MODELS = [
    "gemini-3.5-flash-lite", 
    "gemini-3.1-flash-lite", 
    "gemini-2.5-flash-lite"
]

STAGE2_MODELS = [
    "gemini-3.5-flash", 
    "gemini-2.5-flash", 
    "gemini-1.5-flash"
]

MODEL_LIMITS = {
    "gemini-3.5-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-3.1-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-2.5-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-3.5-flash": {"rpm": 14, "tpm": 240000},
    "gemini-2.5-flash": {"rpm": 14, "tpm": 240000},
    "gemini-1.5-flash": {"rpm": 14, "tpm": 240000}
}

key_states = {}
current_key_index = 0
api_keys_list = []

# --- SCHEMA DEFINITIONS ---

class BroadArticleEvaluation(BaseModel):
    matches_broad_interest: bool
    reason: str

class BroadBatchEvaluation(BaseModel):
    results: list[BroadArticleEvaluation]

class ArticleEvaluation(BaseModel):
    triggers_exclusion: bool
    exclusion_reason: str
    primary_subject_match: bool
    match_reason: str
    is_interesting: bool

class BatchEvaluation(BaseModel):
    results: list[ArticleEvaluation]
    
class DeduplicationResult(BaseModel):
    unique_ids: list[str]

def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def is_valid_article_item(entry):
    link = entry.get('link', '').lower()
    title = entry.get('title', '').lower().strip()
    
    if any(media in link for media in ['/sounds/play/', '/videos/', '/iplayer/']):
        return False
    if title.startswith(('watch:', 'video:', 'podcast:', 'audio:')):
        return False
        
    return True

def fetch_full_text(url):
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read()
            
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside', 'form', 'figure', 'picture', 'svg']):
            tag.decompose()
            
        main_content = soup.find('article') or soup.find('main') or soup.find('body')
        if not main_content:
            return ""
            
        text = main_content.get_text(separator=' ', strip=True)
        return text[:3000] 
    except Exception as e:
        print(f"Failed to fetch full text for {url}: {e}", flush=True)
        return ""

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

def execute_with_retry(model, prompt_text, schema_class):
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
                    response_schema=schema_class,
                    temperature=0.1
                )
            )
            
            if response.parsed:
                state['consecutive_generic_429s'] = 0
                return response.parsed
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

def evaluate_batch(prompt, expected_count, models_to_try, schema_class):
    for model in models_to_try:
        attempts = 0
        max_attempts = 3
        
        while attempts < max_attempts:
            try:
                results = execute_with_retry(model, prompt, schema_class)
                if len(results.results) == expected_count:
                    return results.results, model 
                else:
                    raise Exception(f"API returned {len(results.results)} results, expected {expected_count}")
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

def semantic_deduplication(articles, eval_models):
    if len(articles) <= 1:
        return articles
        
    print(f"--- Running Semantic Deduplication on {len(articles)} articles ---", flush=True)
    
    prompt = "You are a strict editor. Review the following news articles. Group articles that cover the EXACT SAME news event or story. If multiple articles cover the same event (even if they have different headlines), select ONLY ONE ID to keep. Return a JSON list containing only the unique IDs.\n\n"
    
    for art in articles:
        prompt += f"[ID: {art['id']}] Title: {art['title']}\nSnippet: {art['description'][:500]}\n\n"
        
    for model in eval_models:
        try:
            result = execute_with_retry(model, prompt, DeduplicationResult)
            if result and hasattr(result, 'unique_ids'):
                unique_ids = set(result.unique_ids)
                deduped = [a for a in articles if str(a['id']) in unique_ids]
                print(f"[Deduplication] Reduced from {len(articles)} to {len(deduped)} articles using {model}.", flush=True)
                return deduped if deduped else articles
        except Exception as e:
            print(f"[Deduplication] Model {model} failed: {e}. Trying next...", flush=True)
            
    print("[Deduplication] All models failed. Returning original list.", flush=True)
    return articles

def main():
    global api_keys_list
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    try:
        with open(STRICT_INTERESTS_FILE, 'r', encoding='utf-8') as f:
            strict_interests = f.read()
        with open(LENIENT_INTERESTS_FILE, 'r', encoding='utf-8') as f:
            lenient_interests = f.read()
        with open(BROAD_INTERESTS_FILE, 'r', encoding='utf-8') as f:
            broad_interests = f.read()
    except FileNotFoundError as e:
        print(f"CRITICAL ERROR: Could not find required interests file. {e}")
        return
        
    keys_env = os.environ.get("GEMINI_API_KEY", "")
    api_keys_list = [k.strip() for k in keys_env.split(',') if k.strip()]
    
    if not api_keys_list:
        raise ValueError("No API keys found in the GEMINI_API_KEY environment variable.")
        
    raw_archive = load_json(ARCHIVE_FILE, {"strict": [], "lenient": [], "google": [], "google_sports": [], "dcrainmaker": [], "the5krunner": []})
    if isinstance(raw_archive, list):
        archive_data = {"strict": raw_archive, "lenient": [], "google": [], "google_sports": [], "dcrainmaker": [], "the5krunner": []}
    else:
        archive_data = raw_archive
        for key in ["google", "google_sports", "dcrainmaker", "the5krunner"]:
            if key not in archive_data:
                archive_data[key] = []

    proxy_db = load_json(PROXY_DB_FILE, {})
    
    # Initialize DB structural objects
    default_feeds = {
        SINGLE_FEED_ID: {"title": "BBC News AI Filtered", "link": "https://www.bbc.co.uk/news", "description": "AI Filtered Articles combined into a single feed."},
        GOOGLE_FEED_ID: {"title": "Google Blog AI Filtered", "link": "https://blog.google", "description": "AI Filtered Google Blog Updates."},
        GOOGLE_SPORTS_FEED_ID: {"title": "Google Blog Sports & Health AI Filtered", "link": "https://danielwent.github.io/rss-gemini-filter/Google_Blog_AI_Filtered.xml", "description": "AI Filtered Google Blog Sports, Wearables & Health Metrics."},
        DCRAINMAKER_FEED_ID: {"title": "DC Rainmaker AI Filtered", "link": "https://www.dcrainmaker.com", "description": "AI Filtered DC Rainmaker Sports Tech & Wearable Updates."},
        THE5KRUNNER_FEED_ID: {"title": "The 5k Runner AI Filtered", "link": "https://the5krunner.com", "description": "AI Filtered The 5k Runner Sports Tech Updates."}
    }
    
    for feed_key, meta in default_feeds.items():
        if feed_key not in proxy_db:
            proxy_db[feed_key] = {**meta, "articles": []}

    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(hours=24)
    
    to_process_strict = []
    to_process_lenient = []
    to_process_google = []
    to_process_google_sports = []
    to_process_dcrainmaker = []
    to_process_the5krunner = []
    skipped_count = 0
    seen_titles_this_run = set()

    print("--- Starting Feed Fetch ---", flush=True)
    
    for feed in FEEDS:
        mode = feed['mode']
        print(f"Fetching {mode.upper()} feed: {feed['url']}", flush=True)
        try:
            req = urllib.request.Request(
                feed['url'], 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                raw_rss_data = response.read()
                
            parsed = feedparser.parse(raw_rss_data)
            
        except Exception as e:
            print(f"ERROR: Failed to fetch or parse RSS feed {feed['url']}. {e}", flush=True)
            continue
            
        for entry in parsed.entries:
            if not is_valid_article_item(entry):
                skipped_count += 1
                continue
                
            entry_id = str(entry.get('id', entry.get('link', str(time.time()))))
            entry_title = entry.get('title', '').strip()
            
            if entry_title in seen_titles_this_run:
                skipped_count += 1
                continue
            
            if mode in archive_data and (entry_id in archive_data[mode] or entry_title in archive_data[mode]):
                skipped_count += 1
                continue
                
            seen_titles_this_run.add(entry_title)
                
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
            
            if mode == 'strict':
                to_process_strict.append(entry)
            elif mode == 'lenient':
                to_process_lenient.append(entry)
            elif mode == 'google':
                to_process_google.append(entry)
            elif mode == 'google_sports':
                to_process_google_sports.append(entry)
            elif mode == 'dcrainmaker':
                to_process_dcrainmaker.append(entry)
            elif mode == 'the5krunner':
                to_process_the5krunner.append(entry)
                
    print(f"Articles skipped (old, evaluated, or media links): {skipped_count}", flush=True)
    
    queues_to_process = [
        {
            "name": "Lenient Queue", 
            "data": to_process_lenient, 
            "template": LENIENT_PROMPT_TEMPLATE,
            "interests_text": lenient_interests,
            "archive_key": "lenient",
            "feed_id": SINGLE_FEED_ID,
            "requires_stage1": True,
            "stage2_models": STAGE2_MODELS
        },
        {
            "name": "Strict Queue", 
            "data": to_process_strict, 
            "template": STRICT_PROMPT_TEMPLATE,
            "interests_text": strict_interests,
            "archive_key": "strict",
            "feed_id": SINGLE_FEED_ID,
            "requires_stage1": True,
            "stage2_models": STAGE2_MODELS
        },
        {
            "name": "Google Blog Queue",
            "data": to_process_google,
            "template": STRICT_PROMPT_TEMPLATE,
            "interests_text": GOOGLE_BLOG_INTERESTS,
            "archive_key": "google",
            "feed_id": GOOGLE_FEED_ID,
            "requires_stage1": False,
            "stage2_models": STAGE1_MODELS
        },
        {
            "name": "Google Blog Sports Queue",
            "data": to_process_google_sports,
            "template": STRICT_PROMPT_TEMPLATE,
            "interests_text": SPORTS_HEALTH_INTERESTS,
            "archive_key": "google_sports",
            "feed_id": GOOGLE_SPORTS_FEED_ID,
            "requires_stage1": False,
            "stage2_models": STAGE1_MODELS
        },
        {
            "name": "DC Rainmaker Queue",
            "data": to_process_dcrainmaker,
            "template": STRICT_PROMPT_TEMPLATE,
            "interests_text": SPORTS_HEALTH_INTERESTS,
            "archive_key": "dcrainmaker",
            "feed_id": DCRAINMAKER_FEED_ID,
            "requires_stage1": False,
            "stage2_models": STAGE1_MODELS
        },
        {
            "name": "The 5k Runner Queue",
            "data": to_process_the5krunner,
            "template": STRICT_PROMPT_TEMPLATE,
            "interests_text": SPORTS_HEALTH_INTERESTS,
            "archive_key": "the5krunner",
            "feed_id": THE5KRUNNER_FEED_ID,
            "requires_stage1": False,
            "stage2_models": STAGE1_MODELS
        }
    ]

    for queue in queues_to_process:
        to_process = queue["data"]
        archive_key = queue["archive_key"]
        feed_id = queue["feed_id"]
        stage2_models = queue["stage2_models"]
        
        if not to_process:
            print(f"--- No new articles for {queue['name']} ---", flush=True)
            continue
            
        passed_stage1 = []
        
        if queue.get("requires_stage1", True):
            print(f"--- STAGE 1: Pre-filtering {queue['name']} ({len(to_process)} articles) ---", flush=True)
            total_s1_batches = math.ceil(len(to_process) / BATCH_SIZE)
            
            for i in range(0, len(to_process), BATCH_SIZE):
                batch = to_process[i:i+BATCH_SIZE]
                batch_number = (i // BATCH_SIZE) + 1
                
                prompt = BROAD_PROMPT_TEMPLATE.format(
                    batch_len=len(batch), 
                    broad_interests_text=broad_interests
                )
                
                for idx, art in enumerate(batch):
                    link = art.get('link', '')
                    if 'cached_full_text' not in art:
                        full_text = fetch_full_text(link) if link else ""
                        art['cached_full_text'] = full_text
                    else:
                        full_text = art['cached_full_text']
                        
                    content = full_text if full_text else art.get('summary', '')
                    prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nPublished: {art.get('published', 'Unknown')}\nContent: {content}\n\n"
                    
                evaluations, used_model = evaluate_batch(prompt, len(batch), STAGE1_MODELS, BroadBatchEvaluation)
                
                if evaluations:
                    for idx, eval_result in enumerate(evaluations):
                        art = batch[idx]
                        art_title = art.get('title', '').strip()
                        
                        if eval_result.matches_broad_interest:
                            passed_stage1.append(art)
                            print(f"  -> [S1 PASS] Title: {art_title} | Reason: {eval_result.reason}", flush=True)
                        else:
                            print(f"  -> [S1 REJECT] Title: {art_title} | Reason: {eval_result.reason}", flush=True)
                            art_id = str(art.get('id', art.get('link')))
                            if art_id not in archive_data[archive_key]:
                                archive_data[archive_key].append(art_id)
                            if art_title and art_title not in archive_data[archive_key]:
                                archive_data[archive_key].append(art_title)
                    print(f"[Stage 1 - Batch {batch_number}/{total_s1_batches}] Processed via {used_model}.", flush=True)
                else:
                    print(f"[Stage 1 - Batch {batch_number}/{total_s1_batches}] FAILED. Articles will be skipped and retried next run.", flush=True)
        else:
            # Bypass Stage 1
            passed_stage1 = to_process
            for art in passed_stage1:
                link = art.get('link', '')
                if 'cached_full_text' not in art:
                    full_text = fetch_full_text(link) if link else ""
                    art['cached_full_text'] = full_text

        if not passed_stage1:
            print(f"--- All articles filtered out in Stage 1 for {queue['name']} ---", flush=True)
            continue
            
        print(f"--- STAGE 2: Evaluating {queue['name']} ({len(passed_stage1)} articles) ---", flush=True)
        total_s2_batches = math.ceil(len(passed_stage1) / BATCH_SIZE)
        
        for i in range(0, len(passed_stage1), BATCH_SIZE):
            batch = passed_stage1[i:i+BATCH_SIZE]
            batch_number = (i // BATCH_SIZE) + 1
            
            prompt = queue["template"].format(
                batch_len=len(batch), 
                interests_text=queue["interests_text"]
            )
            
            for idx, art in enumerate(batch):
                full_text = art.get('cached_full_text', '')
                content = full_text if full_text else art.get('summary', '')
                prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nPublished: {art.get('published', 'Unknown')}\nContent: {content}\n\n"
                
            evaluations, used_model = evaluate_batch(prompt, len(batch), stage2_models, BatchEvaluation)
            
            if evaluations:
                included_count = 0
                for idx, eval_result in enumerate(evaluations):
                    art = batch[idx]
                    art_id = str(art.get('id', art.get('link')))
                    art_title = art.get('title', '').strip()
                    
                    if art_id not in archive_data[archive_key]:
                        archive_data[archive_key].append(art_id)
                    if art_title and art_title not in archive_data[archive_key]:
                        archive_data[archive_key].append(art_title)

                    decision_str = "Accepted" if eval_result.is_interesting else "Rejected"

                    print(f"  -> Title: {art_title}", flush=True)
                    print(f"     Pass 1 (Reject Match):  {eval_result.triggers_exclusion} | Matched Rule: {eval_result.exclusion_reason}", flush=True)
                    print(f"     Pass 2 (Include Match): {eval_result.primary_subject_match} | Matched Rule: {eval_result.match_reason}", flush=True)
                    print(f"     Final Decision: {decision_str}\n", flush=True)
                    
                    if eval_result.is_interesting:
                        included_count += 1
                        
                        image_url = ""
                        if 'media_thumbnail' in art and art.media_thumbnail:
                            image_url = art.media_thumbnail[0].get('url', '')
                        elif 'media_content' in art and art.media_content:
                            image_url = art.media_content[0].get('url', '')
                        elif 'links' in art:
                            for link in art.links:
                                if link.get('rel') == 'enclosure' and 'image' in link.get('type', ''):
                                    image_url = link.get('href', '')
                                    break
                        
                        proxy_db[feed_id]['articles'].append({
                            'id': art_id,
                            'title': art.get('title', 'No Title'),
                            'link': art.get('link', ''),
                            'description': art.get('summary', art.get('description', '')),
                            'published': art.get('published', art.get('updated', '')),
                            'image_url': image_url
                        })
                print(f"[Stage 2 - Batch {batch_number}/{total_s2_batches}] Successfully processed via {used_model}. Selected {included_count}/{len(batch)} articles.", flush=True)
            else:
                print(f"[Stage 2 - Batch {batch_number}/{total_s2_batches}] FAILED to process after exhausting all models and keys.", flush=True)
            
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

    for feed_id, feed_data in proxy_db.items():
        unique_articles = []
        seen_titles = set()
        for art in feed_data['articles']:
            title = art.get('title', '').strip()
            if title not in seen_titles:
                seen_titles.add(title)
                unique_articles.append(art)
                
        dedup_models = STAGE1_MODELS if feed_id in [GOOGLE_FEED_ID, GOOGLE_SPORTS_FEED_ID, DCRAINMAKER_FEED_ID, THE5KRUNNER_FEED_ID] else STAGE2_MODELS
        deduped_articles = semantic_deduplication(unique_articles, dedup_models)
        
        feed_data['articles'] = deduped_articles
        feed_data['articles'].sort(key=get_pub_time, reverse=True)
        feed_data['articles'] = feed_data['articles'][:100]

    save_json(ARCHIVE_FILE, archive_data)
    save_json(PROXY_DB_FILE, proxy_db)
    
    print("--- Generating Final RSS Files ---", flush=True)
    
    file_mappings = {
        SINGLE_FEED_ID: "BBC_News_AI_Filtered.xml",
        GOOGLE_FEED_ID: "Google_Blog_AI_Filtered.xml",
        GOOGLE_SPORTS_FEED_ID: "Google_Blog_Sports_Filtered.xml",
        DCRAINMAKER_FEED_ID: "DCRainmaker_AI_Filtered.xml",
        THE5KRUNNER_FEED_ID: "The5KRunner_AI_Filtered.xml"
    }

    for feed_id, output_filename in file_mappings.items():
        feed_data = proxy_db.get(feed_id, {})
        # Modified so that it ALWAYS creates an RSS file, even if 0 articles
        if feed_data:
            fg = FeedGenerator()
            fg.id(feed_data['link'])
            fg.title(feed_data['title']) 
            fg.link(href=feed_data['link'], rel='alternate')
            fg.description(feed_data['description']) 
            
            articles = feed_data.get('articles', [])
            for art in articles:
                fe = fg.add_entry()
                fe.id(art['id'])
                fe.title(art['title'])
                fe.link(href=art['link'])
                fe.description(art['description'])
                
                if art.get('image_url'):
                    fe.enclosure(url=art['image_url'], length='0', type='image/jpeg')
                
                if art.get('published'):
                    try:
                        dt = date_parser.parse(art['published'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        fe.pubDate(dt)
                    except Exception:
                        pass
            
            # This generates the file and averts the 404 error
            fg.rss_file(f"{OUTPUT_DIR}/{output_filename}")
            print(f"Generated {output_filename} with {len(articles)} articles.", flush=True)
        
    print("Run complete.", flush=True)

if __name__ == "__main__":
    main()
