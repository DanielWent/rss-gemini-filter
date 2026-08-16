import sys
print("[BOOT] Starting script execution...", flush=True)

import os
import json
import time
import math
import socket
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
import feedparser
from feedgen.feed import FeedGenerator
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel

print("[BOOT] All modules imported successfully.", flush=True)

socket.setdefaulttimeout(30)

# =========================================================================
# SYSTEM CONFIGURATION
# =========================================================================
CRITERIA_DIR = "criteria"
BROAD_CRITERIA_FILE = os.path.join(CRITERIA_DIR, "bbc_interests_broad.txt")
ARCHIVE_FILE = "archive.json"
PROXY_DB_FILE = "proxy_db.json"
OUTPUT_DIR = "public"
BATCH_SIZE = 5
ARCHIVE_TTL_SECONDS = 60 * 86400  # 60 Days Rolling Retention

# Rate Limiting & Quota Management Models
STAGE1_MODELS = [
    "gemini-3.5-flash-lite", 
    "gemini-3.1-flash-lite", 
    "gemini-2.5-flash-lite"
]

STAGE2_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash", 
    "gemini-2.5-flash", 
    "gemini-1.5-flash"
]

MODEL_LIMITS = {
    "gemini-3.6-flash": {"rpm": 14, "tpm": 240000},
    "gemini-3.5-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-3.1-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-2.5-flash-lite": {"rpm": 14, "tpm": 240000},
    "gemini-3.5-flash": {"rpm": 14, "tpm": 240000},
    "gemini-2.5-flash": {"rpm": 14, "tpm": 240000},
    "gemini-1.5-flash": {"rpm": 14, "tpm": 240000}
}

# =========================================================================
# PROMPT TEMPLATES
# =========================================================================
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
# UNIFIED OUTPUT FEEDS & CONFIGURATION REGISTRY
# =========================================================================
OUTPUT_FEEDS = {
    "bbc_news_ai_filtered": {
        "title": "BBC News",
        "link": "https://www.bbc.co.uk/news",
        "description": "AI Filtered BBC News.",
        "image_url": "https://news.bbcimg.co.uk/nol/shared/img/bbc_news_120x60.gif",
        "icon_url": "https://www.bbc.co.uk/favicon.ico",
        "output_file": "BBC_News_AI_Filtered.xml"
    },
    "google_blog_ai_filtered": {
        "title": "The Keyword | Google",
        "link": "https://blog.google",
        "description": "AI Filtered Google Blog Updates.",
        "image_url": "https://blog.google/static/blogv2/images/google-logo.png",
        "icon_url": "https://blog.google/favicon.ico",
        "output_file": "Google_Blog_AI_Filtered.xml"
    },
    "google_blog_sports_filtered": {
        "title": "The Keyword | Google Sports & Health",
        "link": "https://blog.google",
        "description": "AI Filtered Google Blog Sports, Wearables & Health Metrics.",
        "image_url": "https://blog.google/static/blogv2/images/google-logo.png",
        "icon_url": "https://blog.google/favicon.ico",
        "output_file": "Google_Blog_Sports_Filtered.xml"
    },
    "dcrainmaker_ai_filtered": {
        "title": "DC Rainmaker",
        "link": "https://www.dcrainmaker.com",
        "description": "AI Filtered DC Rainmaker Sports Tech & Wearable Updates.",
        "icon_url": "https://www.dcrainmaker.com/favicon.ico",
        "output_file": "DCRainmaker_AI_Filtered.xml"
    },
    "the5krunner_ai_filtered": {
        "title": "the5krunner",
        "link": "https://the5krunner.com",
        "description": "AI Filtered The 5k Runner Sports Tech Updates.",
        "icon_url": "https://the5krunner.com/favicon.ico",
        "output_file": "The5KRunner_AI_Filtered.xml"
    },
    "grassroots_running_ai_filtered": {
        "title": "Grassroots & Niche Running",
        "link": "https://lincoln149.alwaysdata.net/freshrss/",
        "description": "AI Filtered Niche, Grassroots, Ultra, and Non-Mainstream Distance Running News.",
        "icon_url": "https://lincoln149.alwaysdata.net/favicon.ico",
        "output_file": "Grassroots_Running_AI_Filtered.xml"
    },
    "glasgow_times_ai_filtered": {
        "title": "Glasgow Times",
        "link": "https://www.glasgowtimes.co.uk",
        "description": "AI Filtered Glasgow Times Local News, Running, Education, and Science.",
        "icon_url": "https://www.glasgowtimes.co.uk/favicon.ico",
        "output_file": "Glasgow_Times_AI_Filtered.xml"
    },
    "runabc_scotland_ai_filtered": {
        "title": "runABC Scotland News",
        "link": "https://runabc.co.uk",
        "description": "AI Filtered RunABC Scotland Local Events & Regional Athletics News.",
        "icon_url": "https://runabc.co.uk/favicon.ico",
        "output_file": "RunABC_Scotland_AI_Filtered.xml"
    }
}

