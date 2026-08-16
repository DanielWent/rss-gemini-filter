import sys
print("[BOOT] Starting script execution...", flush=True)

import os
import json
import time
import math
import socket
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

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
KEY_USAGE_FILE = "key_usage.json"
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

CATEGORY_SCORING_PROMPT_TEMPLATE = """You are an objective news scoring engine evaluating articles for a Glasgow & Bearsden weekly digest.
Evaluate each article against the provided editorial rubric for CATEGORY {category_label}.

EDITORIAL RUBRIC:
{rubric_text}

INSTRUCTIONS:
Evaluate each article independently. Assign a numeric score between 0.0 and 10.0 based strictly on the rubric.
Return evaluations for all {batch_len} articles in exact order.

ARTICLES:
{articles_payload}
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
    },
    "glasgow_newsletter_candidates": {
        "title": "Glasgow Newsletter Candidates",
        "link": "https://lincoln149.alwaysdata.net/freshrss/",
        "description": "Candidate articles scoring > 5.0 in Cat A/D or > 3.0 in Cat B/C for the Glasgow weekly digest.",
        "icon_url": "https://lincoln149.alwaysdata.net/favicon.ico",
        "output_file": "Glasgow_Newsletter_Candidates.xml"
    }
}

PIPELINES = [
    {
        "name": "Lenient Mainstream Queue",
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
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
        "type": "two_pass",
        "archive_key": "the5krunner",
        "target_feed_id": "the5krunner_ai_filtered",
        "urls": ["https://the5krunner.com/feed/"],
        "criteria_file": os.path.join(CRITERIA_DIR, "sports_tech.txt"),
        "template": STRICT_PROMPT_TEMPLATE,
        "requires_stage1": False,
        "lookback_days": 7,
        "stage2_models": STAGE1_MODELS
    },
    {
        "name": "Glasgow Newsletter Candidates Queue",
        "type": "scoring",
        "archive_key": "newsletter_candidates",
        "target_feed_id": "glasgow_newsletter_candidates",
        "urls": [
            "https://www.glasgowlive.co.uk/?service=rss",
            "https://www.glasgowtimes.co.uk/news/rss/",
            "https://www.thescottishsun.co.uk/where/glasgow/feed/",
            "https://news.stv.tv/section/west-central/feed",
            "https://feeds.bbci.co.uk/news/scotland/glasgow_and_west/rss.xml",
            "https://www.glasgowtimes.co.uk/entertainment/rss/",
            "https://www.heraldscotland.com/news/homenews/rss/",
            "https://glasgow-live-rss-proxy.daniel-went.workers.dev/",
            "https://runabc.co.uk/feeds/scotland-news"
        ],
        "category_files": {
            "A": os.path.join(CRITERIA_DIR, "newsletter_cat_a.txt"),
            "B": os.path.join(CRITERIA_DIR, "newsletter_cat_b.txt"),
            "C": os.path.join(CRITERIA_DIR, "newsletter_cat_c.txt"),
            "D": os.path.join(CRITERIA_DIR, "newsletter_cat_d.txt")
        },
        "category_thresholds": {
            "A": 5.0,
            "B": 3.0,
            "C": 3.0,
            "D": 5.0
        },
        "batch_size": 3,
        "lookback_days": 1,
        "models": STAGE1_MODELS
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

class ArticleScore(BaseModel):
    score: float
    rationale: str

class CategoryBatchScore(BaseModel):
    results: list[ArticleScore]
    
class DeduplicationResult(BaseModel):
    unique_ids: list[str]

# =========================================================================
# UTILITIES & PERSISTENCE
# =========================================================================

key_states = {}
api_keys_list = []
persistent_key_usage = {}

def get_key_fingerprint(key: str) -> str:
    """Returns a deterministic 16-character SHA-256 hash to prevent raw secrets from persisting to git."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:16]