PIPELINES = [
    {
        "name": "Lenient Mainstream Queue",
        "archive_key": "lenient",
        "target_feed_id": "bbc_news_ai_filtered",
        "urls": ["https://feeds.bbci.co.uk/news/rss.xml"],
        "criteria_file": os.path.join(CRITERIA_DIR, "bbc_interests_lenient.txt"),
        "template": LENIENT_PROMPT_TEMPLATE,
        "requires_stage1": True,
        "lookback_days": 1,
        "stage2_models": STAGE2_MODELS
    },
    {
        "name": "Strict Private Feed Queue",
        "archive_key": "strict",
        "target_feed_id": "bbc_news_ai_filtered",
        "urls": ["https://lincoln149.alwaysdata.net/freshrss/api/query.php?user=lincoln149&t=3yPwwxjIWkQrUzb9j75NA3&f=rss"],
        "criteria_file": os.path.join(CRITERIA_DIR, "bbc_interests_strict.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": True,
        "lookback_days": 1,
        "stage2_models": STAGE2_MODELS
    },
    {
        "name": "Grassroots Running Queue",
        "archive_key": "grassroots_running",
        "target_feed_id": "grassroots_running_ai_filtered",
        "urls": ["https://lincoln149.alwaysdata.net/freshrss/api/query.php?user=lincoln149&t=6N05CNtrbYfKjurK1amToT&f=rss"],
        "criteria_file": os.path.join(CRITERIA_DIR, "therunningweek.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 1,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "Glasgow Times Queue",
        "archive_key": "glasgow_times",
        "target_feed_id": "glasgow_times_ai_filtered",
        "urls": [
            "https://www.glasgowtimes.co.uk/news/rss/",
            "https://www.glasgowtimes.co.uk/news/council/rss/",
            "https://www.glasgowtimes.co.uk/news/planning-development/rss/",
            "https://www.glasgowtimes.co.uk/news/schools-education/rss/",
            "https://www.glasgowtimes.co.uk/news/councils-politics/rss/",
            "https://www.glasgowtimes.co.uk/news/traffic-and-travel/rss/",
            "https://www.glasgowtimes.co.uk/news/glasgow-crime/rss/",
            "https://www.glasgowtimes.co.uk/your-area/rss/"
        ],
        "criteria_file": os.path.join(CRITERIA_DIR, "glasgow_times.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 1,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "RunABC Scotland Queue",
        "archive_key": "runabc_scotland",
        "target_feed_id": "runabc_scotland_ai_filtered",
        "urls": ["https://runabc.co.uk/feeds/scotland-news"],
        "criteria_file": os.path.join(CRITERIA_DIR, "runabc_scotland.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 1,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "Google Blog Primary Queue",
        "archive_key": "google",
        "target_feed_id": "google_blog_ai_filtered",
        "urls": [
            "https://blog.google/products-and-platforms/products/google-health/rss/",
            "https://blog.google/innovation-and-ai/models-and-research/gemini-models/rss/",
            "https://blog.google/innovation-and-ai/products/gemini-app/rss/",
            "https://blog.google/products-and-platforms/platforms/android/rss/",
            "https://blog.google/products-and-platforms/devices/pixel/rss/",
            "https://blog.google/products-and-platforms/devices/google-nest/rss/"
        ],
        "criteria_file": os.path.join(CRITERIA_DIR, "google_blog.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 1,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "Google Blog Sports Queue",
        "archive_key": "google_sports",
        "target_feed_id": "google_blog_sports_filtered",
        "urls": ["https://danielwent.github.io/rss-gemini-filter/Google_Blog_AI_Filtered.xml"],
        "criteria_file": os.path.join(CRITERIA_DIR, "sports_tech.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 1,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "DC Rainmaker Queue",
        "archive_key": "dcrainmaker",
        "target_feed_id": "dcrainmaker_ai_filtered",
        "urls": ["https://www.dcrainmaker.com/feed"],
        "criteria_file": os.path.join(CRITERIA_DIR, "sports_tech.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 7,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "The 5k Runner Queue",
        "archive_key": "the5krunner",
        "target_feed_id": "the5krunner_ai_filtered",
        "urls": ["https://the5krunner.com/feed/"],
        "criteria_file": os.path.join(CRITERIA_DIR, "sports_tech.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 7,
        "stage2_models": STAGE1_MODELS
    }
]

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

# =========================================================================
# UTILITIES & PERSISTENCE
# =========================================================================

key_states = {}
current_key_index = 0
api_keys_list = []

def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def load_text(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Missing required criteria file: {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

def clean_article_url(url: str) -> str:
    """Strips query-level tracking parameters (UTM, Facebook, Google click IDs) to prevent duplicate evaluations."""
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qsl(parsed.query)
    filtered = [
        (k, v) for k, v in query_params 
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ref", "mc_cid", "mc_eid"}
    ]
    clean_query = urllib.parse.urlencode(filtered)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, ''))

def load_and_migrate_archive(filepath):
    """Loads archive and automatically migrates legacy array format to timestamped dictionary."""
    raw = load_json(filepath, {})
    migrated = {}
    now_ts = time.time()
    
    if isinstance(raw, list):
        migrated["default"] = {k: now_ts for k in raw}
    elif isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, list):
                migrated[k] = {item: now_ts for item in v}
            elif isinstance(v, dict):
                migrated[k] = v
            else:
                migrated[k] = {}
    return migrated

def save_and_prune_archive(filepath, archive_data):
    """Prunes entries older than ARCHIVE_TTL_SECONDS (60 days) to keep archive.json permanently bounded."""
    cutoff_ts = time.time() - ARCHIVE_TTL_SECONDS
    pruned = {}
    for feed_key, items in archive_data.items():
        pruned[feed_key] = {
            item_id: ts for item_id, ts in items.items() if ts > cutoff_ts
        }
    save_json(filepath, pruned)

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
            
        text = main_content.get_text(separator='\n\n', strip=True)
        return text 
    except Exception as e:
        print(f"Failed to fetch full text for {url}: {e}", flush=True)
        return ""

# =========================================================================
# GEMINI API EXECUTION ENGINE
# =========================================================================

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

# =========================================================================
# MAIN EXECUTION PIPELINE
# =========================================================================

def main():
    global api_keys_list
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    broad_interests_text = load_text(BROAD_CRITERIA_FILE)
    
    keys_env = os.environ.get("GEMINI_API_KEY", "")
    api_keys_list = [k.strip() for k in keys_env.split(',') if k.strip()]
    if not api_keys_list:
        raise ValueError("No API keys found in the GEMINI_API_KEY environment variable.")
        
    archive_data = load_and_migrate_archive(ARCHIVE_FILE)
    proxy_db = load_json(PROXY_DB_FILE, {})
    
    # Initialize Proxy DB structure and refresh feed metadata
    for feed_id, meta in OUTPUT_FEEDS.items():
        if feed_id not in proxy_db:
            proxy_db[feed_id] = {**meta, "articles": []}
        else:
            for prop in ["title", "link", "description", "image_url", "icon_url"]:
                if prop in meta:
                    proxy_db[feed_id][prop] = meta[prop]

    now_utc = datetime.now(timezone.utc)
    now_ts = time.time()
    new_articles_per_feed = {feed_id: 0 for feed_id in OUTPUT_FEEDS}

    print("--- Starting Pipeline Ingestion & Filtering ---", flush=True)

    for pipeline in PIPELINES:
        archive_key = pipeline["archive_key"]
        target_feed_id = pipeline["target_feed_id"]
        if archive_key not in archive_data:
            archive_data[archive_key] = {}
        archive_set = archive_data[archive_key]

        interests_text = load_text(pipeline["criteria_file"])
        lookback_cutoff = now_utc - timedelta(days=pipeline["lookback_days"])
        
        to_process = []
        seen_titles_pipeline = set()
        
        for url in pipeline["urls"]:
            print(f"Fetching [{pipeline['name']}]: {url}", flush=True)
            try:
                req = urllib.request.Request(
                    url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    parsed = feedparser.parse(response.read())

                if hasattr(parsed, 'feed') and target_feed_id in proxy_db:
                    if 'image' in parsed.feed and isinstance(parsed.feed.image, dict) and parsed.feed.image.get('href'):
                        proxy_db[target_feed_id]['image_url'] = parsed.feed.image.href
                    elif 'icon' in parsed.feed:
                        proxy_db[target_feed_id]['icon_url'] = parsed.feed.icon

            except Exception as e:
                print(f"ERROR: Failed to fetch {url}. {e}", flush=True)
                continue

            for entry in parsed.entries:
                if not is_valid_article_item(entry):
                    continue

                raw_link = entry.get('link', '')
                clean_link = clean_article_url(raw_link)
                entry_id = str(entry.get('id', clean_link or str(time.time())))
                entry_title = entry.get('title', '').strip()

                if entry_title in seen_titles_pipeline:
                    continue
                if clean_link and clean_link in archive_set:
                    continue
                if entry_id in archive_set or entry_title in archive_set:
                    continue

                seen_titles_pipeline.add(entry_title)

                pub_date = entry.get('published') or entry.get('updated')
                if pub_date:
                    try:
                        dt = date_parser.parse(pub_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < lookback_cutoff:
                            continue
                    except Exception:
                        pass

                entry['clean_link'] = clean_link
                to_process.append(entry)

        if not to_process:
            print(f"--- No new candidate articles for {pipeline['name']} ---", flush=True)
            continue

        print(f"--- Processing {len(to_process)} articles for {pipeline['name']} ---", flush=True)

        # Stage 1 Pre-filtering (Broad Recall)
        passed_stage1 = []
        if pipeline["requires_stage1"]:
            total_s1_batches = math.ceil(len(to_process) / BATCH_SIZE)
            for i in range(0, len(to_process), BATCH_SIZE):
                batch = to_process[i:i+BATCH_SIZE]
                batch_number = (i // BATCH_SIZE) + 1

                prompt = BROAD_PROMPT_TEMPLATE.format(
                    batch_len=len(batch), 
                    broad_interests_text=broad_interests_text
                )

                for idx, art in enumerate(batch):
                    link = art.get('clean_link') or art.get('link', '')
                    if 'cached_full_text' not in art:
                        art['cached_full_text'] = fetch_full_text(link) if link else ""
                    content = art['cached_full_text'] if art['cached_full_text'] else art.get('summary', '')
                    prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nPublished: {art.get('published', 'Unknown')}\nContent: {content}\n\n"

                evaluations, used_model = evaluate_batch(prompt, len(batch), STAGE1_MODELS, BroadBatchEvaluation)
                if evaluations:
                    for idx, eval_result in enumerate(evaluations):
                        art = batch[idx]
                        art_title = art.get('title', '').strip()
                        art_id = str(art.get('id', art.get('clean_link', art.get('link'))))

                        if eval_result.matches_broad_interest:
                            passed_stage1.append(art)
                            print(f"  -> [S1 PASS] {art_title} | {eval_result.reason}", flush=True)
                        else:
                            print(f"  -> [S1 REJECT] {art_title} | {eval_result.reason}", flush=True)
                            archive_set[art_id] = now_ts
                            if art.get('clean_link'):
                                archive_set[art['clean_link']] = now_ts
                            if art_title:
                                archive_set[art_title] = now_ts
                    print(f"[Stage 1 - Batch {batch_number}/{total_s1_batches}] Complete via {used_model}.", flush=True)
                else:
                    print(f"[Stage 1 - Batch {batch_number}/{total_s1_batches}] FAILED. Retrying next run.", flush=True)
        else:
            passed_stage1 = to_process
            for art in passed_stage1:
                link = art.get('clean_link') or art.get('link', '')
                if 'cached_full_text' not in art:
                    art['cached_full_text'] = fetch_full_text(link) if link else ""

        if not passed_stage1:
            print(f"--- All candidate articles rejected in Stage 1 for {pipeline['name']} ---", flush=True)
            continue

        # Stage 2 Evaluation (Precision Matching)
        total_s2_batches = math.ceil(len(passed_stage1) / BATCH_SIZE)
        for i in range(0, len(passed_stage1), BATCH_SIZE):
            batch = passed_stage1[i:i+BATCH_SIZE]
            batch_number = (i // BATCH_SIZE) + 1

            prompt = pipeline["template"].format(
                batch_len=len(batch), 
                interests_text=interests_text
            )

            for idx, art in enumerate(batch):
                content = art.get('cached_full_text') or art.get('summary', '')
                prompt += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nPublished: {art.get('published', 'Unknown')}\nContent: {content}\n\n"

            evaluations, used_model = evaluate_batch(prompt, len(batch), pipeline["stage2_models"], BatchEvaluation)
            if evaluations:
                included_count = 0
                for idx, eval_result in enumerate(evaluations):
                    art = batch[idx]
                    art_title = art.get('title', '').strip()
                    art_id = str(art.get('id', art.get('clean_link', art.get('link'))))

                    archive_set[art_id] = now_ts
                    if art.get('clean_link'):
                        archive_set[art['clean_link']] = now_ts
                    if art_title:
                        archive_set[art_title] = now_ts

                    decision_str = "Accepted" if eval_result.is_interesting else "Rejected"
                    print(f"  -> Title: {art_title}", flush=True)
                    print(f"     Pass 1 (Reject Match):  {eval_result.triggers_exclusion} | {eval_result.exclusion_reason}", flush=True)
                    print(f"     Pass 2 (Include Match): {eval_result.primary_subject_match} | {eval_result.match_reason}", flush=True)
                    print(f"     Final Decision: {decision_str}\n", flush=True)

                    if eval_result.is_interesting:
                        included_count += 1
                        new_articles_per_feed[target_feed_id] += 1
                        
                        image_url = ""
                        if 'media_thumbnail' in art and art.media_thumbnail:
                            image_url = art.media_thumbnail[0].get('url', '')
                        elif 'media_content' in art and art.media_content:
                            image_url = art.media_content[0].get('url', '')
                        elif 'links' in art:
                            for lk in art.links:
                                if lk.get('rel') == 'enclosure' and 'image' in lk.get('type', ''):
                                    image_url = lk.get('href', '')
                                    break

                        proxy_db[target_feed_id]['articles'].append({
                            'id': art_id,
                            'title': art.get('title', 'No Title'),
                            'link': art.get('clean_link') or art.get('link', ''),
                            'description': art.get('summary', art.get('description', '')),
                            'published': art.get('published', art.get('updated', '')),
                            'image_url': image_url
                        })
                print(f"[Stage 2 - Batch {batch_number}/{total_s2_batches}] Complete via {used_model}. Accepted {included_count}/{len(batch)}.", flush=True)
            else:
                print(f"[Stage 2 - Batch {batch_number}/{total_s2_batches}] FAILED. Articles will retry next run.", flush=True)

    # =========================================================================
    # SORTING, CONDITIONAL DEDUPLICATION & PRUNING
    # =========================================================================
    print("--- Running Database Maintenance & Conditional Deduplication ---", flush=True)

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

        # Zero API calls if no new articles were accepted for this feed
        if new_articles_per_feed.get(feed_id, 0) > 0 and len(unique_articles) > 1:
            dedup_models = STAGE1_MODELS if feed_id != "bbc_news_ai_filtered" else STAGE2_MODELS
            deduped_articles = semantic_deduplication(unique_articles, dedup_models)
            feed_data['articles'] = deduped_articles
        else:
            feed_data['articles'] = unique_articles

        feed_data['articles'].sort(key=get_pub_time, reverse=True)
        feed_data['articles'] = feed_data['articles'][:100]

    save_and_prune_archive(ARCHIVE_FILE, archive_data)
    save_json(PROXY_DB_FILE, proxy_db)

    # =========================================================================
    # RSS GENERATION
    # =========================================================================
    print("--- Generating Output RSS XML Files ---", flush=True)

    for feed_id, meta in OUTPUT_FEEDS.items():
        feed_data = proxy_db.get(feed_id, {})
        output_filename = meta["output_file"]
        
        fg = FeedGenerator()
        fg.id(feed_data.get('link', meta['link']))
        fg.title(feed_data.get('title', meta['title']))
        fg.link(href=feed_data.get('link', meta['link']), rel='alternate')
        fg.description(feed_data.get('description', meta['description']))

        if feed_data.get('image_url'):
            try:
                fg.image(url=feed_data['image_url'], title=feed_data['title'], link=feed_data['link'])
                fg.logo(feed_data['image_url'])
            except Exception:
                pass
        if feed_data.get('icon_url'):
            try:
                fg.icon(feed_data['icon_url'])
            except Exception:
                pass

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

        fg.rss_file(os.path.join(OUTPUT_DIR, output_filename))
        print(f"Generated {output_filename} with {len(articles)} articles.", flush=True)

    print("Run complete.", flush=True)

if __name__ == "__main__":
    main()