def get_pacific_date() -> str:
    """Returns the current date formatted as YYYY-MM-DD in America/Los_Angeles."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(timezone(timedelta(hours=-7))).strftime("%Y-%m-%d")

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
    cutoff_ts = time.time() - ARCHIVE_TTL_SECONDS
    pruned = {}
    for feed_key, items in archive_data.items():
        pruned[feed_key] = {
            item_id: ts for item_id, ts in items.items() if ts > cutoff_ts
        }
    save_json(filepath, pruned)

def init_key_usage_tracker():
    """Loads historical key usage from disk and resets records if a new Pacific day has started."""
    global persistent_key_usage
    today_pt = get_pacific_date()
    raw = load_json(KEY_USAGE_FILE, {"date": today_pt, "keys": {}})
    
    if raw.get("date") != today_pt:
        print(f"[Quota Tracker] New Pacific day detected ({today_pt}). Resetting daily RPD counters.", flush=True)
        persistent_key_usage = {"date": today_pt, "keys": {}}
        save_json(KEY_USAGE_FILE, persistent_key_usage)
    else:
        persistent_key_usage = raw

def record_key_success(key: str, model: str):
    """Increments the persistent daily counter specifically for this key and model pair using hashed fingerprint."""
    global persistent_key_usage
    today_pt = get_pacific_date()
    if persistent_key_usage.get("date") != today_pt:
        persistent_key_usage = {"date": today_pt, "keys": {}}

    key_fp = get_key_fingerprint(key)
    k_data = persistent_key_usage["keys"].setdefault(key_fp, {})
    m_data = k_data.setdefault(model, {"daily_requests": 0, "exhausted": False})
    m_data["daily_requests"] += 1
    save_json(KEY_USAGE_FILE, persistent_key_usage)

def mark_key_daily_exhausted(key: str, model: str):
    """Marks key as exhausted ONLY for this specific model on the current Pacific day using hashed fingerprint."""
    global persistent_key_usage
    today_pt = get_pacific_date()
    if persistent_key_usage.get("date") != today_pt:
        persistent_key_usage = {"date": today_pt, "keys": {}}

    key_fp = get_key_fingerprint(key)
    k_data = persistent_key_usage["keys"].setdefault(key_fp, {})
    m_data = k_data.setdefault(model, {"daily_requests": 0, "exhausted": False})
    m_data["exhausted"] = True
    save_json(KEY_USAGE_FILE, persistent_key_usage)

def is_valid_article_item(entry):
    link = entry.get('link', '').lower()
    title = entry.get('title', '').lower().strip()
    
    if any(media in link for media in ['/sounds/play/', '/videos/', '/iplayer/']):
        return False
    if title.startswith(('watch:', 'video:', 'podcast:', 'audio:')):
        return False
        
    return True

def extract_image_from_entry(entry):
    """Extracts thumbnail or featured image from standard RSS/Atom tags or embedded HTML."""
    if 'media_thumbnail' in entry and entry.media_thumbnail:
        for thumb in entry.media_thumbnail:
            if isinstance(thumb, dict) and thumb.get('url'):
                return thumb.get('url')

    if 'media_content' in entry and entry.media_content:
        for media in entry.media_content:
            if isinstance(media, dict) and media.get('url'):
                if media.get('medium') == 'image' or 'image' in media.get('type', ''):
                    return media.get('url')
                if any(ext in media.get('url', '').lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                    return media.get('url')
        if isinstance(entry.media_content[0], dict) and entry.media_content[0].get('url'):
            return entry.media_content[0].get('url')

    if 'enclosures' in entry and entry.enclosures:
        for enc in entry.enclosures:
            if isinstance(enc, dict):
                url = enc.get('href') or enc.get('url', '')
                enc_type = enc.get('type', '').lower()
                if 'image' in enc_type or any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                    return url

    if 'links' in entry and entry.links:
        for lk in entry.links:
            if isinstance(lk, dict):
                url = lk.get('href', '')
                lk_type = lk.get('type', '').lower()
                rel = lk.get('rel', '').lower()
                if (rel == 'enclosure' and 'image' in lk_type) or ('image' in lk_type):
                    return url
                if rel == 'enclosure' and any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                    return url

    if 'image' in entry:
        img = entry.get('image')
        if isinstance(img, dict) and img.get('href'):
            return img.get('href')
        elif isinstance(img, str) and img.startswith('http'):
            return img

    for field in ['summary', 'description', 'content']:
        raw_val = entry.get(field)
        if raw_val:
            if isinstance(raw_val, list):
                raw_html = "".join([c.get('value', '') if isinstance(c, dict) else str(c) for c in raw_val])
            elif isinstance(raw_val, dict):
                raw_html = raw_val.get('value', '')
            else:
                raw_html = str(raw_val)

            if '<img' in raw_html.lower():
                try:
                    soup = BeautifulSoup(raw_html, 'html.parser')
                    img_tag = soup.find('img')
                    if img_tag:
                        src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-original')
                        if src and src.startswith('http'):
                            return src
                except Exception:
                    pass

    return ""

def fetch_article_details(url):
    """Fetches clean article text and extracts OpenGraph/Twitter thumbnail from the webpage."""
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read()
            
        soup = BeautifulSoup(html, 'html.parser')

        # Extract OpenGraph / Twitter metadata images
        page_image = ""
        og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'og:image'})
        if og_img and og_img.get('content'):
            page_image = og_img.get('content').strip()

        if not page_image:
            tw_img = soup.find('meta', attrs={'name': 'twitter:image'}) or soup.find('meta', property='twitter:image')
            if tw_img and tw_img.get('content'):
                page_image = tw_img.get('content').strip()

        if not page_image:
            link_img = soup.find('link', rel='image_src')
            if link_img and link_img.get('href'):
                page_image = link_img.get('href').strip()

        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside', 'form', 'figure', 'picture', 'svg']):
            tag.decompose()
            
        main_content = soup.find('article') or soup.find('main') or soup.find('body')
        text = ""
        if main_content:
            text = main_content.get_text(separator='\n\n', strip=True)

        return text, page_image
    except Exception as e:
        print(f"Failed to fetch article details for {url}: {e}", flush=True)
        return "", ""

# =========================================================================
# GEMINI API EXECUTION ENGINE (PER-MODEL INDEPENDENT POOL)
# =========================================================================

def get_available_key(model, estimated_tokens):
    """Picks the least-used non-exhausted key for this specific model using hashed fingerprints."""
    global key_states, api_keys_list, persistent_key_usage
    limits = MODEL_LIMITS.get(model, {"rpm": 14, "tpm": 240000})
    now = time.time() * 1000 
    minute_ago = now - 60000

    if not api_keys_list:
        return None, None, -1

    for key in api_keys_list:
        state_id = f"{model}_{key}"
        if state_id not in key_states:
            key_fp = get_key_fingerprint(key)
            persisted_meta = persistent_key_usage.get("keys", {}).get(key_fp, {}).get(model, {})
            is_exhausted = persisted_meta.get("exhausted", False)
            daily_reqs = persisted_meta.get("daily_requests", 0)
            
            key_states[state_id] = {
                'requests': [], 
                'tokens': [], 
                'status': 'exhausted' if is_exhausted else 'active', 
                'cooldown_until': 0, 
                'consecutive_generic_429s': 0,
                'daily_requests': daily_reqs
            }

    sorted_keys = sorted(
        api_keys_list,
        key=lambda k: (
            key_states[f"{model}_{k}"]['status'] == 'exhausted',
            key_states[f"{model}_{k}"]['daily_requests']
        )
    )

    min_wait_time = float('inf')
    all_exhausted = True

    for key in sorted_keys:
        state_id = f"{model}_{key}"
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
                state['daily_requests'] += 1
                record_key_success(key, model)
                return response.parsed
            else:
                raise Exception("API_EMPTY_RESPONSE")
                
        except Exception as e:
            error_msg = str(e).lower()
            now_ms = time.time() * 1000
            key_fp = get_key_fingerprint(key)[:8]
            
            if "404" in error_msg or "not_found" in error_msg:
                state['status'] = 'exhausted'
                mark_key_daily_exhausted(key, model)
                print(f"[Model Unavailable] 404 on model {model} (Key {key_fp}). Disabled for this model only.", flush=True)
                continue
                
            if "429" in error_msg or "quota" in error_msg or "resource exhausted" in error_msg:
                if "perday" in error_msg or ("freetier" in error_msg and "day" in error_msg):
                    state['status'] = 'exhausted'
                    mark_key_daily_exhausted(key, model)
                    print(f"[Quota Exhausted] Daily RPD limit reached for model {model} (Key {key_fp}). Disabled for this model only.", flush=True)
                    continue
                elif "perminute" in error_msg or "rpm" in error_msg:
                    state['cooldown_until'] = now_ms + 2000
                    print(f"[Rate Limit] RPM exceeded on model {model} (Key {key_fp}). Cooldown 2s.", flush=True)
                    continue
                else:
                    state['consecutive_generic_429s'] += 1
                    cooldown_ms = min(60000 * state['consecutive_generic_429s'], 300000)
                    state['cooldown_until'] = now_ms + cooldown_ms
                    print(f"[Rate Limit Hit] Generic 429 on model {model} (Key {key_fp}). Cooldown set for {cooldown_ms/1000}s.", flush=True)
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
                    print(f"[Fallback] All keys exhausted for model {model}. Pivoting to next model in pipeline.", flush=True)
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
        
    init_key_usage_tracker()
    archive_data = load_and_migrate_archive(ARCHIVE_FILE)
    proxy_db = load_json(PROXY_DB_FILE, {})
    
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
        pipeline_type = pipeline.get("type", "two_pass")

        if archive_key not in archive_data:
            archive_data[archive_key] = {}
        archive_set = archive_data[archive_key]

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
                entry['image_url'] = extract_image_from_entry(entry)
                to_process.append(entry)

        if not to_process:
            print(f"--- No new candidate articles for {pipeline['name']} ---", flush=True)
            continue

        print(f"--- Processing {len(to_process)} articles for {pipeline['name']} ---", flush=True)

        # =========================================================================
        # EXECUTION PATH A: FOUR-CATEGORY SCORING PIPELINE (BATCH SIZE 3)
        # =========================================================================
        if pipeline_type == "scoring":
            categories_rubrics = {
                cat: load_text(path) for cat, path in pipeline["category_files"].items()
            }
            batch_size = pipeline.get("batch_size", 3)
            category_thresholds = pipeline.get("category_thresholds", {
                "A": 5.0,
                "B": 3.0,
                "C": 3.0,
                "D": 5.0
            })
            scoring_models = pipeline.get("models", STAGE1_MODELS)
            total_batches = math.ceil(len(to_process) / batch_size)

            for i in range(0, len(to_process), batch_size):
                batch = to_process[i:i+batch_size]
                batch_number = (i // batch_size) + 1

                for art in batch:
                    link = art.get('clean_link') or art.get('link', '')
                    if 'cached_full_text' not in art:
                        text, page_img = fetch_article_details(link) if link else ("", "")
                        art['cached_full_text'] = text
                        if page_img and not art.get('image_url'):
                            art['image_url'] = page_img

                articles_payload = ""
                for idx, art in enumerate(batch):
                    content = art.get('cached_full_text') or art.get('summary', '')
                    articles_payload += f"--- Article {idx+1} ---\nTitle: {art.get('title')}\nPublished: {art.get('published', 'Unknown')}\nContent: {content}\n\n"

                batch_scores = {idx: {} for idx in range(len(batch))}
                batch_failed = False

                for cat_label, rubric in categories_rubrics.items():
                    prompt = CATEGORY_SCORING_PROMPT_TEMPLATE.format(
                        category_label=cat_label,
                        rubric_text=rubric,
                        batch_len=len(batch),
                        articles_payload=articles_payload
                    )
                    evaluations, used_model = evaluate_batch(prompt, len(batch), scoring_models, CategoryBatchScore)
                    if evaluations:
                        for idx, eval_result in enumerate(evaluations):
                            batch_scores[idx][cat_label] = {
                                "score": eval_result.score,
                                "rationale": eval_result.rationale
                            }
                    else:
                        batch_failed = True
                        print(f"[Scoring - Batch {batch_number}/{total_batches}] FAILED on Category {cat_label}.", flush=True)
                        break

                if not batch_failed:
                    for idx, art in enumerate(batch):
                        art_id = str(art.get('id', art.get('clean_link', art.get('link'))))
                        art_title = art.get('title', '').strip()

                        archive_set[art_id] = now_ts
                        if art.get('clean_link'):
                            archive_set[art['clean_link']] = now_ts
                        if art_title:
                            archive_set[art_title] = now_ts

                        scores = batch_scores[idx]
                        score_summary = ", ".join([
                            f"{c}: {scores[c]['score']:.1f} (req > {category_thresholds.get(c, 3.0):.1f})" 
                            for c in sorted(scores.keys())
                        ])

                        qualifying_categories = [
                            c for c, data in scores.items()
                            if data["score"] > category_thresholds.get(c, 3.0)
                        ]
                        is_candidate = len(qualifying_categories) > 0

                        print(f"  -> Title: {art_title}", flush=True)
                        print(f"     Scores: [{score_summary}]", flush=True)

                        if is_candidate:
                            new_articles_per_feed[target_feed_id] += 1
                            passed_str = ", ".join(qualifying_categories)
                            print(f"     Decision: Candidate Accepted (Met threshold in Cat: {passed_str})\n", flush=True)

                            final_image_url = art.get('image_url') or extract_image_from_entry(art)
                            if not final_image_url and (art.get('clean_link') or art.get('link')):
                                _, scraped_img = fetch_article_details(art.get('clean_link') or art.get('link'))
                                final_image_url = scraped_img

                            proxy_db[target_feed_id]['articles'].append({
                                'id': art_id,
                                'title': art.get('title', 'No Title'),
                                'link': art.get('clean_link') or art.get('link', ''),
                                'description': art.get('summary', art.get('description', '')),
                                'published': art.get('published', art.get('updated', '')),
                                'image_url': final_image_url
                            })
                        else:
                            print("     Decision: Rejected (Did not meet threshold in any category)\n", flush=True)

                    print(f"[Scoring - Batch {batch_number}/{total_batches}] Processed successfully.", flush=True)
                else:
                    print(f"[Scoring - Batch {batch_number}/{total_batches}] Batch skipped due to API failure. Will retry next run.", flush=True)
            continue

        # =========================================================================
        # EXECUTION PATH B: STANDARD TWO-PASS FILTERING PIPELINES
        # =========================================================================
        interests_text = load_text(pipeline["criteria_file"])
        passed_stage1 = []

        if pipeline.get("requires_stage1", False):
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
                        text, page_img = fetch_article_details(link) if link else ("", "")
                        art['cached_full_text'] = text
                        if page_img and not art.get('image_url'):
                            art['image_url'] = page_img
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
                    text, page_img = fetch_article_details(link) if link else ("", "")
                    art['cached_full_text'] = text
                    if page_img and not art.get('image_url'):
                        art['image_url'] = page_img

        if not passed_stage1:
            print(f"--- All candidate articles rejected in Stage 1 for {pipeline['name']} ---", flush=True)
            continue

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
                        
                        final_image_url = art.get('image_url') or extract_image_from_entry(art)
                        if not final_image_url and (art.get('clean_link') or art.get('link')):
                            _, scraped_img = fetch_article_details(art.get('clean_link') or art.get('link'))
                            final_image_url = scraped_img

                        proxy_db[target_feed_id]['articles'].append({
                            'id': art_id,
                            'title': art.get('title', 'No Title'),
                            'link': art.get('clean_link') or art.get('link', ''),
                            'description': art.get('summary', art.get('description', '')),
                            'published': art.get('published', art.get('updated', '')),
                            'image_url': final_image_url
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

            desc = art.get('description', '')
            img_url = art.get('image_url', '')

            if img_url:
                mime_type = 'image/jpeg'
                if '.png' in img_url.lower():
                    mime_type = 'image/png'
                elif '.webp' in img_url.lower():
                    mime_type = 'image/webp'
                elif '.gif' in img_url.lower():
                    mime_type = 'image/gif'

                fe.enclosure(url=img_url, length='0', type=mime_type)

                if '<img' not in desc.lower():
                    fe.description(f'<p><img src="{img_url}" alt="thumbnail" /></p>' + desc)
                else:
                    fe.description(desc)
            else:
                fe.description(desc)

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
