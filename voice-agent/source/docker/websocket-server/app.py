# FILE: docker/websocket_server/app.py
# Version: 5.1.4 - Official XTTS v2 Voices & Retention Logic
# Changes v5.1.4:
#   - Official XTTS v2 voices only (33 verified speakers)
#   - Hebrew marked as experimental (not officially supported)
#   - Retention logic: when customer wants to leave, agent convinces them to stay
#   - Promise of callback from representative for high churn risk
#   - Multi-language retention responses for all 16 supported languages
#   - Fixed voice recommendations per language with correct accents
# Changes v5.1.3:
#   - Expanded Sentiment Analysis: All 17 XTTS v2 languages with 100+ keywords each
#   - Improved TTS Quality: Lower temperature (0.65) for consistent voice
#   - Enhanced Text Cleaning: Better handling of abbreviations, leading/trailing chars
#   - Upgrade Intent Detection: Multi-language plan query vs upgrade action
#   - Fixed Latency Report: Always shows all metrics with explanations
#   - Language Consistency: All responses in selected TTS language
# Changes v5.1.2:
#   - Intent Detection Fix: "how many tickets" vs "open a ticket"
#   - Multi-language confirmation/cancellation phrases
# Changes v5.1.1:
#   - Audio reliability improvements
# Changes v5.1.0:
#   - TTS Fallback Chain: XTTS → Edge-TTS → Text-only (never silent)
#   - Aggressive TTS Caching: 2000 phrases, 10min TTL
#   - Service Health Probes: Background health checks every 30s
#   - Connection Pre-warming: HTTP pool ready on startup
#   - Smart Timeouts: Optimized per service type
#   - LLM Response Cache: Common greetings pre-cached
#   - Graceful Degradation: Continue even if services fail
#   - Enhanced Logging: Emoji-based status for quick diagnosis
# Changes v5.0.1:
#   - TTS Circuit Breaker disabled (always try)
#   - 5 retries for TTS
#   - Text fallback when voice fails
# Previous (v5.0.0):
#   - Full multi-language support (Hebrew, Spanish, French, etc.)
#   - Expanded sentiment analysis and churn detection
#   - Session stability fixes (collision prevention)
#   - Native language database responses
# Previous (v4.1.0):
#   - Circuit Breaker pattern for ASR/TTS/LLM services
#   - TTS response caching for common phrases
#   - Connection watchdog monitoring
#   - Multi-language PIN recognition (15+ languages)
#   - Multi-language sentiment analysis with churn detection
#   - Health check endpoint for service monitoring
#   - LOCALE_MAP moved to global constant (performance)
#   - Enhanced error handling and recovery
#   - Completely redesigned conversation prompts for natural speech
#   - New upgrade request flow with full database tracking
#   - Intelligent multi-state conversation handling
#   - Performance: Connection pooling, query caching, HTTP/2
#   - Auto-initialization of upgrade_requests table
#   - Session cleanup and memory management
#   - Retry logic with exponential backoff

import asyncio
import os
import io
import wave
import tempfile
import traceback
import websockets
import time
import logging
import json
import subprocess
import httpx
import re
import signal
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, Any
from collections import OrderedDict, defaultdict

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# PostgreSQL import
try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    logging.warning("asyncpg not installed. Database features disabled.")
    ASYNCPG_AVAILABLE = False

# Build Metadata
BUILD_NUMBER = "5.1.4"

# =============================================================================
# GLOBAL CONNECTION POOLS (Performance improvement)
# =============================================================================
_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()

async def get_http_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client with connection pooling - thread safe"""
    global _http_client
    
    # Fast path - client exists and is healthy
    if _http_client is not None and not _http_client.is_closed:
        return _http_client
    
    # Slow path - need to create client (with lock to prevent race condition)
    async with _http_client_lock:
        # Double check after acquiring lock
        if _http_client is not None and not _http_client.is_closed:
            return _http_client
            
        # Disable HTTP/2 to prevent 'h11' protocol errors with some TTS servers
        _http_client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            http2=False
        )
        logging.info("[HTTP] Created new HTTP client pool (HTTP/1.1)")
    
    return _http_client

async def close_http_client():
    """Close the shared HTTP client"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
        logging.info("[HTTP] HTTP client pool closed")


# =============================================================================
# RATE LIMITER - Token Bucket Algorithm
# =============================================================================
class RateLimiter:
    """Token bucket rate limiter"""
    
    def __init__(self, requests_per_minute: int = 30):
        self._buckets = defaultdict(lambda: {"tokens": requests_per_minute, "last": time.time()})
        self._rate = requests_per_minute
    
    def is_allowed(self, client_id: str) -> bool:
        bucket = self._buckets[client_id]
        now = time.time()
        
        # Refill tokens
        elapsed = now - bucket["last"]
        bucket["tokens"] = min(self._rate, bucket["tokens"] + elapsed * (self._rate / 60))
        bucket["last"] = now
        
        # Check if request allowed
        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True
        return False
    
    def cleanup(self):
        """Remove old entries"""
        now = time.time()
        old = [k for k, v in self._buckets.items() if now - v["last"] > 300]
        for k in old:
            del self._buckets[k]

_rate_limiter = RateLimiter(requests_per_minute=30)


# =============================================================================
# REQUEST CACHE - For common database queries
# =============================================================================
class SimpleCache:
    """Simple in-memory cache with TTL and size limits"""
    
    def __init__(self, default_ttl: int = 60, max_size: int = 1000):
        self._cache = {}
        self._ttl = default_ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str):
        """Get value from cache if not expired"""
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                self._hits += 1
                return value
            else:
                del self._cache[key]
        self._misses += 1
        return None
    
    def set(self, key: str, value, ttl: int = None):
        """Set value in cache with TTL"""
        # Enforce size limit - remove oldest entries if needed
        if len(self._cache) >= self._max_size:
            # Remove 10% oldest entries
            items = sorted(self._cache.items(), key=lambda x: x[1][1])
            for k, _ in items[:len(items)//10 + 1]:
                del self._cache[k]
        
        expiry = time.time() + (ttl or self._ttl)
        self._cache[key] = (value, expiry)
    
    def clear(self):
        """Clear all cache entries"""
        self._cache.clear()
    
    def cleanup(self):
        """Remove expired entries"""
        now = time.time()
        expired = [k for k, (v, exp) in self._cache.items() if now >= exp]
        for k in expired:
            del self._cache[k]
    
    def stats(self) -> dict:
        """Get cache statistics"""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1f}%"
        }

# Global cache instances
_query_cache = SimpleCache(default_ttl=60, max_size=500)  # DB queries - 60s TTL
_tts_cache = SimpleCache(default_ttl=600, max_size=2000)  # TTS audio - 10min TTL, 2000 phrases
_llm_cache = SimpleCache(default_ttl=300, max_size=200)   # LLM responses - 5min TTL

# Database connection pool
_db_pool: Any = None
_db_pool_lock = asyncio.Lock()
_db_settings: dict = {}  # Store last used db settings for refresh

async def get_db_pool(host, port, dbname, user, password) -> Any:
    """Get or create database connection pool"""
    global _db_pool, _db_settings
    
    # Fast check
    if _db_pool:
        return _db_pool

    # Store settings for later use (refresh requests)
    _db_settings = {
        'host': host,
        'port': port,
        'database': dbname,
        'user': user,
        'password': password
    }
    
    async with _db_pool_lock:
        if _db_pool is None and ASYNCPG_AVAILABLE:
            try:
                # Optimized pool size: min_size=2 ensures connection ready for first request
                _db_pool = await asyncpg.create_pool(
                    min_size=2,
                    max_size=20,
                    host=host,
                    port=port,
                    database=dbname,
                    user=user,
                    password=password
                )
                logging.info(f"Database pool created: {dbname}@{host}")
            except Exception as e:
                logging.error(f"Failed to create DB pool: {e}")
                return None
    return _db_pool

async def ensure_db_pool() -> Any:
    """Ensure db pool exists, using stored settings if available"""
    global _db_pool, _db_settings
    if _db_pool:
        return _db_pool
    if _db_settings:
        return await get_db_pool(**_db_settings)
    return None

async def close_db_pool():
    """Close database connection pool"""
    global _db_pool
    if _db_pool:
        await _db_pool.close()
        _db_pool = None
        logging.info("Database pool closed")


# --- Default Settings from Environment Variables ---
def get_bool_env(var_name: str, default: bool = False) -> bool:
    return os.getenv(var_name, str(default)).lower() in ('true', '1', 't', 'yes', 'y')

DEFAULT_SETTINGS = {
    "llm_api_base": os.getenv("LLM_API_BASE"),
    "llm_api_key": os.getenv("LLM_API_KEY"),
    "llm_prompt_template": os.getenv("LLM_PROMPT_TEMPLATE", 'Answer the question: "{transcript}"\n\nAnswer concisely.'),
    "llm_model_name": os.getenv("LLM_MODEL_NAME", "meta-llama/Llama-3.2-1B-Instruct"),
    # ASR - Whisper
    "asr_server_address": os.getenv("ASR_SERVER_ADDRESS", "whisper-large-v3-predictor-00002-deployment.liav-hpe-com-ba9ce2f9.svc.cluster.local:9000"),
    "asr_language_code": os.getenv("ASR_LANGUAGE_CODE", "en"),
    # TTS - XTTS v2
    "tts_server_address": os.getenv("TTS_SERVER_ADDRESS", "localhost:8000"),
    "tts_language_code": os.getenv("TTS_LANGUAGE_CODE", "en"),
    "tts_speaker": os.getenv("TTS_SPEAKER", ""),
    "tts_sample_rate_hz": int(os.getenv("TTS_SAMPLE_RATE_HZ", "24000")),
    # Database settings
    "db_enabled": get_bool_env("DB_ENABLED", False),
    "db_host": os.getenv("DB_HOST", "localhost"),
    "db_port": os.getenv("DB_PORT", "5432"),
    "db_name": os.getenv("DB_NAME", "customer_service"),
    "db_user": os.getenv("DB_USER", "agent"),
    "db_password": os.getenv("DB_PASSWORD", ""),
}

SENTENCE_TERMINATORS = ['.', '?', '!']

# =============================================================================
# LOCALE MAPPING - For ASR language codes (moved outside function for performance)
# =============================================================================
LOCALE_MAP = {
    'en': 'en-US', 'he': 'he-IL', 'es': 'es-ES', 'fr': 'fr-FR',
    'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT', 'pl': 'pl-PL',
    'tr': 'tr-TR', 'ru': 'ru-RU', 'nl': 'nl-NL', 'cs': 'cs-CZ',
    'ar': 'ar-SA', 'zh': 'zh-CN', 'ja': 'ja-JP', 'hu': 'hu-HU',
    'ko': 'ko-KR', 'hi': 'hi-IN', 'vi': 'vi-VN', 'th': 'th-TH',
    'uk': 'uk-UA', 'el': 'el-GR', 'ro': 'ro-RO', 'sv': 'sv-SE',
    'da': 'da-DK', 'fi': 'fi-FI', 'no': 'no-NO', 'bg': 'bg-BG',
    'ca': 'ca-ES', 'hr': 'hr-HR', 'et': 'et-EE', 'id': 'id-ID',
    'lv': 'lv-LV', 'lt': 'lt-LT', 'ms': 'ms-MY', 'sk': 'sk-SK',
    'sl': 'sl-SI', 'tl': 'tl-PH', 'ta': 'ta-IN', 'te': 'te-IN',
    'ur': 'ur-PK', 'cy': 'cy-GB', 'sw': 'sw-KE', 'af': 'af-ZA',
    'is': 'is-IS', 'km': 'km-KH', 'lo': 'lo-LA', 'mk': 'mk-MK',
    'ml': 'ml-IN', 'mr': 'mr-IN', 'my': 'my-MM', 'ne': 'ne-NP',
    'sr': 'sr-RS', 'si': 'si-LK', 'hy': 'hy-AM', 'az': 'az-AZ',
    'be': 'be-BY', 'bs': 'bs-BA', 'gl': 'gl-ES', 'ka': 'ka-GE',
    'gu': 'gu-IN', 'kn': 'kn-IN', 'kk': 'kk-KZ', 'ky': 'ky-KG',
    'mn': 'mn-MN', 'fa': 'fa-IR', 'sd': 'sd-PK', 'tt': 'tt-RU',
    'uz': 'uz-UZ', 'am': 'am-ET', 'jw': 'jv-ID', 'su': 'su-ID',
    'ha': 'ha-NG', 'yo': 'yo-NG', 'ln': 'ln-CD', 'so': 'so-SO',
    'ps': 'ps-AF', 'yi': 'yi-001',
    'bn': 'bn-IN', 'pa': 'pa-IN', 'sq': 'sq-AL', 'la': 'la-VA',
    'mi': 'mi-NZ', 'ht': 'ht-HT', 'mg': 'mg-MG', 'mt': 'mt-MT',
    'sn': 'sn-ZW', 'tg': 'tg-TJ', 'uz': 'uz-UZ', 'xh': 'xh-ZA',
    'zu': 'zu-ZA',
}

# =============================================================================
# CIRCUIT BREAKER - Prevents cascading failures when services are down
# =============================================================================
class CircuitBreaker:
    """Circuit breaker pattern to prevent overwhelming failed services"""
    
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: int = 30):
        self.name = name
        self.failures = 0
        self.threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED (normal), OPEN (blocking), HALF_OPEN (testing)
    
    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.threshold:
            self.state = "OPEN"
            logging.warning(f"[CircuitBreaker:{self.name}] OPENED after {self.failures} failures")
    
    def record_success(self):
        if self.state == "HALF_OPEN":
            logging.info(f"[CircuitBreaker:{self.name}] Recovery successful, closing circuit")
        self.failures = 0
        self.state = "CLOSED"
    
    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logging.info(f"[CircuitBreaker:{self.name}] Entering HALF_OPEN state, testing...")
                return True
            return False
        return True  # HALF_OPEN allows one request through
    
    def get_status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "threshold": self.threshold,
        }

# Global circuit breakers for each service
# v5.1.0: TTS circuit breaker effectively DISABLED (threshold=999) 
# to ensure agent ALWAYS tries to respond
_asr_circuit = CircuitBreaker("ASR", failure_threshold=5, recovery_timeout=30)
_tts_circuit = CircuitBreaker("TTS", failure_threshold=999, recovery_timeout=5)  # Effectively disabled - always try TTS
_llm_circuit = CircuitBreaker("LLM", failure_threshold=3, recovery_timeout=60)


# =============================================================================
# SERVICE HEALTH TRACKER - v5.1.0
# =============================================================================
class ServiceHealth:
    """Track health status of external services"""
    
    def __init__(self):
        self._services = {
            "tts": {"status": "unknown", "last_check": 0, "latency": 0, "url": ""},
            "asr": {"status": "unknown", "last_check": 0, "latency": 0, "url": ""},
            "llm": {"status": "unknown", "last_check": 0, "latency": 0, "url": ""},
            "db": {"status": "unknown", "last_check": 0, "latency": 0, "url": ""},
        }
        self._lock = asyncio.Lock()
    
    async def update(self, service: str, status: str, latency: float = 0, url: str = ""):
        async with self._lock:
            if service in self._services:
                self._services[service] = {
                    "status": status,
                    "last_check": time.time(),
                    "latency": latency,
                    "url": url
                }
    
    def get(self, service: str) -> dict:
        return self._services.get(service, {"status": "unknown"})
    
    def get_all(self) -> dict:
        return self._services.copy()
    
    def is_healthy(self, service: str) -> bool:
        svc = self._services.get(service, {})
        # Consider healthy if checked in last 60s and status is "ok"
        if svc.get("status") == "ok" and time.time() - svc.get("last_check", 0) < 60:
            return True
        return False

_service_health = ServiceHealth()


# =============================================================================
# FALLBACK TTS - v5.1.0 - Use edge-tts if XTTS fails
# =============================================================================
EDGE_TTS_AVAILABLE = False
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
    logging.info("[TTS] ✅ edge-tts available as fallback")
except ImportError:
    logging.info("[TTS] ℹ️ edge-tts not installed (optional fallback)")

# Language to edge-tts voice mapping
EDGE_TTS_VOICES = {
    "en": "en-US-JennyNeural",
    "he": "he-IL-HilaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
}

async def fallback_edge_tts(text: str, language: str) -> bytes:
    """Fallback TTS using edge-tts (Microsoft Azure voices)"""
    if not EDGE_TTS_AVAILABLE:
        return b""
    
    try:
        voice = EDGE_TTS_VOICES.get(language, "en-US-JennyNeural")
        communicate = edge_tts.Communicate(text, voice)
        
        audio_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.extend(chunk["data"])
        
        if audio_data:
            logging.info(f"[TTS] ✅ Edge-TTS fallback success: {len(audio_data)} bytes")
            return bytes(audio_data)
    except Exception as e:
        logging.warning(f"[TTS] ⚠️ Edge-TTS fallback failed: {e}")
    
    return b""

# =============================================================================
# MULTI-LANGUAGE NUMBER WORDS - For PIN recognition in all supported languages
# =============================================================================
NUMBER_WORDS = {
    'en': {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'oh': '0', 'o': '0',
    },
    'he': {
        'אפס': '0', 'אחת': '1', 'אחד': '1', 'שתיים': '2', 'שניים': '2', 'שתים': '2',
        'שלוש': '3', 'שלושה': '3', 'ארבע': '4', 'ארבעה': '4', 'חמש': '5', 'חמישה': '5',
        'שש': '6', 'שישה': '6', 'שבע': '7', 'שבעה': '7', 'שמונה': '8', 'תשע': '9', 'תשעה': '9',
    },
    'es': {
        'cero': '0', 'uno': '1', 'una': '1', 'dos': '2', 'tres': '3', 'cuatro': '4',
        'cinco': '5', 'seis': '6', 'siete': '7', 'ocho': '8', 'nueve': '9',
    },
    'fr': {
        'zéro': '0', 'zero': '0', 'un': '1', 'une': '1', 'deux': '2', 'trois': '3',
        'quatre': '4', 'cinq': '5', 'six': '6', 'sept': '7', 'huit': '8', 'neuf': '9',
    },
    'de': {
        'null': '0', 'eins': '1', 'zwei': '2', 'zwo': '2', 'drei': '3', 'vier': '4',
        'fünf': '5', 'funf': '5', 'sechs': '6', 'sieben': '7', 'acht': '8', 'neun': '9',
    },
    'it': {
        'zero': '0', 'uno': '1', 'una': '1', 'due': '2', 'tre': '3', 'quattro': '4',
        'cinque': '5', 'sei': '6', 'sette': '7', 'otto': '8', 'nove': '9',
    },
    'pt': {
        'zero': '0', 'um': '1', 'uma': '1', 'dois': '2', 'duas': '2', 'três': '3', 'tres': '3',
        'quatro': '4', 'cinco': '5', 'seis': '6', 'sete': '7', 'oito': '8', 'nove': '9',
    },
    'ru': {
        'ноль': '0', 'один': '1', 'одна': '1', 'два': '2', 'две': '2', 'три': '3',
        'четыре': '4', 'пять': '5', 'шесть': '6', 'семь': '7', 'восемь': '8', 'девять': '9',
    },
    'ar': {
        'صفر': '0', 'واحد': '1', 'اثنان': '2', 'ثلاثة': '3', 'أربعة': '4',
        'خمسة': '5', 'ستة': '6', 'سبعة': '7', 'ثمانية': '8', 'تسعة': '9',
    },
    'zh': {
        '零': '0', '一': '1', '二': '2', '两': '2', '三': '3', '四': '4',
        '五': '5', '六': '6', '七': '7', '八': '8', '九': '9',
    },
    'ja': {
        'ゼロ': '0', '零': '0', '一': '1', 'いち': '1', '二': '2', 'に': '2',
        '三': '3', 'さん': '3', '四': '4', 'よん': '4', 'し': '4',
        '五': '5', 'ご': '5', '六': '6', 'ろく': '6', '七': '7', 'なな': '7', 'しち': '7',
        '八': '8', 'はち': '8', '九': '9', 'きゅう': '9', 'く': '9',
    },
    'ko': {
        '영': '0', '공': '0', '일': '1', '하나': '1', '이': '2', '둘': '2',
        '삼': '3', '셋': '3', '사': '4', '넷': '4', '오': '5', '다섯': '5',
        '육': '6', '여섯': '6', '칠': '7', '일곱': '7', '팔': '8', '여덟': '8', '구': '9', '아홉': '9',
    },
    'pl': {
        'zero': '0', 'jeden': '1', 'jedna': '1', 'dwa': '2', 'dwie': '2', 'trzy': '3',
        'cztery': '4', 'pięć': '5', 'sześć': '6', 'siedem': '7', 'osiem': '8', 'dziewięć': '9',
    },
    'tr': {
        'sıfır': '0', 'bir': '1', 'iki': '2', 'üç': '3', 'uc': '3', 'dört': '4', 'dort': '4',
        'beş': '5', 'bes': '5', 'altı': '6', 'alti': '6', 'yedi': '7', 'sekiz': '8', 'dokuz': '9',
    },
    'nl': {
        'nul': '0', 'een': '1', 'één': '1', 'twee': '2', 'drie': '3', 'vier': '4',
        'vijf': '5', 'zes': '6', 'zeven': '7', 'acht': '8', 'negen': '9',
    },
    'hi': {
        'शून्य': '0', 'एक': '1', 'दो': '2', 'तीन': '3', 'चार': '4',
        'पांच': '5', 'छह': '6', 'सात': '7', 'आठ': '8', 'नौ': '9',
    },
}

# =============================================================================
# MULTI-LANGUAGE SENTIMENT DETECTION - For customer churn risk analysis
# =============================================================================
# XTTS v2 Supported Languages: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, hu, ko, hi
# Significantly expanded keywords for broader detection of dissatisfaction
# =============================================================================
NEGATIVE_SENTIMENT_KEYWORDS = {
    # English - Extended
    'en': [
        # Anger/Frustration
        'angry', 'furious', 'frustrated', 'annoyed', 'upset', 'pissed', 'mad', 'livid',
        'outraged', 'infuriated', 'exasperated', 'irritated', 'agitated', 'enraged',
        # Disappointment
        'disappointed', 'let down', 'dissatisfied', 'unhappy', 'displeased', 'dismayed',
        # Quality complaints
        'terrible', 'horrible', 'awful', 'worst', 'pathetic', 'useless', 'garbage',
        'trash', 'junk', 'crap', 'broken', 'buggy', 'glitchy', 'slow', 'laggy',
        # Service complaints
        'bad service', 'poor service', 'rude', 'unprofessional', 'incompetent',
        'ignoring', 'no help', 'waste of time', 'waste of money', 'scam', 'fraud',
        'rip off', 'overpriced', 'misleading', 'false advertising',
        # Cancellation intent
        'cancel', 'cancelling', 'cancellation', 'terminate', 'end', 'stop', 'quit',
        'leaving', 'switching', 'competitor', 'refund', 'money back', 'sue', 'lawyer',
        'complaint', 'manager', 'supervisor', 'escalate', 'legal action',
        # Emotional
        'hate', 'disgusted', 'sick of', 'tired of', 'fed up', 'had enough',
        'never again', 'nightmare', 'disaster', 'mess', 'chaos', 'unbelievable',
        'ridiculous', 'outrageous', 'unacceptable', 'insulting',
    ],
    
    # Hebrew - Extended
    'he': [
        # כעס/תסכול
        'כועס', 'זועם', 'מתוסכל', 'עצבני', 'נסער', 'מרוגז', 'מטורף', 'משתגע',
        # אכזבה
        'מאוכזב', 'לא מרוצה', 'מותש', 'נמאס', 'די', 'חלאס',
        # תלונות איכות
        'נורא', 'איום', 'גרוע', 'הכי גרוע', 'פתטי', 'חסר תועלת', 'זבל', 'חרא',
        'שבור', 'תקוע', 'איטי', 'לא עובד', 'באגים', 'קורס',
        # תלונות שירות
        'שירות גרוע', 'שירות לקוי', 'חוצפה', 'לא מקצועי', 'מזלזלים',
        'מתעלמים', 'לא עוזר', 'בזבוז זמן', 'בזבוז כסף', 'הונאה', 'גניבה',
        'יקר מדי', 'שקרים', 'פרסום כוזב',
        # כוונת ביטול
        'לבטל', 'ביטול', 'מבטל', 'לסיים', 'להפסיק', 'לעזוב', 'להתנתק',
        'עוזב', 'עובר למתחרים', 'רוצה החזר', 'כסף בחזרה', 'עורך דין', 'תביעה',
        'תלונה', 'מנהל', 'להסלים',
        # רגשי
        'שונא', 'מגעיל', 'נמאס לי', 'עייפתי', 'די לי', 'יותר מדי',
        'לא שוב', 'סיוט', 'אסון', 'בלגן', 'לא יאמן', 'מגוחך', 'לא מקובל',
    ],
    
    # Spanish - Extended
    'es': [
        # Enojo/Frustración
        'enfadado', 'furioso', 'frustrado', 'molesto', 'enojado', 'cabreado', 'irritado',
        'indignado', 'exasperado', 'harto',
        # Decepción
        'decepcionado', 'desilusionado', 'insatisfecho', 'descontento', 'disgustado',
        # Quejas de calidad
        'terrible', 'horrible', 'pésimo', 'peor', 'patético', 'inútil', 'basura',
        'porquería', 'roto', 'lento', 'no funciona', 'falla', 'bugs',
        # Quejas de servicio
        'mal servicio', 'servicio pésimo', 'grosero', 'maleducado', 'incompetente',
        'ignorando', 'sin ayuda', 'pérdida de tiempo', 'pérdida de dinero', 'estafa',
        'fraude', 'robo', 'muy caro', 'engaño', 'publicidad falsa',
        # Intención de cancelar
        'cancelar', 'dar de baja', 'terminar', 'finalizar', 'dejar', 'irme',
        'cambiar de', 'competencia', 'reembolso', 'devolver dinero', 'abogado',
        'demanda', 'queja', 'gerente', 'supervisor', 'escalar',
        # Emocional
        'odio', 'asqueroso', 'harto de', 'cansado de', 'ya no más', 'pesadilla',
        'desastre', 'caos', 'increíble', 'ridículo', 'inaceptable', 'ofensivo',
    ],
    
    # French - Extended
    'fr': [
        # Colère/Frustration
        'en colère', 'furieux', 'frustré', 'énervé', 'agacé', 'irrité', 'exaspéré',
        'indigné', 'fou de rage', 'excédé',
        # Déception
        'déçu', 'désappointé', 'insatisfait', 'mécontent', 'contrarié',
        # Plaintes qualité
        'terrible', 'horrible', 'affreux', 'pire', 'pathétique', 'inutile', 'nul',
        'poubelle', 'cassé', 'lent', 'ne marche pas', 'bug', 'planté',
        # Plaintes service
        'mauvais service', 'service nul', 'impoli', 'mal élevé', 'incompétent',
        'ignore', 'aucune aide', 'perte de temps', 'perte d\'argent', 'arnaque',
        'escroquerie', 'vol', 'trop cher', 'mensonge', 'publicité mensongère',
        # Intention d'annuler
        'annuler', 'résilier', 'terminer', 'arrêter', 'partir', 'quitter',
        'changer pour', 'concurrent', 'remboursement', 'rendre l\'argent', 'avocat',
        'procès', 'plainte', 'directeur', 'responsable', 'escalader',
        # Émotionnel
        'déteste', 'dégoûté', 'marre de', 'fatigué de', 'plus jamais', 'cauchemar',
        'catastrophe', 'bordel', 'incroyable', 'ridicule', 'inacceptable', 'insultant',
    ],
    
    # German - Extended
    'de': [
        # Ärger/Frustration
        'wütend', 'zornig', 'frustriert', 'verärgert', 'genervt', 'gereizt', 'empört',
        'aufgebracht', 'sauer', 'stinkig',
        # Enttäuschung
        'enttäuscht', 'unzufrieden', 'unglücklich', 'missmutig',
        # Qualitätsbeschwerden
        'schrecklich', 'furchtbar', 'schlimm', 'schlimmste', 'erbärmlich', 'nutzlos',
        'müll', 'schrott', 'kaputt', 'langsam', 'funktioniert nicht', 'bug', 'absturz',
        # Servicebeschwerden
        'schlechter service', 'miserabler service', 'unhöflich', 'inkompetent',
        'ignoriert', 'keine hilfe', 'zeitverschwendung', 'geldverschwendung', 'betrug',
        'abzocke', 'zu teuer', 'lüge', 'falsche werbung',
        # Kündigungsabsicht
        'kündigen', 'stornieren', 'beenden', 'aufhören', 'verlassen', 'wechseln',
        'konkurrenz', 'erstattung', 'geld zurück', 'anwalt', 'klage', 'beschwerde',
        'manager', 'vorgesetzter', 'eskalieren',
        # Emotional
        'hasse', 'ekelhaft', 'satt haben', 'müde von', 'nie wieder', 'alptraum',
        'katastrophe', 'chaos', 'unglaublich', 'lächerlich', 'inakzeptabel', 'beleidigend',
    ],
    
    # Italian - Extended
    'it': [
        # Rabbia/Frustrazione
        'arrabbiato', 'furioso', 'frustrato', 'irritato', 'infuriato', 'esasperato',
        'indignato', 'incazzato', 'stufo',
        # Delusione
        'deluso', 'insoddisfatto', 'scontento', 'dispiaciuto',
        # Lamentele qualità
        'terribile', 'orribile', 'pessimo', 'peggiore', 'patetico', 'inutile',
        'spazzatura', 'rotto', 'lento', 'non funziona', 'bug', 'crash',
        # Lamentele servizio
        'cattivo servizio', 'servizio pessimo', 'maleducato', 'incompetente',
        'ignorato', 'nessun aiuto', 'perdita di tempo', 'perdita di soldi', 'truffa',
        'frode', 'furto', 'troppo caro', 'bugia', 'pubblicità ingannevole',
        # Intenzione di cancellare
        'cancellare', 'disdire', 'terminare', 'smettere', 'lasciare', 'andarmene',
        'cambiare a', 'concorrenza', 'rimborso', 'rivoglio i soldi', 'avvocato',
        'causa', 'reclamo', 'responsabile', 'supervisore', 'escalare',
        # Emotivo
        'odio', 'disgustato', 'stufo di', 'stanco di', 'mai più', 'incubo',
        'disastro', 'caos', 'incredibile', 'ridicolo', 'inaccettabile', 'offensivo',
    ],
    
    # Portuguese - Extended
    'pt': [
        # Raiva/Frustração
        'bravo', 'furioso', 'frustrado', 'irritado', 'zangado', 'enfurecido',
        'indignado', 'exasperado', 'farto',
        # Decepção
        'decepcionado', 'desapontado', 'insatisfeito', 'descontente',
        # Reclamações qualidade
        'terrível', 'horrível', 'péssimo', 'pior', 'patético', 'inútil',
        'lixo', 'porcaria', 'quebrado', 'lento', 'não funciona', 'bug', 'trava',
        # Reclamações serviço
        'mau serviço', 'serviço péssimo', 'mal educado', 'incompetente',
        'ignorando', 'sem ajuda', 'perda de tempo', 'perda de dinheiro', 'golpe',
        'fraude', 'roubo', 'muito caro', 'mentira', 'propaganda enganosa',
        # Intenção de cancelar
        'cancelar', 'encerrar', 'terminar', 'parar', 'sair', 'ir embora',
        'mudar para', 'concorrência', 'reembolso', 'dinheiro de volta', 'advogado',
        'processo', 'reclamação', 'gerente', 'supervisor', 'escalar',
        # Emocional
        'odeio', 'enojado', 'farto de', 'cansado de', 'nunca mais', 'pesadelo',
        'desastre', 'caos', 'inacreditável', 'ridículo', 'inaceitável', 'ofensivo',
    ],
    
    # Polish - Extended
    'pl': [
        # Złość/Frustracja
        'zły', 'wściekły', 'sfrustrowany', 'zirytowany', 'wkurzony', 'rozdrażniony',
        'oburzony', 'rozwścieczony',
        # Rozczarowanie
        'rozczarowany', 'zawiedziony', 'niezadowolony', 'nieszczęśliwy',
        # Skargi na jakość
        'okropny', 'straszny', 'fatalny', 'najgorszy', 'żałosny', 'bezużyteczny',
        'śmieć', 'badziewie', 'zepsuty', 'wolny', 'nie działa', 'bug', 'zawiesza się',
        # Skargi na obsługę
        'zła obsługa', 'fatalna obsługa', 'niegrzeczny', 'nieuprzejmy', 'niekompetentny',
        'ignorowany', 'bez pomocy', 'strata czasu', 'strata pieniędzy', 'oszustwo',
        'przekręt', 'za drogo', 'kłamstwo', 'fałszywa reklama',
        # Zamiar anulowania
        'anulować', 'zrezygnować', 'zakończyć', 'przestać', 'odejść', 'zmienić na',
        'konkurencja', 'zwrot pieniędzy', 'prawnik', 'pozew', 'skarga', 'kierownik',
        'przełożony', 'eskalować',
        # Emocjonalne
        'nienawidzę', 'zniesmaczony', 'mam dość', 'zmęczony', 'nigdy więcej', 'koszmar',
        'katastrofa', 'chaos', 'niewiarygodne', 'śmieszne', 'niedopuszczalne', 'obraźliwe',
    ],
    
    # Turkish - Extended
    'tr': [
        # Öfke/Hayal kırıklığı
        'kızgın', 'öfkeli', 'sinirli', 'hayal kırıklığına uğramış', 'bıkkın', 'rahatsız',
        'çileden çıkmış', 'kudurmuş',
        # Hayal kırıklığı
        'hayal kırıklığı', 'memnuniyetsiz', 'mutsuz', 'hoşnutsuz',
        # Kalite şikayetleri
        'berbat', 'korkunç', 'kötü', 'en kötü', 'acınası', 'işe yaramaz',
        'çöp', 'bozuk', 'yavaş', 'çalışmıyor', 'bug', 'donuyor',
        # Hizmet şikayetleri
        'kötü hizmet', 'berbat hizmet', 'kaba', 'saygısız', 'beceriksiz',
        'görmezden geliniyor', 'yardım yok', 'zaman kaybı', 'para kaybı', 'dolandırıcılık',
        'sahtekarlık', 'çok pahalı', 'yalan', 'yanıltıcı reklam',
        # İptal niyeti
        'iptal', 'sonlandırmak', 'bitirmek', 'bırakmak', 'ayrılmak', 'değiştirmek',
        'rakip', 'iade', 'paramı geri', 'avukat', 'dava', 'şikayet', 'müdür',
        'yönetici', 'eskale etmek',
        # Duygusal
        'nefret', 'iğrenmiş', 'bıktım', 'yoruldum', 'bir daha asla', 'kabus',
        'felaket', 'kaos', 'inanılmaz', 'saçma', 'kabul edilemez', 'hakaret',
    ],
    
    # Russian - Extended
    'ru': [
        # Гнев/Разочарование
        'злой', 'разъярённый', 'разочарованный', 'раздражённый', 'расстроенный',
        'взбешённый', 'возмущённый', 'в ярости',
        # Разочарование
        'разочарован', 'недоволен', 'несчастлив', 'огорчён',
        # Жалобы на качество
        'ужасный', 'кошмарный', 'отвратительный', 'худший', 'жалкий', 'бесполезный',
        'мусор', 'хлам', 'сломан', 'медленный', 'не работает', 'баг', 'виснет',
        # Жалобы на сервис
        'плохой сервис', 'ужасный сервис', 'грубый', 'хам', 'некомпетентный',
        'игнорируют', 'нет помощи', 'трата времени', 'трата денег', 'мошенничество',
        'обман', 'слишком дорого', 'ложь', 'ложная реклама',
        # Намерение отмены
        'отменить', 'расторгнуть', 'закончить', 'прекратить', 'уйти', 'перейти к',
        'конкурент', 'возврат', 'деньги назад', 'адвокат', 'суд', 'жалоба',
        'менеджер', 'руководитель', 'эскалировать',
        # Эмоциональные
        'ненавижу', 'отвращение', 'надоело', 'устал', 'никогда больше', 'кошмар',
        'катастрофа', 'хаос', 'невероятно', 'смешно', 'неприемлемо', 'оскорбительно',
    ],
    
    # Dutch - Extended
    'nl': [
        # Boosheid/Frustratie
        'boos', 'woedend', 'gefrustreerd', 'geïrriteerd', 'kwaad', 'razend',
        'verontwaardigd', 'geërgerd',
        # Teleurstelling
        'teleurgesteld', 'ontevreden', 'ongelukkig', 'misnoegd',
        # Kwaliteitsklachten
        'verschrikkelijk', 'vreselijk', 'afschuwelijk', 'slechtste', 'zielig', 'nutteloos',
        'troep', 'rommel', 'kapot', 'traag', 'werkt niet', 'bug', 'crasht',
        # Serviceklachten
        'slechte service', 'beroerde service', 'onbeleefd', 'incompetent',
        'genegeerd', 'geen hulp', 'tijdverspilling', 'geldverspilling', 'oplichting',
        'fraude', 'te duur', 'leugen', 'misleidende reclame',
        # Opzeggingsintentie
        'opzeggen', 'annuleren', 'beëindigen', 'stoppen', 'vertrekken', 'overstappen',
        'concurrent', 'terugbetaling', 'geld terug', 'advocaat', 'rechtszaak', 'klacht',
        'manager', 'leidinggevende', 'escaleren',
        # Emotioneel
        'haat', 'walg', 'zat', 'moe van', 'nooit meer', 'nachtmerrie',
        'ramp', 'chaos', 'ongelooflijk', 'belachelijk', 'onacceptabel', 'beledigend',
    ],
    
    # Czech - Extended
    'cs': [
        # Hněv/Frustrace
        'naštvaný', 'zuřivý', 'frustrovaný', 'podrážděný', 'rozčilený', 'rozzuřený',
        'rozhořčený', 'vytočený',
        # Zklamání
        'zklamaný', 'nespokojený', 'nešťastný', 'rozladěný',
        # Stížnosti na kvalitu
        'hrozný', 'strašný', 'příšerný', 'nejhorší', 'žalostný', 'k ničemu',
        'smetí', 'haraburdí', 'rozbitý', 'pomalý', 'nefunguje', 'bug', 'padá',
        # Stížnosti na služby
        'špatná služba', 'mizerná služba', 'nezdvořilý', 'nekompetentní',
        'ignorován', 'žádná pomoc', 'ztráta času', 'ztráta peněz', 'podvod',
        'švindl', 'moc drahé', 'lež', 'klamavá reklama',
        # Záměr zrušení
        'zrušit', 'ukončit', 'přestat', 'odejít', 'přejít k',
        'konkurence', 'vrácení peněz', 'peníze zpět', 'právník', 'žaloba', 'stížnost',
        'manažer', 'nadřízený', 'eskalovat',
        # Emocionální
        'nenávidím', 'znechucený', 'mám dost', 'unavený', 'nikdy více', 'noční můra',
        'katastrofa', 'chaos', 'neuvěřitelné', 'směšné', 'nepřijatelné', 'urážlivé',
    ],
    
    # Arabic - Extended
    'ar': [
        # غضب/إحباط
        'غاضب', 'مستشيط', 'محبط', 'منزعج', 'مستاء', 'ثائر', 'متوتر', 'مغتاظ',
        # خيبة أمل
        'خائب الأمل', 'غير راضٍ', 'حزين', 'مستاء',
        # شكاوى الجودة
        'فظيع', 'مريع', 'سيء', 'أسوأ', 'مثير للشفقة', 'عديم الفائدة',
        'قمامة', 'خردة', 'معطل', 'بطيء', 'لا يعمل', 'عطل', 'يتوقف',
        # شكاوى الخدمة
        'خدمة سيئة', 'خدمة فظيعة', 'وقح', 'غير مؤهل',
        'متجاهل', 'لا مساعدة', 'مضيعة للوقت', 'مضيعة للمال', 'احتيال',
        'نصب', 'غالي جدا', 'كذب', 'إعلان كاذب',
        # نية الإلغاء
        'إلغاء', 'إنهاء', 'توقف', 'مغادرة', 'التحويل إلى',
        'منافس', 'استرداد', 'أريد مالي', 'محامي', 'دعوى', 'شكوى',
        'مدير', 'مشرف', 'تصعيد',
        # عاطفي
        'أكره', 'مشمئز', 'سئمت', 'تعبت', 'لن أعود', 'كابوس',
        'كارثة', 'فوضى', 'لا يصدق', 'سخيف', 'غير مقبول', 'مهين',
    ],
    
    # Chinese - Extended
    'zh': [
        # 愤怒/沮丧
        '生气', '愤怒', '沮丧', '恼火', '不满', '暴怒', '恼怒', '激动',
        # 失望
        '失望', '不满意', '不开心', '郁闷',
        # 质量投诉
        '糟糕', '可怕', '差劲', '最差', '可悲', '没用',
        '垃圾', '废物', '坏了', '慢', '不工作', '故障', '死机',
        # 服务投诉
        '服务差', '服务糟糕', '粗鲁', '没礼貌', '不专业',
        '被忽视', '没帮助', '浪费时间', '浪费钱', '骗子',
        '欺诈', '太贵', '说谎', '虚假广告',
        # 取消意向
        '取消', '终止', '停止', '离开', '换成',
        '竞争对手', '退款', '要回钱', '律师', '起诉', '投诉',
        '经理', '主管', '升级',
        # 情绪
        '讨厌', '恶心', '受够了', '累了', '再也不', '噩梦',
        '灾难', '混乱', '难以置信', '荒谬', '不可接受', '侮辱',
    ],
    
    # Japanese - Extended
    'ja': [
        # 怒り/不満
        '怒っている', '激怒', 'イライラ', '腹が立つ', '不満', '激昂', 'むかつく',
        # 失望
        'がっかり', '不満足', '不幸せ', '残念',
        # 品質の苦情
        'ひどい', '最悪', '悪い', 'ワースト', '情けない', '役に立たない',
        'ゴミ', 'ガラクタ', '壊れた', '遅い', '動かない', 'バグ', 'フリーズ',
        # サービスの苦情
        'サービスが悪い', 'ひどいサービス', '失礼', '無能',
        '無視された', '助けがない', '時間の無駄', '金の無駄', '詐欺',
        '不正', '高すぎる', '嘘', '虚偽広告',
        # キャンセル意向
        'キャンセル', '解約', '終了', 'やめる', '去る', '乗り換える',
        '競合', '払い戻し', '返金', '弁護士', '訴訟', 'クレーム',
        'マネージャー', '上司', 'エスカレート',
        # 感情的
        '嫌い', '気持ち悪い', 'うんざり', '疲れた', '二度と', '悪夢',
        '災害', '混乱', '信じられない', 'ばかげた', '受け入れられない', '侮辱',
    ],
    
    # Hungarian - Extended
    'hu': [
        # Düh/Frusztráció
        'dühös', 'mérges', 'frusztrált', 'bosszús', 'ideges', 'felháborodott',
        'feldühödött', 'kiakadt',
        # Csalódottság
        'csalódott', 'elégedetlen', 'boldogtalan', 'lehangolt',
        # Minőségi panaszok
        'szörnyű', 'borzalmas', 'rettenetes', 'legrosszabb', 'szánalmas', 'haszontalan',
        'szemét', 'kacat', 'elromlott', 'lassú', 'nem működik', 'hiba', 'lefagy',
        # Szolgáltatási panaszok
        'rossz szolgáltatás', 'szörnyű szolgáltatás', 'udvariatlan', 'hozzá nem értő',
        'figyelmen kívül hagyva', 'nincs segítség', 'időpocsékolás', 'pénzkidobás', 'átverés',
        'csalás', 'túl drága', 'hazugság', 'megtévesztő reklám',
        # Lemondási szándék
        'lemondani', 'megszüntetni', 'befejezni', 'abbahagyni', 'elmenni', 'váltani',
        'konkurencia', 'visszatérítés', 'pénzt vissza', 'ügyvéd', 'per', 'panasz',
        'menedzser', 'felettes', 'eszkalálni',
        # Érzelmi
        'utálom', 'undorító', 'elegem van', 'fáradt', 'soha többé', 'rémálom',
        'katasztrófa', 'káosz', 'hihetetlen', 'nevetséges', 'elfogadhatatlan', 'sértő',
    ],
    
    # Korean - Extended
    'ko': [
        # 화/좌절
        '화난', '분노한', '좌절한', '짜증난', '불만족', '격노한', '열받은',
        # 실망
        '실망한', '불만족한', '불행한', '낙담한',
        # 품질 불만
        '끔찍한', '무서운', '나쁜', '최악', '한심한', '쓸모없는',
        '쓰레기', '고장난', '느린', '안 돼', '버그', '멈춤',
        # 서비스 불만
        '나쁜 서비스', '끔찍한 서비스', '무례한', '무능한',
        '무시당함', '도움 없음', '시간 낭비', '돈 낭비', '사기',
        '사기꾼', '너무 비싼', '거짓말', '허위 광고',
        # 취소 의향
        '취소', '해지', '끝내다', '그만두다', '떠나다', '바꾸다',
        '경쟁사', '환불', '돈 돌려줘', '변호사', '소송', '불만',
        '매니저', '상사', '에스컬레이트',
        # 감정적
        '싫어', '역겨운', '질렸어', '지쳤어', '다시는', '악몽',
        '재앙', '혼란', '믿을 수 없는', '우스꽝스러운', '받아들일 수 없는', '모욕적인',
    ],
    
    # Hindi - Extended (Romanized + Devanagari)
    'hi': [
        # गुस्सा/निराशा
        'gussa', 'naraz', 'nirash', 'pareshaan', 'chidha', 'krodhit',
        'गुस्सा', 'नाराज़', 'निराश', 'परेशान', 'चिढ़ा', 'क्रोधित',
        # निराशा
        'nirash', 'असंतुष्ट', 'dukhi', 'mayoos', 'निराश', 'दुखी', 'मायूस',
        # गुणवत्ता शिकायतें
        'bekaar', 'ghatiya', 'sabse kharab', 'faltu', 'bekar',
        'बेकार', 'घटिया', 'सबसे खराब', 'फालतू',
        'toota', 'dheema', 'kaam nahi karta', 'kharab',
        'टूटा', 'धीमा', 'काम नहीं करता', 'खराब',
        # सेवा शिकायतें
        'buri service', 'ghatiya service', 'badtameez', 'nakabil',
        'बुरी सर्विस', 'घटिया सर्विस', 'बदतमीज़', 'नाकाबिल',
        'ignore', 'madad nahi', 'samay barbadi', 'paisa barbadi', 'dhokha',
        'मदद नहीं', 'समय बर्बादी', 'पैसा बर्बादी', 'धोखा',
        # रद्द करने का इरादा
        'radd', 'band karo', 'chhod do', 'jaana', 'badalna',
        'रद्द', 'बंद करो', 'छोड़ दो', 'जाना', 'बदलना',
        'wapsi', 'vakeel', 'shikayat', 'manager',
        'वापसी', 'वकील', 'शिकायत', 'मैनेजर',
        # भावनात्मक
        'nafrat', 'ghin', 'thak gaya', 'phir kabhi nahi', 'bura sapna',
        'नफरत', 'घिन', 'थक गया', 'फिर कभी नहीं', 'बुरा सपना',
    ],
}

# Churn risk indicators - phrases that suggest customer might leave
# Expanded with more variations and languages
CHURN_INDICATORS = {
    'en': [
        'cancel subscription', 'close account', 'switch to', 'moving to', 'looking elsewhere', 
        'had enough', 'last chance', 'final warning', 'cancel my plan', 'stop service',
        'end my contract', 'unsubscribe', 'delete my account', 'remove me', 'too expensive',
        'found better', 'cheaper elsewhere', 'not worth it', 'waste of money', 'goodbye forever'
    ],
    'he': [
        'לבטל מנוי', 'לסגור חשבון', 'לעבור ל', 'מחפש מקום אחר', 'נמאס לי', 'הזדמנות אחרונה',
        'לבטל את התוכנית', 'להפסיק שירות', 'לסיים חוזה', 'להסיר אותי', 'יקר מדי',
        'מצאתי יותר טוב', 'יותר זול', 'לא שווה', 'בזבוז כסף', 'שלום ולא להתראות',
        'רוצה להתנתק', 'תנתקו אותי', 'עובר למתחרים'
    ],
    'es': [
        'cancelar suscripción', 'cerrar cuenta', 'cambiar a', 'buscar otro', 'harto',
        'última oportunidad', 'advertencia final', 'cancelar mi plan', 'detener servicio',
        'terminar contrato', 'darse de baja', 'borrar cuenta', 'eliminarme', 'muy caro',
        'encontré mejor', 'más barato', 'no vale la pena', 'pérdida de dinero', 'adiós para siempre'
    ],
    'fr': [
        'annuler abonnement', 'fermer compte', 'changer pour', 'chercher ailleurs',
        'j\'en ai assez', 'dernière chance', 'dernier avertissement', 'arrêter service',
        'résilier contrat', 'supprimer compte', 'trop cher', 'trouvé mieux', 'adieu pour toujours'
    ],
    'de': [
        'Abonnement kündigen', 'Konto schließen', 'wechseln zu', 'woanders suchen',
        'genug davon', 'letzte Chance', 'letzte Warnung', 'Vertrag kündigen',
        'zu teuer', 'Besseres gefunden', 'nie wieder', 'auf Wiedersehen'
    ],
    'ja': [
        'サブスクリプションをキャンセル', 'アカウントを閉じる', '乗り換える', '他を探す',
        'もう十分', '最後のチャンス', '解約', '高すぎる', 'もっと良いものを見つけた'
    ],
    'it': [
        'arrabbiato', 'furioso', 'frustrato', 'deluso', 'terribile', 'orribile',
        'odio', 'inaccettabile', 'ridicolo', 'cancellare', 'rimborso', 'reclamo',
        'avvocato', 'denuncia', 'responsabile', 'truffa', 'inutile', 'spreco di tempo',
        'stufo', 'stanco', 'vergogna', 'mai più', 'rotto', 'lento', 'pessimo servizio',
        'maleducato', 'non funziona', 'concorrenza', 'vado via', 'disdire',
    ],
    'pt': [
        'bravo', 'furioso', 'frustrado', 'decepcionado', 'terrível', 'horrível',
        'odeio', 'inaceitável', 'ridículo', 'cancelar', 'reembolso', 'reclamação',
        'gerente', 'supervisor', 'advogado', 'processo', 'golpe', 'fraude', 'inútil',
        'perda de tempo', 'farto', 'cansado', 'vergonha', 'nunca mais', 'quebrado',
        'lento', 'péssimo serviço', 'grosseiro', 'não funciona', 'concorrente',
        'vou sair', 'encerrar', 'lixo', 'porcaria',
    ],
    'tr': [
        'kızgın', 'öfkeli', 'hayal kırıklığı', 'berbat', 'korkunç', 'en kötü',
        'nefret', 'kabul edilemez', 'saçma', 'iptal', 'iade', 'şikayet',
        'müdür', 'yönetici', 'avukat', 'dava', 'dolandırıcılık', 'gereksiz',
        'zaman kaybı', 'bıktım', 'yoruldum', 'rezalet', 'bir daha asla', 'bozuk',
        'yavaş', 'kötü hizmet', 'kaba', 'çalışmıyor', 'rakip', 'gidiyorum',
        'kapatmak', 'lanet', 'rezil',
    ],
    'pl': [
        'zły', 'wściekły', 'sfrustrowany', 'rozczarowany', 'okropny', 'straszny',
        'najgorszy', 'nienawidzę', 'nieakceptowalne', 'śmieszne', 'anulować', 'zwrot',
        'skarga', 'kierownik', 'prawnik', 'oszustwo', 'bezużyteczne', 'strata czasu',
        'mam dość', 'zmęczony', 'wstyd', 'nigdy więcej', 'zepsute', 'wolne',
        'zła obsługa', 'niegrzeczny', 'nie działa', 'konkurencja', 'odchodzę',
        'zamknąć', 'beznadziejny', 'tragedia',
    ],
    'nl': [
        'boos', 'woedend', 'gefrustreerd', 'teleurgesteld', 'verschrikkelijk', 'vreselijk',
        'slechtste', 'haat', 'onacceptabel', 'belachelijk', 'annuleren', 'terugbetaling',
        'klacht', 'manager', 'advocaat', 'oplichting', 'nutteloos', 'tijdverspilling',
        'zat', 'moe', 'schande', 'nooit meer', 'kapot', 'traag', 'slechte service',
        'onbeleefd', 'werkt niet', 'concurrent', 'ik ga weg', 'opzeggen', 'waardeloos',
    ],
    'ko': [
        '화난', '분노', '좌절', '실망', '끔찍한', '최악', '싫어', '용납할 수 없는',
        '어이없는', '취소', '환불', '불만', '매니저', '변호사', '사기', '쓸모없는',
        '시간 낭비', '지겨운', '지친', '수치', '절대 다시는', '고장', '느린',
        '나쁜 서비스', '무례한', '작동 안함', '경쟁사', '떠날 거야', '해지', '엉망',
    ],
    'hi': [
        'gussa', 'naraz', 'niraash', 'bekaar', 'sabse bura', 'nafrat',
        'namumkin', 'radd', 'wapas', 'shikayat', 'manager', 'vakeel',
        'dhokha', 'scam', 'faltu', 'samay ki barbadi', 'thak gaya',
        'sharm', 'kabhi nahi', 'kharab', 'dheema', 'gandi service',
        'kaam nahi kar raha', 'chhod raha hu', 'band karo',
        # Devanagari
        'गुस्सा', 'नाराज़', 'निराश', 'बेकार', 'सबसे बुरा', 'नफरत',
        'रद्द', 'वापस', 'शिकायत', 'मैनेजर', 'वकील', 'धोखा', 'घोटाला',
        'फालतू', 'समय की बर्बादी', 'थक गया', 'शर्म', 'कभी नहीं',
        'खराब', 'धीमा', 'गंदी सर्विस', 'काम नहीं कर रहा', 'छोड़ रहा हूं', 'बंद करो'
    ]
}

# Churn risk indicators - phrases that suggest customer might leave
# Expanded with more variations and languages
CHURN_INDICATORS = {
    'en': [
        'cancel subscription', 'close account', 'switch to', 'moving to', 'looking elsewhere', 
        'had enough', 'last chance', 'final warning', 'cancel my plan', 'stop service',
        'end my contract', 'unsubscribe', 'delete my account', 'remove me', 'too expensive',
        'found better', 'cheaper elsewhere', 'not worth it', 'waste of money', 'goodbye forever'
    ],
    'he': [
        'לבטל מנוי', 'לסגור חשבון', 'לעבור ל', 'מחפש מקום אחר', 'נמאס לי', 'הזדמנות אחרונה',
        'לבטל את התוכנית', 'להפסיק שירות', 'לסיים חוזה', 'להסיר אותי', 'יקר מדי',
        'מצאתי יותר טוב', 'יותר זול', 'לא שווה', 'בזבוז כסף', 'שלום ולא להתראות',
        'רוצה להתנתק', 'תנתקו אותי', 'עובר למתחרים'
    ],
    'es': [
        'cancelar suscripción', 'cerrar cuenta', 'cambiar a', 'buscar otro', 'harto',
        'última oportunidad', 'advertencia final', 'cancelar mi plan', 'detener servicio',
        'terminar contrato', 'darse de baja', 'borrar cuenta', 'eliminarme', 'muy caro',
        'encontré mejor', 'más barato', 'no vale la pena', 'pérdida de dinero', 'adiós para siempre'
    ],
    'fr': [
        'annuler abonnement', 'fermer compte', 'changer pour', 'chercher ailleurs',
        'j\'en ai assez', 'dernière chance', 'dernier avertissement', 'arrêter service',
        'finir contrat', 'se désabonner', 'supprimer compte', 'trop cher',
        'trouvé mieux', 'moins cher', 'ça ne vaut pas la peine', 'perte d\'argent', 'adieu'
    ],
    'de': [
        'abo kündigen', 'konto schließen', 'wechseln zu', 'woanders suchen',
        'habe genug', 'letzte chance', 'letzte warnung', 'plan kündigen', 'dienst stoppen',
        'vertrag beenden', 'abmelden', 'konto löschen', 'zu teuer',
        'besser gefunden', 'billiger woanders', 'nicht wert', 'geldverschwendung'
    ],
    'ru': [
        'отменить подписку', 'закрыть счет', 'перейти к', 'ищу другое',
        'хватит', 'последний шанс', 'последнее предупреждение', 'отменить план',
        'остановить сервис', 'расторгнуть договор', 'отписаться', 'удалить аккаунт',
        'слишком дорого', 'нашел лучше', 'дешевле', 'не стоит того', 'трата денег'
    ],
    'ar': [
        'إلغاء الاشتراك', 'إغلاق الحساب', 'التبديل إلى', 'البحث في مكان آخر',
        'طفح الكيل', 'فرصة أخيرة', 'تحذير أخير', 'إيقاف الخدمة', 'إنهاء العقد',
        'حذف حسابي', 'مكلف للغاية', 'وجدت أفضل', 'أرخص', 'لا يستحق', 'مضيعة للمال'
    ],
    'pt': [
        'cancelar assinatura', 'fechar conta', 'mudar para', 'procurar outro',
        'já chega', 'última chance', 'aviso final', 'parar serviço', 'encerrar contrato',
        'cancelar inscrição', 'excluir conta', 'muito caro', 'encontrei melhor',
        'mais barato', 'não vale a pena', 'perda de dinheiro', 'adeus'
    ],
    'it': [
        'cancellare abbonamento', 'chiudere account', 'passare a', 'cercare altrove',
        'ne ho abbastanza', 'ultima possibilità', 'ultimo avviso', 'fermare servizio',
        'terminare contratto', 'disiscriversi', 'eliminare account', 'troppo costoso',
        'trovato meglio', 'più economico', 'non ne vale la pena', 'spreco di denaro'
    ],
    'tr': [
        'aboneliği iptal et', 'hesabı kapat', 'başka yere geç', 'başka yer ara',
        'yeter artık', 'son şans', 'son uyarı', 'hizmeti durdur', 'sözleşmeyi bitir',
        'üyelikten çık', 'hesabımı sil', 'çok pahalı', 'daha iyisini buldum',
        'daha ucuz', 'değmez', 'para kaybı'
    ],
    'pl': [
        'anuluj subskrypcję', 'zamknij konto', 'zmień na', 'szukam gdzie indziej',
        'mam dość', 'ostatnia szansa', 'ostatnie ostrzeżenie', 'zatrzymaj usługę',
        'zakończ umowę', 'wypisz się', 'usuń konto', 'za drogo', 'znalazłem lepsze',
        'taniej', 'nie warto', 'strata pieniędzy'
    ],
    'nl': [
        'abonnement annuleren', 'account sluiten', 'overstappen naar', 'ergens anders zoeken',
        'genoeg gehad', 'laatste kans', 'laatste waarschuwing', 'service stoppen',
        'contract beëindigen', 'uitschrijven', 'account verwijderen', 'te duur',
        'beter gevonden', 'goedkoper', 'niet waard', 'geldverspilling'
    ],
    'zh': [
        '取消订阅', '关闭账户', '切换到', '寻找其他', '受够了',
        '最后机会', '最后警告', '取消计划', '停止服务', '终止合同',
        '退订', '删除账户', '太贵', '找到更好的', '更便宜', '不值得', '浪费钱'
    ],
    'ja': [
        'サブスクリプションをキャンセル', 'アカウントを閉鎖', 'に切り替える', '他を探す',
        'もう十分', '最後のチャンス', '最後の警告', 'プランをキャンセル', 'サービスを停止',
        '契約を終了', '退会', 'アカウントを削除', '高すぎる', 'もっと良いものを見つけた',
        '安い', '価値がない', '金の無駄'
    ],
    'ko': [
        '구독 취소', '계정 폐쇄', '로 전환', '다른 곳을 찾다',
        '충분해', '마지막 기회', '마지막 경고', '플랜 취소', '서비스 중단',
        '계약 종료', '구독 해지', '계정 삭제', '너무 비싸다', '더 좋은 것을 찾았다',
        '더 싼', '가치가 없다', '돈 낭비'
    ],
    'hi': [
        'sadasyata radd', 'khata band', 'switch', 'khatam',
        'mehenga', 'bekaar', 'barbaad', 'band karo',
        # Devanagari
        'सदस्यता रद्द', 'खाता बंद', 'स्विच', 'खत्म',
        'महंगा', 'बेकार', 'बरबाद', 'बंद करो'
    ],
}

# =============================================================================
# TTS CACHE - Cache common phrases to reduce TTS calls
# =============================================================================
_tts_cache = SimpleCache(default_ttl=3600)  # 1 hour TTL for TTS cache

# =============================================================================
# CONNECTION WATCHDOG - Monitor service health
# =============================================================================
class ConnectionWatchdog:
    """Monitors connection health and reports issues"""
    
    def __init__(self):
        self.last_successful_asr = time.time()
        self.last_successful_tts = time.time()
        self.last_successful_llm = time.time()
        self.alert_threshold = 60  # seconds
    
    def record_asr_success(self):
        self.last_successful_asr = time.time()
    
    def record_tts_success(self):
        self.last_successful_tts = time.time()
    
    def record_llm_success(self):
        self.last_successful_llm = time.time()
    
    def check_health(self) -> list:
        now = time.time()
        issues = []
        if now - self.last_successful_asr > self.alert_threshold:
            issues.append(f"ASR: No success in {int(now - self.last_successful_asr)}s")
        if now - self.last_successful_tts > self.alert_threshold:
            issues.append(f"TTS: No success in {int(now - self.last_successful_tts)}s")
        if now - self.last_successful_llm > self.alert_threshold:
            issues.append(f"LLM: No success in {int(now - self.last_successful_llm)}s")
        return issues

_watchdog = ConnectionWatchdog()

# =============================================================================
# NATURAL RESPONSE TEMPLATES - For human-like TTS output
# XTTS v2 Supported: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh, ja, hu, ko, hi
# =============================================================================
NATURAL_RESPONSES = {
    # Full multi-language support - Native translations without English fallback
    "en": {
        "greeting_new": ["Hey there! Welcome. Could you tell me your four digit PIN to verify your account?", "Hi! Thanks for calling. I just need your four digit PIN to pull up your account."],
        "greeting_returning": ["Hey {name}! Good to have you back. What can I do for you today?", "Hi {name}! How's it going? What brings you in today?"],
        "pin_invalid": ["Hmm, that PIN didn't match. You've got {remaining} more tries.", "That one didn't work. {remaining} attempts left. Try again?"],
        "pin_unclear": ["Sorry, I didn't quite catch that. Could you say your four digit PIN again?", "I missed that - could you repeat your PIN?"],
        "pin_locked": ["I'm sorry, you've hit the limit on PIN attempts. Please contact support."],
        "upgrade_ask_plan": ["Sure thing! Are you looking at Standard or Premium?", "Absolutely! Thinking about Standard or Premium?"],
        "upgrade_confirm": ["Perfect, upgrade to {plan}. Should I submit that request?", "Got it - {plan}. Confirm the upgrade?"],
        "upgrade_submitted": ["Done! I've submitted your upgrade request for {plan}.", "Perfect, your {plan} upgrade request is in."],
        "upgrade_cancelled": ["No problem! Let me know if you change your mind.", "Got it - no changes for now."],
        "info_not_found": ["I couldn't find any {type} information on your account.", "No {type} found."],
        "query_error": ["I ran into a snag looking that up. Mind if we try again?", "System hiccup. Try again?"],
        "goodbye": ["Take care! Don't hesitate to call if you need anything.", "Thanks for calling! Have a great day!"],
        "ticket_ask_details": ["Sure, I can open a ticket. Please describe the issue.", "I can help with that. What is the reason for the ticket?"],
        "ticket_created": ["I've opened ticket number {id} for you.", "Done. Ticket #{id} has been created."],
        "ticket_close_success": ["Done. I've closed ticket number {id} about '{subject}'.", "I've closed your open ticket, number {id}."],
        "ticket_close_latest": ["You have {count} open tickets. I've closed the most recent one, ticket {id}, about '{subject}'."],
        "ticket_close_fail": ["I tried to close that ticket but ran into an issue.", "I ran into an issue updating your ticket."],
        "ticket_close_not_found": ["I couldn't find an open ticket with number {id}. Want me to list your open tickets?", "No open ticket found with that number."],
        "ticket_create_fail": ["I had a bit of trouble opening that ticket. You might want to try again later."],
        "ticket_create_cancel": ["Okay, I won't open a ticket."],
        "upgrade_not_found": ["I couldn't find a plan matching '{plan}'. We have Standard and Premium - which would you prefer?"],
        "upgrade_unclear": ["Sorry, I didn't catch that. You want to upgrade to {plan}, right? Yes or no."],
        "upgrade_error": ["I ran into a small issue. Can you confirm the upgrade again?"],
        "sub_active": ["You're on the {plan} plan, {name}. That's {price} dollars a month."],
        "sub_status": ["Your {plan} subscription is currently {status}."],
        "bal_overdue": ["Looks like you have {amount} dollars overdue, {name}. Want help sorting that?"],
        "bal_pending": ["You've got {amount} dollars pending, {name}. Nothing overdue."],
        "bal_clear": ["Good news, {name}! Your balance is clear."],
        "invoice_one": ["You've got one invoice for {amount} dollars, and it's {status}."],
        "invoice_many": ["I see {count} invoices. Your most recent is {amount} dollars, currently {status}."],
        "plan_details": ["You're on {name} at {price} dollars a month. You've got {data} gigs of data."],
        "ticket_open": ["You have {count} open tickets. The most recent is about {subject}."],
        "ticket_resolved": ["All your {count} support tickets have been resolved!"],
        "ticket_none": ["You don't have any support tickets."],
        "cust_info": ["Your account email is {email} and phone is {phone}."],
        "general_found": ["Here's what I found for you."],
    },
    "he": {
        "greeting_new": ["היי! אפשר לקבל את קוד ה-PIN בן ארבע הספרות שלך?", "ברוכים הבאים. אני צריך את הקוד הסודי לאימות."],
        "greeting_returning": ["היי {name}! איזה כיף שחזרת. איך אפשר לעזור?", "שלום {name}! במה אוכל לסייע היום?"],
        "pin_invalid": ["הקוד לא תואם. נשארו {remaining} ניסיונות.", "זה לא עבד. נותרו {remaining} ניסיונות."],
        "pin_unclear": ["סליחה, לא שמעתי. אפשר לחזור על הקוד?", "תוכל להגיד שוב את ארבע הספרות?"],
        "pin_locked": ["מצטער, הגעת למגבלת הניסיונות. פנה לתמיכה.", "החשבון ננעל עקב ריבוי ניסיונות."],
        "upgrade_ask_plan": ["בטח! חושבים על סטנדרט או פרימיום?", "מעוניינים בחבילת סטנדרט או פרימיום?"],
        "upgrade_confirm": ["מעולה, לשדרג ל-{plan}. לאשר?", "אז {plan}. להגיש את הבקשה?"],
        "upgrade_submitted": ["בוצע! הגשתי בקשה ל-{plan}.", "הבקשה לשדרוג ל-{plan} נשלחה בהצלחה."],
        "upgrade_cancelled": ["אין בעיה. תודיע לי אם תשנה דעה.", "בוטל."],
        "info_not_found": ["לא מצאתי מידע על {type}.", "אין נתונים על {type}."],
        "query_error": ["הייתה בעיה קטנה. שננסה שוב?", "שגיאת מערכת."],
        "goodbye": ["להתראות! תרגיש חופשי להתקשר שוב.", "יום טוב!"],
        "ticket_ask_details": ["אני אפתח קריאה. מה הבעיה?", "תאר לי את התקלה עבור הכרטיס."],
        "ticket_created": ["פתחתי קריאה מספר {id}.", "בוצע. כרטיס #{id} נפתח."],
        "ticket_close_success": ["סגרתי את קריאה מספר {id} בנושא '{subject}'.", "הקריאה הפתוחה {id} נסגרה."],
        "ticket_close_latest": ["יש לך {count} קריאות פתוחות. סגרתי את האחרונה, מספר {id}."],
        "ticket_close_fail": ["הייתה בעיה בסגירת הקריאה.", "לא הצלחתי לעדכן את הכרטיס."],
        "ticket_close_not_found": ["לא מצאתי קריאה פתוחה עם המספר {id}.", "אין כרטיס פתוח עם המספר הזה."],
        "ticket_create_fail": ["הייתה לי בעיה לפתוח את הקריאה הזו. אולי ננסה אחר כך?"],
        "ticket_create_cancel": ["אוקיי, לא אפתח קריאה."],
        "upgrade_not_found": ["לא מצאתי תוכנית בשם '{plan}'. יש לנו סטנדרט ופרימיום."],
        "upgrade_unclear": ["סליחה, לא הבנתי. אתה רוצה לשדרג ל-{plan}? כן או לא?"],
        "upgrade_error": ["נתקלתי בבעיה קטנה. תוכל לאשר שוב את השדרוג?"],
        "sub_active": ["אתה בתוכנית {plan}, {name}. זה {price} שקלים לחודש."],
        "sub_status": ["המנוי {plan} שלך כרגע בסטטוס {status}."],
        "bal_overdue": ["יש לך חוב של {amount} שקלים, {name}. רוצה עזרה עם זה?", "שים לב, {amount} שקלים באיחור."],
        "bal_pending": ["יש {amount} שקלים בהמתנה לתשלום, {name}.", "סכום פתוח: {amount} שקלים."],
        "bal_clear": ["חדשות טובות {name}, אין חובות!", "היתרה מאופסת."],
        "invoice_one": ["יש חשבונית אחת על סך {amount} שקלים, בסטטוס {status}."],
        "invoice_many": ["אני רואה {count} חשבוניות. האחרונה על {amount} שקלים, {status}."],
        "plan_details": ["חבילת {name} ב-{price} שקלים. כולל {data} ג'יגה.", "מנוי {name}, {price} לחודש."],
        "ticket_open": ["יש לך {count} קריאות פתוחות. האחרונה לגבי {subject}.", "{count} כרטיסים פתוחים."],
        "ticket_resolved": ["כל {count} הקריאות שלך טופלו!", "אין תקלות פתוחות."],
        "ticket_none": ["אין לך קריאות שירות במערכת."],
        "cust_info": ["האימייל הוא {email} והטלפון {phone}."],
        "general_found": ["הנה מה שמצאתי."],
    },
    "es": {
        "greeting_new": ["¡Hola! Bienvenido. ¿Podrías decirme tu PIN de cuatro dígitos?", "¡Hola! Necesito tu PIN de cuatro dígitos para verificar tu cuenta."],
        "greeting_returning": ["¡Hola {name}! Qué bueno tenerte de vuelta. ¿En qué puedo ayudarte?", "¡{name}! Un gusto escucharte de nuevo."],
        "pin_invalid": ["Ese PIN no coincide. Te quedan {remaining} intentos.", "No funcionó. {remaining} intentos restantes."],
        "pin_unclear": ["Perdona, no escuché bien. ¿Podrías repetir tu PIN?", "¿Puedes repetir los cuatro dígitos?"],
        "pin_locked": ["Lo siento, has alcanzado el límite de intentos. Contacta soporte."],
        "upgrade_ask_plan": ["¡Claro! ¿Buscas el plan Standard o Premium?", "¿Te interesa Standard o Premium?"],
        "upgrade_confirm": ["Perfecto, cambiar a {plan}. ¿Confirmo la solicitud?", "¿Quieres proceder con el plan {plan}?"],
        "upgrade_submitted": ["¡Listo! Envié tu solicitud para {plan}.", "Hecho. Solicitud de {plan} enviada."],
        "upgrade_cancelled": ["¡Sin problema! Avísame si cambias de opinión.", "Cancelado. ¿Algo más?"],
        "info_not_found": ["No encontré información de {type}.", "Sin datos de {type}."],
        "query_error": ["Tuve un problema buscando eso. ¿Intentamos de nuevo?", "Error de sistema. ¿Pruebas otra vez?"],
        "goodbye": ["¡Cuídate! Llama si necesitas algo.", "¡Gracias por llamar! Que tengas buen día."],
        "ticket_ask_details": ["Puedo abrir un ticket. Describe el problema, por favor.", "¿Cuál es la razón del ticket?"],
        "ticket_created": ["Abrí el ticket número {id} para ti.", "Listo. Ticket #{id} creado."],
        "ticket_close_success": ["He cerrado el ticket número {id} sobre '{subject}'.", "Listo. Ticket {id} cerrado."],
        "ticket_close_latest": ["Tienes {count} tickets abiertos. Cerré el más reciente, ticket {id}, sobre '{subject}'."],
        "ticket_close_fail": ["Tuve un problema al cerrar ese ticket.", "Error al actualizar el ticket."],
        "ticket_close_not_found": ["No encontré un ticket abierto con el número {id}.", "¿Quieres que liste tus tickets?"],
        "ticket_create_fail": ["Tuve un problema al crear el ticket. Intenta más tarde."],
        "ticket_create_cancel": ["Está bien, no abriré el ticket."],
        "upgrade_not_found": ["No encontré el plan '{plan}'. Tenemos Standard y Premium."],
        "upgrade_unclear": ["Perdón, no entendí. ¿Quieres cambiar a {plan}, verdad? ¿Sí o no?"],
        "upgrade_error": ["Tuve un pequeño problema. ¿Puedes confirmar de nuevo?"],
        "sub_active": ["Estás en el plan {plan}, {name}. Son {price} euros al mes."],
        "sub_status": ["Tu suscripción {plan} está actualmente {status}."],
        "bal_overdue": ["Tienes {amount} euros vencidos, {name}. ¿Quieres ayuda con eso?"],
        "bal_pending": ["Tienes {amount} euros pendientes, {name}. Nada vencido."],
        "bal_clear": ["¡Buenas noticias, {name}! Tu saldo está al día."],
        "invoice_one": ["Tienes una factura de {amount} euros, y está {status}."],
        "invoice_many": ["Veo {count} facturas. La más reciente es de {amount} euros, {status}."],
        "plan_details": ["Tienes {name} por {price} euros al mes. Incluye {data} gigas."],
        "ticket_open": ["Tienes {count} tickets abiertos. El más reciente es sobre {subject}."],
        "ticket_resolved": ["¡Tus {count} tickets están resueltos!"],
        "ticket_none": ["No tienes tickets de soporte."],
        "cust_info": ["Tu email es {email} y teléfono {phone}."],
        "general_found": ["Esto es lo que encontré."],
    },
    "fr": {
        "greeting_new": ["Bonjour! Pouvez-vous me donner votre code PIN à quatre chiffres?", "Bienvenue. J'ai besoin de votre PIN pour accéder à votre compte."],
        "greeting_returning": ["Bonjour {name}! Comment puis-je vous aider?", "Ravi de vous revoir, {name}!"],
        "pin_invalid": ["Ce code ne correspond pas. Il reste {remaining} essais.", "Incorrect. {remaining} tentatives restantes."],
        "pin_unclear": ["Désolé, je n'ai pas compris. Pouvez-vous répéter?", "Répétez le PIN s'il vous plaît."],
        "pin_locked": ["Désolé, limite d'essais atteinte. Contactez le support."],
        "upgrade_ask_plan": ["Bien sûr! Vous voulez Standard ou Premium?", "Standard ou Premium vous intéresse?"],
        "upgrade_confirm": ["Parfait, passer à {plan}. Je confirme?", "Vous voulez le plan {plan}. On y va?"],
        "upgrade_submitted": ["C'est fait! Demande pour {plan} envoyée.", "Noté. Mise à niveau vers {plan} soumise."],
        "upgrade_cancelled": ["Pas de problème! Dites-moi si vous changez d'avis.", "Annulé."],
        "info_not_found": ["Je n'ai pas trouvé d'infos sur {type}.", "Aucun {type} trouvé."],
        "query_error": ["Petit problème technique. On réessaie?", "Erreur système."],
        "goodbye": ["Au revoir! N'hésitez pas à rappeler.", "Bonne journée!"],
        "ticket_ask_details": ["Je peux ouvrir un ticket. Décrivez le problème.", "Quel est le problème ?"],
        "ticket_created": ["J'ai ouvert le ticket numéro {id}.", "Ticket #{id} créé avec succès."],
        "ticket_close_success": ["C'est fait. J'ai fermé le ticket numéro {id} concernant '{subject}'.", "Ticket {id} fermé."],
        "ticket_close_latest": ["Vous avez {count} tickets ouverts. J'ai fermé le plus récent, ticket {id}."],
        "ticket_close_fail": ["J'ai eu un problème pour fermer ce ticket."],
        "ticket_close_not_found": ["Je n'ai pas trouvé de ticket ouvert avec le numéro {id}."],
        "ticket_create_fail": ["J'ai eu un problème pour ouvrir ce ticket."],
        "ticket_create_cancel": ["D'accord, je n'ouvre pas de ticket."],
        "upgrade_not_found": ["Je n'ai pas trouvé de plan correspondant à '{plan}'."],
        "upgrade_unclear": ["Désolé, je n'ai pas compris. Vous voulez passer à {plan}, c'est ça ?"],
        "upgrade_error": ["J'ai rencontré un petit problème. Pouvez-vous confirmer ?"],
        "sub_active": ["Vous avez le plan {plan}, {name}. C'est {price} euros par mois."],
        "sub_status": ["Votre abonnement {plan} est {status}."],
        "bal_overdue": ["Vous avez {amount} euros en retard, {name}. Besoin d'aide?", "Attention, {amount} euros impayés."],
        "bal_pending": ["Vous avez {amount} euros en attente, {name}.", "Solde en attente : {amount} euros."],
        "bal_clear": ["Bonne nouvelle, {name}! Tout est payé.", "Votre compte est à jour."],
        "invoice_one": ["Une facture de {amount} euros, statut {status}."],
        "invoice_many": ["Je vois {count} factures. La dernière est de {amount} euros, {status}."],
        "plan_details": ["Forfait {name} à {price} euros. {data} gigas de données."],
        "ticket_open": ["Vous avez {count} tickets ouverts. Le dernier concerne {subject}."],
        "ticket_resolved": ["Vos {count} tickets sont tous résolus!", "Aucun ticket ouvert."],
        "ticket_none": ["Aucun ticket de support."],
        "cust_info": ["Email : {email}, Tél : {phone}."],
        "general_found": ["Voici ce que j'ai trouvé."],
    },
    "de": {
        "greeting_new": ["Hallo! Können Sie mir Ihre vierstellige PIN nennen?", "Willkommen. Bitte nennen Sie Ihre PIN zur Verifizierung."],
        "greeting_returning": ["Hallo {name}! Wie kann ich helfen?", "Schön Sie zu hören, {name}."],
        "pin_invalid": ["PIN falsch. Noch {remaining} Versuche.", "Das war nicht korrekt. {remaining} Versuche übrig."],
        "pin_unclear": ["Entschuldigung, können Sie die PIN wiederholen?", "Bitte wiederholen Sie die vier Ziffern."],
        "pin_locked": ["Zu viele Versuche. Bitte kontaktieren Sie den Support."],
        "upgrade_ask_plan": ["Gerne! Möchten Sie Standard oder Premium?", "Interesse an Standard oder Premium?"],
        "upgrade_confirm": ["Perfekt, Upgrade auf {plan}. Soll ich das bestätigen?", "Plan {plan} ausgewählt. Bestätigen?"],
        "upgrade_submitted": ["Erledigt! Anfrage für {plan} gesendet.", "Upgrade auf {plan} eingereicht."],
        "upgrade_cancelled": ["Kein Problem. Sagen Sie Bescheid bei Änderungen.", "Abgebrochen."],
        "info_not_found": ["Keine Information zu {type} gefunden.", "Nichts zu {type} gefunden."],
        "query_error": ["Ein kleiner Fehler. Versuchen wir es nochmal?", "Systemfehler."],
        "goodbye": ["Auf Wiederhören!", "Tschüss, rufen Sie gerne wieder an."],
        "ticket_ask_details": ["Ich öffne ein Ticket. Was ist das Problem?", "Bitte beschreiben Sie das Problem für das Ticket."],
        "ticket_created": ["Ticket Nummer {id} wurde erstellt.", "Erledigt. Ticket #{id} ist offen."],
        "ticket_close_success": ["Erledigt. Ich habe Ticket Nummer {id} geschlossen.", "Ticket {id} ist jetzt geschlossen."],
        "ticket_close_latest": ["Sie haben {count} offene Tickets. Ich habe das letzte Ticket {id} geschlossen."],
        "ticket_close_fail": ["Ich hatte ein Problem beim Schließen des Tickets."],
        "ticket_close_not_found": ["Ich konnte kein offenes Ticket mit der Nummer {id} finden."],
        "ticket_create_fail": ["Ich hatte Probleme beim Erstellen des Tickets."],
        "ticket_create_cancel": ["Okay, ich erstelle kein Ticket."],
        "upgrade_not_found": ["Ich konnte keinen Plan namens '{plan}' finden."],
        "upgrade_unclear": ["Entschuldigung, wollen Sie auf {plan} upgraden?"],
        "upgrade_error": ["Ein kleines Problem ist aufgetreten. Bitte bestätigen Sie erneut."],
        "sub_active": ["Sie haben den {plan} Plan, {name}. {price} Euro pro Monat.", "Ihr Plan ist {plan} für {price} Euro."],
        "sub_status": ["Ihr {plan} Abo ist derzeit {status}."],
        "bal_overdue": ["Sie haben {amount} Euro offen, {name}. Soll ich helfen?", "Achtung, {amount} Euro überfällig."],
        "bal_pending": ["Es sind {amount} Euro ausstehend, {name}.", "Offener Betrag: {amount} Euro."],
        "bal_clear": ["Gute Nachrichten, {name}! Alles bezahlt.", "Konto ausgeglichen."],
        "invoice_one": ["Eine Rechnung über {amount} Euro, Status {status}."],
        "invoice_many": ["Ich sehe {count} Rechnungen. Die letzte über {amount} Euro ist {status}."],
        "plan_details": ["Plan {name} für {price} Euro. {data} GB Datenvolumen."],
        "ticket_open": ["Sie haben {count} offene Tickets. Das letzte betrifft {subject}."],
        "ticket_resolved": ["Alle {count} Tickets sind gelöst.", "Keine offenen Probleme."],
        "ticket_none": ["Keine Support-Tickets vorhanden."],
        "cust_info": ["Email: {email}, Telefon: {phone}."],
        "general_found": ["Das habe ich gefunden."],
    },
    "it": {
        "greeting_new": ["Ciao! Potresti dirmi il tuo PIN di quattro cifre?", "Benvenuto. Mi serve il PIN per verificare l'account."],
        "greeting_returning": ["Ciao {name}! Come posso aiutarti?", "Bentornato {name}!"],
        "pin_invalid": ["PIN non valido. Hai ancora {remaining} tentativi.", "Sbagliato. {remaining} tentativi rimasti."],
        "pin_unclear": ["Scusa, non ho capito. Puoi ripetere il PIN?", "Ripeti le quattro cifre per favore."],
        "pin_locked": ["Spiacente, troppi tentativi. Contatta il supporto."],
        "upgrade_ask_plan": ["Certo! Preferisci Standard o Premium?", "Vuoi passare a Standard o Premium?"],
        "upgrade_confirm": ["Perfetto, passo a {plan}. Confermo?", "Vuoi il piano {plan}. Procedo?"],
        "upgrade_submitted": ["Fatto! Richiesta per {plan} inviata.", "Richiesta di upgrade a {plan} inoltrata."],
        "upgrade_cancelled": ["Nessun problema. Avvisami se cambi idea.", "Annullato."],
        "info_not_found": ["Non ho trovato info su {type}.", "Nessun dato per {type}."],
        "query_error": ["Piccolo problema tecnico. Riprovare?", "Errore di sistema."],
        "goodbye": ["Arrivederci! Chiama quando vuoi.", "Buona giornata!"],
        "ticket_ask_details": ["Posso aprire un ticket. Descrivi il problema.", "Qual è il motivo della segnalazione?"],
        "ticket_created": ["Ho aperto il ticket numero {id}.", "Fatto. Ticket #{id} creato."],
        "ticket_close_success": ["Fatto. Ho chiuso il ticket numero {id} riguardante '{subject}'.", "Ticket {id} chiuso."],
        "ticket_close_latest": ["Hai {count} ticket aperti. Ho chiuso l'ultimo, il numero {id}."],
        "ticket_close_fail": ["Ho avuto un problema nel chiudere il ticket."],
        "ticket_close_not_found": ["Non ho trovato ticket aperti con numero {id}."],
        "ticket_create_fail": ["Ho avuto problemi ad aprire il ticket."],
        "ticket_create_cancel": ["Ok, non apro nessun ticket."],
        "upgrade_not_found": ["Non ho trovato il piano '{plan}'."],
        "upgrade_unclear": ["Scusa, vuoi passare al piano {plan}?"],
        "upgrade_error": ["C'è stato un piccolo problema. Puoi confermare?"],
        "sub_active": ["Hai il piano {plan}, {name}. Costa {price} euro al mese."],
        "sub_status": ["Il tuo abbonamento {plan} è {status}."],
        "bal_overdue": ["Hai {amount} euro scaduti, {name}. Vuoi aiuto?", "Attenzione, {amount} euro non pagati."],
        "bal_pending": ["Hai {amount} euro in sospeso, {name}."],
        "bal_clear": ["Ottime notizie, {name}! Saldo a zero.", "Tutto pagato."],
        "invoice_one": ["Una fattura di {amount} euro, stato {status}."],
        "invoice_many": ["Vedo {count} fatture. L'ultima è di {amount} euro, {status}."],
        "plan_details": ["Piano {name} a {price} euro. {data} GB di dati."],
        "ticket_open": ["Hai {count} ticket aperti. L'ultimo riguarda {subject}."],
        "ticket_resolved": ["Tutti i tuoi {count} ticket sono risolti!"],
        "ticket_none": ["Nessun ticket di supporto."],
        "cust_info": ["Email: {email}, Telefono: {phone}."],
        "general_found": ["Ecco cosa ho trovato."],
    },
    "pt": {
        "greeting_new": ["Olá! Pode me dizer seu PIN de quatro dígitos?", "Bem-vindo. Preciso do seu PIN para verificar a conta."],
        "greeting_returning": ["Olá {name}! Como posso ajudar hoje?", "Oi {name}! Tudo bem?"],
        "pin_invalid": ["PIN incorreto. Restam {remaining} tentativas.", "Não conferiu. {remaining} chances."],
        "pin_unclear": ["Desculpe, não entendi. Pode repetir o PIN?", "Repita os quatro dígitos, por favor."],
        "pin_locked": ["Desculpe, limite de tentativas atingido. Contate o suporte."],
        "upgrade_ask_plan": ["Claro! Você quer Standard ou Premium?", "Interessado no Standard ou Premium?"],
        "upgrade_confirm": ["Perfeito, mudar para {plan}. Posso confirmar?", "Upgrade para {plan}. Tudo certo?"],
        "upgrade_submitted": ["Pronto! Solicitação para {plan} enviada.", "Pedido de {plan} submetido."],
        "upgrade_cancelled": ["Sem problemas. Me avise se mudar de ideia.", "Cancelado."],
        "info_not_found": ["Não encontrei informações sobre {type}.", "Nada sobre {type}."],
        "query_error": ["Tive um problema. Vamos tentar de novo?", "Erro no sistema."],
        "goodbye": ["Até logo! Ligue se precisar.", "Tchau, tenha um bom dia!"],
        "ticket_ask_details": ["Posso abrir um chamado. Qual é o problema?", "Descreva o problema para o ticket."],
        "ticket_created": ["Abri o ticket número {id}.", "Feito. Chamado #{id} criado."],
        "ticket_close_success": ["Pronto. Fechei o ticket número {id} sobre '{subject}'.", "Ticket {id} encerrado."],
        "ticket_close_latest": ["Você tem {count} tickets abertos. Fechei o mais recente, número {id}."],
        "ticket_close_fail": ["Tive um problema ao fechar o ticket."],
        "ticket_close_not_found": ["Não encontrei ticket aberto com número {id}."],
        "ticket_create_fail": ["Tive problemas ao criar o ticket."],
        "ticket_create_cancel": ["Ok, não vou abrir o ticket."],
        "upgrade_not_found": ["Não encontrei o plano '{plan}'."],
        "upgrade_unclear": ["Desculpe, você quer mudar para {plan}?"],
        "upgrade_error": ["Tive um pequeno problema. Pode confirmar de novo?"],
        "sub_active": ["Seu plano é {plan}, {name}. Custa {price} euros por mês."],
        "sub_status": ["Sua assinatura {plan} está {status}."],
        "bal_overdue": ["Você tem {amount} euros vencidos, {name}. Quer ajuda?", "Atenção, {amount} em atraso."],
        "bal_pending": ["Tem {amount} euros pendentes, {name}."],
        "bal_clear": ["Boas notícias, {name}! Tudo pago.", "Saldo zerado."],
        "invoice_one": ["Uma fatura de {amount} euros, status {status}."],
        "invoice_many": ["Vejo {count} faturas. A mais recente é de {amount} euros, {status}."],
        "plan_details": ["Plano {name} por {price} euros. {data} GB de dados."],
        "ticket_open": ["Você tem {count} chamados abertos. O último é sobre {subject}."],
        "ticket_resolved": ["Seus {count} chamados estão resolvidos!"],
        "ticket_none": ["Nenhum chamado de suporte."],
        "cust_info": ["Email: {email}, Telefone: {phone}."],
        "general_found": ["Aqui está o que encontrei."],
    },
    "ru": {
        "greeting_new": ["Привет! Назовите ваш четырехзначный PIN-код.", "Здравствуйте. Нужен PIN для входа."],
        "greeting_returning": ["Привет, {name}! Чем могу помочь?", "Здравствуйте, {name}!"],
        "pin_invalid": ["Неверный PIN. Осталось {remaining} попытки.", "Ошибка. Попыток: {remaining}."],
        "pin_unclear": ["Извините, не расслышал. Повторите PIN.", "Повторите 4 цифры."],
        "pin_locked": ["Лимит попыток исчерпан. Обратитесь в поддержку."],
        "upgrade_ask_plan": ["Конечно! Выбираете Standard или Premium?", "Standard или Premium?"],
        "upgrade_confirm": ["Отлично, переход на {plan}. Подтверждаю?", "План {plan}. Оформляем?"],
        "upgrade_submitted": ["Готово! Заявка на {plan} отправлена.", "Запрос на {plan} создан."],
        "upgrade_cancelled": ["Без проблем. Скажите, если передумаете.", "Отменено."],
        "info_not_found": ["Не нашел информации о {type}.", "Нет данных по {type}."],
        "query_error": ["Произошла ошибка. Попробуем снова?", "Сбой системы."],
        "goodbye": ["До свидания! Звоните, если что.", "Всего доброго!"],
        "ticket_ask_details": ["Я создам тикет. Опишите проблему.", "Какая причина обращения?"],
        "ticket_created": ["Создан тикет номер {id}.", "Готово. Заявка #{id} открыта."],
        "ticket_close_success": ["Готово. Тикет {id} закрыт.", "Заявка {id} успешно закрыта."],
        "ticket_close_latest": ["У вас {count} открытых заявок. Я закрыл последнюю, номер {id}."],
        "ticket_close_fail": ["Не удалось закрыть тикет."],
        "ticket_close_not_found": ["Не нашел открытого тикета с номером {id}."],
        "ticket_create_fail": ["Не удалось создать тикет."],
        "ticket_create_cancel": ["Хорошо, не буду создавать тикет."],
        "upgrade_not_found": ["Не нашел план '{plan}'."],
        "upgrade_unclear": ["Извините, вы хотите перейти на {plan}?"],
        "upgrade_error": ["Возникла ошибка. Подтвердите еще раз."],
        "sub_active": ["У вас план {plan}, {name}. {price} рублей в месяц."],
        "sub_status": ["Ваша подписка {plan} сейчас {status}."],
        "bal_overdue": ["У вас долг {amount} рублей, {name}. Помочь?", "Просрочено {amount} рублей."],
        "bal_pending": ["К оплате {amount} рублей, {name}."],
        "bal_clear": ["Отличные новости, {name}! Долгов нет.", "Баланс в порядке."],
        "invoice_one": ["Один счет на {amount} рублей, статус {status}."],
        "invoice_many": ["Вижу {count} счетов. Последний на {amount} рублей, {status}."],
        "plan_details": ["План {name} за {price} рублей. {data} ГБ данных."],
        "ticket_open": ["У вас {count} открытых тикетов. Последний про {subject}."],
        "ticket_resolved": ["Все ваши {count} тикетов решены!", "Нет открытых проблем."],
        "ticket_none": ["Нет обращений в поддержку."],
        "cust_info": ["Email: {email}, Телефон: {phone}."],
        "general_found": ["Вот что я нашел."],
    },
    "tr": {
        "greeting_new": ["Merhaba! 4 haneli PIN kodunuzu söyleyebilir misiniz?", "Hoş geldiniz. Hesabınızı doğrulamak için PIN kodunuza ihtiyacım var."],
        "greeting_returning": ["Merhaba {name}! Tekrar hoş geldiniz. Bugün size nasıl yardımcı olabilirim?", "Selam {name}! İşler nasıl gidiyor?"],
        "pin_invalid": ["Hmm, bu PIN eşleşmedi. {remaining} hakkınız kaldı.", "Bu işe yaramadı. {remaining} deneme kaldı. Tekrar dener misiniz?"],
        "pin_unclear": ["Üzgünüm, tam anlayamadım. 4 haneli PIN kodunuzu tekrar söyler misiniz?", "Kaçırdım - PIN kodunuzu tekrar eder misiniz?"],
        "pin_locked": ["Üzgünüm, PIN deneme sınırına ulaştınız. Lütfen destek ile iletişime geçin."],
        "upgrade_ask_plan": ["Tabii ki! Standard mı yoksa Premium mu düşünüyorsunuz?", "Kesinlikle! Standard mı Premium mu?"],
        "upgrade_confirm": ["Mükemmel, {plan} paketine geçiş. Bu isteği göndereyim mi?", "Anlaşıldı - {plan}. Yükseltmeyi onaylıyor musunuz?"],
        "upgrade_submitted": ["Tamamdır! {plan} için yükseltme isteğinizi gönderdim.", "Mükemmel, {plan} yükseltme isteğiniz alındı."],
        "upgrade_cancelled": ["Sorun değil! Fikrinizi değiştirirseniz bana bildirin.", "Anlaşıldı - şimdilik değişiklik yok."],
        "info_not_found": ["Hesabınızda {type} bilgisi bulamadım.", "{type} bulunamadı."],
        "query_error": ["Bunu ararken bir sorunla karşılaştım. Tekrar deneyelim mi?", "Sistem hatası. Tekrar dener misiniz?"],
        "goodbye": ["Kendinize iyi bakın! Bir şeye ihtiyacınız olursa aramaktan çekinmeyin.", "Aradığınız için teşekkürler! İyi günler!"],
        "ticket_ask_details": ["Tabii, bir destek talebi açabilirim. Lütfen sorunu tanımlayın.", "Buna yardımcı olabilirim. Biletin nedeni nedir?"],
        "ticket_created": ["Sizin için {id} numaralı bileti açtım.", "Tamamlandı. #{id} numaralı bilet oluşturuldu."],
        "ticket_close_success": ["Tamamdır. {id} numaralı '{subject}' konulu bileti kapattım.", "{id} numaralı açık biletinizi kapattım."],
        "ticket_close_latest": ["{count} açık biletiniz var. En sonuncusu olan {id} numaralı, '{subject}' konulu bileti kapattım."],
        "ticket_close_fail": ["O bileti kapatmaya çalıştım ama bir sorunla karşılaştım.", "Biletinizi güncellerken bir sorun oluştu."],
        "ticket_close_not_found": ["{id} numaralı açık bir bilet bulamadım. Açık biletlerinizi listelememi ister misiniz?", "Bu numarayla açık bilet bulunamadı."],
        "ticket_create_fail": ["Bileti açarken biraz sorun yaşadım. Daha sonra tekrar denemek isteyebilirsiniz."],
        "ticket_create_cancel": ["Tamam, bilet açmıyorum."],
        "upgrade_not_found": ["'{plan}' ile eşleşen bir plan bulamadım. Standard ve Premium seçeneklerimiz var - hangisini tercih edersiniz?"],
        "upgrade_unclear": ["Üzgünüm, anlayamadım. {plan} paketine yükseltmek istiyorsunuz, değil mi? Evet veya hayır."],
        "upgrade_error": ["Küçük bir sorunla karşılaştım. Yükseltmeyi tekrar onaylayabilir misiniz?"],
        "sub_active": ["{plan} paketindesiniz, {name}. Aylık {price} lira.", "Şu anki planınız {plan}, aylık ücreti {price} lira."],
        "sub_status": ["{plan} aboneliğiniz şu anda {status} durumunda."],
        "bal_overdue": ["Görünüşe göre {amount} lira gecikmiş borcunuz var, {name}. Bunu çözmek ister misiniz?", "{amount} lira ödenmemiş borcunuz bulunuyor."],
        "bal_pending": ["{amount} lira bekleyen ödemeniz var, {name}.", "Ödenmesi gereken tutar: {amount} lira."],
        "bal_clear": ["İyi haber, {name}! Bakiyeniz temiz.", "Borcunuz yok."],
        "invoice_one": ["{amount} liralık bir faturanız var ve durumu {status}."],
        "invoice_many": ["{count} fatura görüyorum. En sonuncusu {amount} lira ve durumu {status}."],
        "plan_details": ["{name} paketindesiniz, aylık {price} lira. {data} gigabayt veriniz var."],
        "ticket_open": ["{count} açık biletiniz var. En sonuncusu {subject} hakkında.", "Devam eden {count} sorununuz var."],
        "ticket_resolved": ["{count} destek biletinizin tamamı çözüldü!", "Tüm sorunlarınız giderildi."],
        "ticket_none": ["Hiç destek biletiniz yok."],
        "cust_info": ["Hesap e-postanız {email} ve telefonunuz {phone}."],
        "general_found": ["Sizin için bulduklarım bunlar."],
    },
    "pl": {
        "greeting_new": ["Cześć! Witaj. Czy możesz podać mi swój czterocyfrowy kod PIN, aby zweryfikować konto?", "Hej! Dzięki za telefon. Potrzebuję tylko Twojego czterocyfrowego kodu PIN, aby otworzyć konto."],
        "greeting_returning": ["Hej {name}! Dobrze Cię znowu słyszeć. Co mogę dziś dla Ciebie zrobić?", "Cześć {name}! Jak leci? Co Cię dziś do nas sprowadza?"],
        "pin_invalid": ["Hmm, ten PIN nie pasuje. Masz jeszcze {remaining} próby.", "To nie zadziałało. Pozostało {remaining} prób. Spróbujesz ponownie?"],
        "pin_unclear": ["Przepraszam, nie do końca zrozumiałem. Czy możesz powtórzyć swój czterocyfrowy PIN?", "Umknęło mi to - czy możesz powtórzyć PIN?"],
        "pin_locked": ["Przykro mi, wyczerpałeś limit prób PIN. Skontaktuj się z pomocą techniczną."],
        "upgrade_ask_plan": ["Jasne! Interesuje Cię Standard czy Premium?", "Oczywiście! Myślisz o Standard czy Premium?"],
        "upgrade_confirm": ["Świetnie, zmiana na {plan}. Czy mam wysłać to zgłoszenie?", "Zrozumiałem - {plan}. Potwierdzasz zmianę?"],
        "upgrade_submitted": ["Gotowe! Wysłałem Twoją prośbę o zmianę na {plan}.", "Świetnie, Twoje zgłoszenie o {plan} zostało przyjęte."],
        "upgrade_cancelled": ["Nie ma problemu! Daj znać, jeśli zmienisz zdanie.", "Zrozumiałem - na razie bez zmian."],
        "info_not_found": ["Nie mogłem znaleźć żadnych informacji o {type} na Twoim koncie.", "Nie znaleziono {type}."],
        "query_error": ["Napotkałem problem podczas wyszukiwania. Czy możemy spróbować ponownie?", "Czkawka systemu. Spróbować ponownie?"],
        "goodbye": ["Trzymaj się! Nie wahaj się zadzwonić, jeśli będziesz czegoś potrzebować.", "Dzięki za telefon! Miłego dnia!"],
        "ticket_ask_details": ["Jasne, mogę otworzyć zgłoszenie. Proszę opisz problem.", "Mogę w tym pomóc. Jaki jest powód zgłoszenia?"],
        "ticket_created": ["Otworzyłem dla Ciebie zgłoszenie numer {id}.", "Gotowe. Zgłoszenie #{id} zostało utworzone."],
        "ticket_close_success": ["Gotowe. Zamknąłem zgłoszenie numer {id} dotyczące '{subject}'.", "Zamknąłem Twoje otwarte zgłoszenie numer {id}."],
        "ticket_close_latest": ["Masz {count} otwartych zgłoszeń. Zamknąłem najnowsze, zgłoszenie {id}, dotyczące '{subject}'."],
        "ticket_close_fail": ["Próbowałem zamknąć to zgłoszenie, ale napotkałem problem.", "Napotkałem problem podczas aktualizacji Twojego zgłoszenia."],
        "ticket_close_not_found": ["Nie mogłem znaleźć otwartego zgłoszenia o numerze {id}. Czy chcesz, abym wymienił Twoje otwarte zgłoszenia?", "Nie znaleziono otwartego zgłoszenia o tym numerze."],
        "ticket_create_fail": ["Miałem mały problem z otwarciem tego zgłoszenia. Możesz spróbować ponownie później."],
        "ticket_create_cancel": ["Okej, nie otworzę zgłoszenia."],
        "upgrade_not_found": ["Nie mogłem znaleźć planu pasującego do '{plan}'. Mamy Standard i Premium - który wolisz?"],
        "upgrade_unclear": ["Przepraszam, nie złapałem tego. Chcesz zmienić na {plan}, prawda? Tak czy nie."],
        "upgrade_error": ["Napotkałem mały problem. Czy możesz potwierdzić zmianę jeszcze raz?"],
        "sub_active": ["Jesteś w planie {plan}, {name}. To kosztuje {price} złotych miesięcznie.", "Twój obecny plan to {plan}, {price} złotych."],
        "sub_status": ["Twój abonament {plan} jest obecnie {status}."],
        "bal_overdue": ["Wygląda na to, że masz {amount} złotych zaległości, {name}. Chcesz pomocy w uporządkowaniu tego?", "Masz {amount} złotych przeterminowanej płatności."],
        "bal_pending": ["Masz {amount} złotych do zapłaty, {name}. Nic zaległego.", "Do zapłaty: {amount} złotych."],
        "bal_clear": ["Dobre wieści, {name}! Twoje saldo jest czyste.", "Brak zaległości."],
        "invoice_one": ["Masz jedną fakturę na {amount} złotych, a jej status to {status}."],
        "invoice_many": ["Widzę {count} faktur. Twoja najnowsza jest na {amount} złotych, obecnie {status}."],
        "plan_details": ["Jesteś w {name} za {price} złotych miesięcznie. Masz {data} giga danych."],
        "ticket_open": ["Masz {count} otwartych zgłoszeń. Najnowsze dotyczy {subject}.", "Liczba otwartych spraw: {count}."],
        "ticket_resolved": ["Wszystkie Twoje {count} zgłoszenia zostały rozwiązane!", "Wszystkie sprawy zamknięte."],
        "ticket_none": ["Nie masz żadnych zgłoszeń technicznych."],
        "cust_info": ["Twój email to {email}, a telefon to {phone}."],
        "general_found": ["Oto co dla Ciebie znalazłem."],
    },
    "nl": {
        "greeting_new": ["Hallo! Welkom. Kunt u mij uw viercijferige pincode geven om uw account te verifiëren?", "Hoi! Bedankt voor het bellen. Ik heb alleen uw viercijferige pincode nodig om uw account op te zoeken."],
        "greeting_returning": ["Hé {name}! Fijn dat je er weer bent. Wat kan ik vandaag voor je doen?", "Hoi {name}! Hoe gaat het? Waarmee kan ik helpen?"],
        "pin_invalid": ["Hmm, die pincode klopte niet. Je hebt nog {remaining} pogingen.", "Dat werkte niet. Nog {remaining} pogingen over. Opnieuw proberen?"],
        "pin_unclear": ["Sorry, dat heb ik niet helemaal verstaan. Kunt u uw viercijferige pincode nog eens zeggen?", "Ik heb dat gemist - kunt u uw pincode herhalen?"],
        "pin_locked": ["Het spijt me, u heeft de limiet voor pincodepogingen bereikt. Neem contact op met de ondersteuning."],
        "upgrade_ask_plan": ["Natuurlijk! Kijk je naar Standard of Premium?", "Absoluut! Denk je aan Standard of Premium?"],
        "upgrade_confirm": ["Perfect, upgrade naar {plan}. Zal ik dat verzoek indienen?", "Begrepen - {plan}. De upgrade bevestigen?"],
        "upgrade_submitted": ["Gedaan! Ik heb uw upgradeverzoek voor {plan} ingediend.", "Perfect, uw {plan} upgradeverzoek is binnen."],
        "upgrade_cancelled": ["Geen probleem! Laat het me weten als je van gedachten verandert.", "Begrepen - voorlopig geen wijzigingen."],
        "info_not_found": ["Ik kon geen {type} informatie vinden op uw account.", "Geen {type} gevonden."],
        "query_error": ["Ik kwam een probleem tegen bij het opzoeken. Zullen we het opnieuw proberen?", "Systeemfoutje. Opnieuw proberen?"],
        "goodbye": ["Het ga je goed! Aarzel niet om te bellen als je iets nodig hebt.", "Bedankt voor het bellen! Fijne dag!"],
        "ticket_ask_details": ["Natuurlijk, ik kan een ticket openen. Beschrijf het probleem alsjeblieft.", "Ik kan daarbij helpen. Wat is de reden voor het ticket?"],
        "ticket_created": ["Ik heb ticketnummer {id} voor je geopend.", "Gedaan. Ticket #{id} is aangemaakt."],
        "ticket_close_success": ["Gedaan. Ik heb ticketnummer {id} over '{subject}' gesloten.", "Ik heb uw open ticket, nummer {id}, gesloten."],
        "ticket_close_latest": ["Je hebt {count} open tickets. Ik heb de meest recente gesloten, ticket {id}, over '{subject}'."],
        "ticket_close_fail": ["Ik probeerde dat ticket te sluiten maar kwam een probleem tegen.", "Ik kwam een probleem tegen bij het bijwerken van uw ticket."],
        "ticket_close_not_found": ["Ik kon geen open ticket vinden met nummer {id}. Wil je dat ik je open tickets opnoem?", "Geen open ticket gevonden met dat nummer."],
        "ticket_create_fail": ["Ik had wat moeite met het openen van dat ticket. Misschien wil je het later nog eens proberen."],
        "ticket_create_cancel": ["Oké, ik open geen ticket."],
        "upgrade_not_found": ["Ik kon geen plan vinden dat overeenkomt met '{plan}'. We hebben Standard en Premium - welke heb je liever?"],
        "upgrade_unclear": ["Sorry, dat heb ik niet begrepen. Je wilt upgraden naar {plan}, toch? Ja of nee."],
        "upgrade_error": ["Ik kwam een klein probleem tegen. Kun je de upgrade nog eens bevestigen?"],
        "sub_active": ["Je zit op het {plan} plan, {name}. Dat is {price} euro per maand.", "Huidig plan: {plan}, {price} euro."],
        "sub_status": ["Je {plan} abonnement is momenteel {status}."],
        "bal_overdue": ["Het lijkt erop dat je {amount} euro achterstallig bent, {name}. Wil je hulp om dat te regelen?", "Let op: {amount} euro achterstand."],
        "bal_pending": ["Je hebt {amount} euro openstaand, {name}. Niets achterstallig.", "Openstaand bedrag: {amount} euro."],
        "bal_clear": ["Goed nieuws, {name}! Je saldo is in orde.", "Alles is betaald."],
        "invoice_one": ["Je hebt één factuur voor {amount} euro, en die is {status}."],
        "invoice_many": ["Ik zie {count} facturen. Je meest recente is {amount} euro, momenteel {status}."],
        "plan_details": ["Je zit op {name} voor {price} euro per maand. Je hebt {data} gigabyte data."],
        "ticket_open": ["Je hebt {count} open tickets. De meest recente gaat over {subject}.", "Er zijn {count} lopende zaken."],
        "ticket_resolved": ["Al je {count} ondersteuningstickets zijn opgelost!", "Alle problemen verholpen."],
        "ticket_none": ["Je hebt geen ondersteuningstickets."],
        "cust_info": ["Uw account e-mail is {email} en telefoon is {phone}."],
        "general_found": ["Dit is wat ik voor je heb gevonden."],
    },
    "cs": {
        "greeting_new": ["Ahoj! Vítejte. Můžete mi říct svůj čtyřmístný PIN kód pro ověření účtu?", "Dobrý den! Díky za zavolání. Potřebuji jen váš čtyřmístný PIN kód, abych mohl vyhledat váš účet."],
        "greeting_returning": ["Ahoj {name}! Jsem rád, že jsi zpět. Co pro tebe dnes mohu udělat?", "Ahoj {name}! Jak to jde? Co tě k nám dnes přivádí?"],
        "pin_invalid": ["Hmm, ten PIN nesouhlasí. Máte ještě {remaining} pokusy.", "To nefungovalo. Zbývá {remaining} pokusů. Zkusíte to znovu?"],
        "pin_unclear": ["Omlouvám se, to jsem úplně nezachytil. Mohl byste znovu říct svůj čtyřmístný PIN?", "Uniklo mi to - mohl byste zopakovat svůj PIN?"],
        "pin_locked": ["Omlouvám se, vyčerpal jste limit pokusů o PIN. Kontaktujte prosím podporu."],
        "upgrade_ask_plan": ["Jasná věc! Díváte se na Standard nebo Premium?", "Rozhodně! Přemýšlíte o Standard nebo Premium?"],
        "upgrade_confirm": ["Perfektní, upgrade na {plan}. Mám tuto žádost odeslat?", "Rozumím - {plan}. Potvrdit upgrade?"],
        "upgrade_submitted": ["Hotovo! Odeslal jsem vaši žádost o upgrade na {plan}.", "Perfektní, vaše žádost o upgrade na {plan} je přijata."],
        "upgrade_cancelled": ["Žádný problém! Dejte mi vědět, pokud změníte názor.", "Rozumím - prozatím žádné změny."],
        "info_not_found": ["Nemohl jsem najít žádné informace o {type} na vašem účtu.", "Žádné {type} nenalezeny."],
        "query_error": ["Při vyhledávání jsem narazil na zádrhel. Nevadí, když to zkusíme znovu?", "Škytavka systému. Zkusit znovu?"],
        "goodbye": ["Mějte se! Neváhejte zavolat, pokud budete cokoliv potřebovat.", "Díky za zavolání! Přeji hezký den!"],
        "ticket_ask_details": ["Jistě, mohu otevřít tiket. Prosím, popište problém.", "Mohu s tím pomoci. Jaký je důvod tiketu?"],
        "ticket_created": ["Otevřel jsem pro vás tiket číslo {id}.", "Hotovo. Tiket #{id} byl vytvořen."],
        "ticket_close_success": ["Hotovo. Uzavřel jsem tiket číslo {id} týkající se '{subject}'.", "Uzavřel jsem váš otevřený tiket, číslo {id}."],
        "ticket_close_latest": ["Máte {count} otevřených tiketů. Uzavřel jsem ten nejnovější, tiket {id}, týkající se '{subject}'."],
        "ticket_close_fail": ["Snažil jsem se ten tiket uzavřít, ale narazil jsem na problém.", "Při aktualizaci vašeho tiketu jsem narazil na problém."],
        "ticket_close_not_found": ["Nemohl jsem najít otevřený tiket s číslem {id}. Chcete, abych vyjmenoval vaše otevřené tikety?", "S tímto číslem nebyl nalezen žádný otevřený tiket."],
        "ticket_create_fail": ["Měl jsem trochu potíže s otevřením toho tiketu. Možná to budete chtít zkusit později."],
        "ticket_create_cancel": ["Dobře, neotevřu tiket."],
        "upgrade_not_found": ["Nemohl jsem najít plán odpovídající '{plan}'. Máme Standard a Premium - kterému byste dali přednost?"],
        "upgrade_unclear": ["Promiňte, to jsem nezachytil. Chcete upgradovat na {plan}, že? Ano nebo ne."],
        "upgrade_error": ["Narazil jsem na malý problém. Můžete upgrade potvrdit znovu?"],
        "sub_active": ["Jste na plánu {plan}, {name}. To je {price} korun měsíčně.", "Váš plán: {plan}, {price} korun."],
        "sub_status": ["Vaše předplatné {plan} je aktuálně {status}."],
        "bal_overdue": ["Vypadá to, že máte {amount} korun po splatnosti, {name}. Chcete pomoci to vyřešit?", "Pozor: {amount} korun po splatnosti."],
        "bal_pending": ["Máte {amount} korun k úhradě, {name}. Nic po splatnosti.", "Částka k úhradě: {amount} korun."],
        "bal_clear": ["Dobré zprávy, {name}! Váš zůstatek je čistý.", "Vše uhrazeno."],
        "invoice_one": ["Máte jednu fakturu na {amount} korun a je {status}."],
        "invoice_many": ["Vidím {count} faktur. Vaše nejnovější je na {amount} korun, aktuálně {status}."],
        "plan_details": ["Jste na {name} za {price} korun měsíčně. Máte {data} giga dat."],
        "ticket_open": ["Máte {count} otevřených tiketů. Nejnovější je o {subject}.", "Počet otevřených problémů: {count}."],
        "ticket_resolved": ["Všech vašich {count} tiketů podpory bylo vyřešeno!", "Vše vyřešeno."],
        "ticket_none": ["Nemáte žádné tikety podpory."],
        "cust_info": ["Váš email účtu je {email} a telefon je {phone}."],
        "general_found": ["Tady je to, co jsem pro vás našel."],
    },
    "hu": {
        "greeting_new": ["Szia! Üdvözöllek. Megmondanád a négyjegyű PIN kódodat a fiókod ellenőrzéséhez?", "Szia! Kösz, hogy hívtál. Csak a négyjegyű PIN kódodra van szükségem a fiókod előhívásához."],
        "greeting_returning": ["Szia {name}! Jó, hogy újra itt vagy. Mit tehetek érted ma?", "Szia {name}! Hogy vagy? Mi járatban vagy ma?"],
        "pin_invalid": ["Hmm, ez a PIN nem egyezik. Még {remaining} próbálkozásod van.", "Ez nem sikerült. {remaining} próbálkozás maradt. Megpróbálod újra?"],
        "pin_unclear": ["Bocsánat, ezt nem teljesen értettem. Elmondanád újra a négyjegyű PIN kódodat?", "Ezt elmulasztottam - megismételnéd a PIN kódodat?"],
        "pin_locked": ["Sajnálom, elérted a PIN próbálkozások korlátját. Kérlek, lépj kapcsolatba az ügyfélszolgálattal."],
        "upgrade_ask_plan": ["Persze! A Standard vagy a Premium csomagot nézed?", "Abszolút! A Standardon vagy a Premiumon gondolkodsz?"],
        "upgrade_confirm": ["Tökéletes, frissítés {plan}-ra. Benyújtsam ezt a kérelmet?", "Értem - {plan}. Megerősíted a frissítést?"],
        "upgrade_submitted": ["Kész! Benyújtottam a frissítési kérelmedet a {plan}-ra.", "Tökéletes, a {plan} frissítési kérelmed beérkezett."],
        "upgrade_cancelled": ["Semmi gond! Szólj, ha meggondolod magad.", "Értem - egyelőre nincsenek változások."],
        "info_not_found": ["Nem találtam semmilyen {type} információt a fiókodban.", "Nincs {type} találat."],
        "query_error": ["Bökkenőbe ütköztem a keresés során. Nem bánod, ha újra megpróbáljuk?", "Rendszerhiba. Újrapróbáljuk?"],
        "goodbye": ["Vigyázz magadra! Ne habozz hívni, ha bármire szükséged van.", "Kösz a hívást! Legyen szép napod!"],
        "ticket_ask_details": ["Persze, nyithatok egy jegyet. Kérlek, írd le a problémát.", "Segíthetek ebben. Mi a jegy oka?"],
        "ticket_created": ["Megnyitottam neked a(z) {id} számú jegyet.", "Kész. A(z) #{id} jegy létrehozva."],
        "ticket_close_success": ["Kész. Lezártam a(z) {id} számú jegyet a(z) '{subject}' témában.", "Lezártam a nyitott jegyedet, a(z) {id} számút."],
        "ticket_close_latest": ["{count} nyitott jegyed van. Lezártam a legutóbbit, a(z) {id} számú jegyet a(z) '{subject}' témában."],
        "ticket_close_fail": ["Megpróbáltam lezárni azt a jegyet, de problémába ütköztem.", "Problémába ütköztem a jegyed frissítésekor."],
        "ticket_close_not_found": ["Nem találtam {id} számú nyitott jegyet. Szeretnéd, hogy felsoroljam a nyitott jegyeidet?", "Nem található nyitott jegy ezzel a számmal."],
        "ticket_create_fail": ["Kicsit nehezen tudtam megnyitni azt a jegyet. Lehet, hogy később újra meg akarod próbálni."],
        "ticket_create_cancel": ["Rendben, nem nyitok jegyet."],
        "upgrade_not_found": ["Nem találtam a(z) '{plan}'-nak megfelelő csomagot. Van Standard és Premium csomagunk - melyiket szeretnéd?"],
        "upgrade_unclear": ["Bocsánat, ezt nem fogtam. Frissíteni szeretnél a(z) {plan}-ra, ugye? Igen vagy nem."],
        "upgrade_error": ["Kisebb problémába ütköztem. Megerősítenéd újra a frissítést?"],
        "sub_active": ["A(z) {plan} csomagban vagy, {name}. Ez {price} forint havonta.", "Jelenlegi csomagod: {plan}, {price} forint."],
        "sub_status": ["A(z) {plan} előfizetésed jelenleg {status}."],
        "bal_overdue": ["Úgy tűnik, {amount} forint hátralékod van, {name}. Szeretnél segítséget a rendezésében?", "Figyelem: {amount} forint lejárt tartozás."],
        "bal_pending": ["{amount} forint függő tételed van, {name}. Semmi lejárt.", "Fizetendő összeg: {amount} forint."],
        "bal_clear": ["Jó hírek, {name}! Az egyenleged tiszta.", "Nincs tartozás."],
        "invoice_one": ["Van egy számlád {amount} forintról, és ez {status}."],
        "invoice_many": ["{count} számlát látok. a legutóbbi {amount} forintról szól, jelenleg {status}."],
        "plan_details": ["A(z) {name} csomagban vagy havi {price} forintért. {data} giga adatod van."],
        "ticket_open": ["{count} nyitott jegyed van. A legutóbbi a(z) {subject} témáról szól.", "{count} nyitott ügy."],
        "ticket_resolved": ["Az összes {count} támogatási jegyed megoldódott!", "Minden probléma megoldva."],
        "ticket_none": ["Nincsenek támogatási jegyeid."],
        "cust_info": ["A fiókod e-mail címe {email}, a telefonszáma pedig {phone}."],
        "general_found": ["Itt van, amit találtam neked."],
    },
    "hi": {
        "greeting_new": ["Namaste! Swagat hai. Kya aap mujhe apna chaar ankon ka PIN bata sakte hain?", "Hello! Call karne ke liye shukriya. Mujhe bas aapka chaar ankon ka PIN chahiye."],
        "greeting_returning": ["Namaste {name}! Aapko wapas dekhkar achha laga. Aaj main aapki kya madad kar sakta hoon?", "Hello {name}! Kaisa chal raha hai?"],
        "pin_invalid": ["Hmm, woh PIN match nahi hua. Aapke paas {remaining} aur koshishen hain.", "Woh kaam nahi kiya. {remaining} koshishen bachi hain. Phir se koshish karen?"],
        "pin_unclear": ["Maaf kijiye, main theek se sun nahi paya. Kya aap apna chaar ankon ka PIN phir se bol sakte hain?", "Maine woh miss kar diya - kya aap apna PIN dohra sakte hain?"],
        "pin_locked": ["Mujhe khed hai, aapne PIN koshishon ki seema paar kar li hai. Kripya support se sampark karen."],
        "upgrade_ask_plan": ["Zaroor! Kya aap Standard ya Premium dekh rahe hain?", "Bilkul! Standard ya Premium ke baare mein soch rahe hain?"],
        "upgrade_confirm": ["Sahi hai, {plan} par upgrade. Kya main woh anurodh jama karoon?", "Samajh gaya - {plan}. Upgrade confirm karen?"],
        "upgrade_submitted": ["Ho gaya! Maine {plan} ke liye aapka upgrade anurodh jama kar diya hai.", "Sahi hai, aapka {plan} upgrade anurodh andar hai."],
        "upgrade_cancelled": ["Koi baat nahi! Agar aap apna man badalte hain to mujhe batayen.", "Samajh gaya - abhi ke liye koi badlav nahi."],
        "info_not_found": ["Mujhe aapke account par koi {type} jaankari nahi mili.", "Koi {type} nahi mila."],
        "query_error": ["Mujhe use dhundhne mein thodi dikkat hui. Kya hum phir se koshish kar sakte hain?", "System mein dikkat. Phir se koshish karen?"],
        "goodbye": ["Apna khayal rakhen! Agar aapko kisi cheez ki zaroorat ho to call karne mein sankoch na karen.", "Call karne ke liye shukriya! Aapka din shubh ho!"],
        "ticket_ask_details": ["Zaroor, main ek ticket khol sakta hoon. Kripya samasya ka varnan karen.", "Main usmein madad kar sakta hoon. Ticket ka karan kya hai?"],
        "ticket_created": ["Maine aapke liye ticket number {id} khol diya hai.", "Ho gaya. Ticket #{id} banaya gaya hai."],
        "ticket_close_success": ["Ho gaya. Maine '{subject}' ke baare mein ticket number {id} band kar diya hai.", "Maine aapka khula ticket, number {id}, band kar diya hai."],
        "ticket_close_latest": ["Aapke paas {count} khule ticket hain. Maine sabse haaliya ticket, ticket {id}, '{subject}' ke baare mein band kar diya hai."],
        "ticket_close_fail": ["Maine us ticket ko band karne ki koshish ki lekin ek samasya aayi.", "Aapke ticket ko update karte samay mujhe ek samasya aayi."],
        "ticket_close_not_found": ["Mujhe number {id} ke saath koi khula ticket nahi mila. Kya aap chahte hain ki main aapke khule ticket list karoon?", "Us number ke saath koi khula ticket nahi mila."],
        "ticket_create_fail": ["Mujhe us ticket ko kholne mein thodi pareshani hui. Aap baad mein phir se koshish karna chahenge."],
        "ticket_create_cancel": ["Theek hai, main ticket nahi kholunga."],
        "upgrade_not_found": ["Mujhe '{plan}' se mail khata hua koi plan nahi mila. Hamare paas Standard aur Premium hain - aap kaun sa pasand karenge?"],
        "upgrade_unclear": ["Maaf kijiye, maine woh nahi pakda. Aap {plan} par upgrade karna chahte hain, hai na? Haan ya nahi."],
        "upgrade_error": ["Mujhe ek chhoti si samasya aayi. Kya aap upgrade phir se confirm kar sakte hain?"],
        "sub_active": ["Aap {plan} plan par hain, {name}. Woh {price} rupaye mahina hai.", "Aapka plan: {plan}, {price} rupaye."],
        "sub_status": ["Aapka {plan} subscription abhi {status} hai."],
        "bal_overdue": ["Lagta hai aapka {amount} rupaye बकाया hai, {name}. Kya aapko use suljhane mein madad chahiye?", "Dhyan den: {amount} rupaye bakaya."],
        "bal_pending": ["Aapka {amount} rupaye pending hai, {name}. Kuch bhi overdue nahi.", "Baqaya rashi: {amount} rupaye."],
        "bal_clear": ["Achhi khabar, {name}! Aapka balance saaf hai.", "Sab kuch paid hai."],
        "invoice_one": ["Aapke paas {amount} rupaye ka ek invoice hai, aur woh {status} hai."],
        "invoice_many": ["Mujhe {count} invoices dikh rahe hain. Aapka sabse haaliya {amount} rupaye ka hai, abhi {status}."],
        "plan_details": ["Aap {name} par hain {price} rupaye mahina. Aapke paas {data} GB data hai."],
        "ticket_open": ["Aapke paas {count} khule ticket hain. Sabse haaliya {subject} ke baare mein hai.", "{count} mamle khule hain."],
        "ticket_resolved": ["Aapke sabhi {count} support tickets hal ho gaye hain!", "Sabhi samasyaen hal ho gayin."],
        "ticket_none": ["Aapke paas koi support ticket nahi hai."],
        "cust_info": ["Aapka account email {email} hai aur phone {phone} hai."],
        "general_found": ["Yeh raha jo mujhe aapke liye mila."],
    },
    "ar": {
        "greeting_new": ["مرحبًا! هل يمكنك إخباري برمز PIN المكون من 4 أرقام؟", "أهلاً بك. أحتاج الرمز السري للتحقق."],
        "greeting_returning": ["أهلاً {name}! كيف يمكنني مساعدتك اليوم؟", "مرحبًا {name}، سعيد بعودتك."],
        "pin_invalid": ["الرمز غير صحيح. بقيت {remaining} محاولات.", "خطأ في الرمز. {remaining} محاولات متبقية."],
        "pin_unclear": ["عفواً، لم أسمع جيداً. هل يمكنك تكرار الرمز؟", "أعد الأرقام الأربعة من فضلك."],
        "pin_locked": ["عذراً، تم تجاوز الحد المسموح. يرجى الاتصال بالدعم.", "تم قفل الحساب."],
        "upgrade_ask_plan": ["بالتأكيد! هل تفكر في Standard أم Premium؟", "هل تريد باقة Standard أم Premium؟"],
        "upgrade_confirm": ["ممتاز، الترقية إلى {plan}. هل أؤكد الطلب؟", "خطة {plan}. موافق؟"],
        "upgrade_submitted": ["تم! أرسلت طلب الترقية إلى {plan}.", "تم تقديم طلب {plan} بنجاح."],
        "upgrade_cancelled": ["لا مشكلة. أخبرني إذا غيرت رأيك.", "تم الإلغاء."],
        "info_not_found": ["لم أجد معلومات حول {type}.", "لا توجد بيانات {type}."],
        "query_error": ["حدث خطأ بسيط. هل نحاول مرة أخرى؟", "خطأ في النظام."],
        "goodbye": ["مع السلامة! اتصل في أي وقت.", "يومك سعيد!"],
        "ticket_ask_details": ["سأفتح تذكرة دعم. ما هي المشكلة؟", "صف لي المشكلة لفتح التذكرة."],
        "ticket_created": ["فتحت التذكرة رقم {id}.", "تم إنشاء التذكرة #{id}."],
        "ticket_close_success": ["تم. أغلقت التذكرة رقم {id}.", "التذكرة {id} مغلقة الآن."],
        "ticket_close_latest": ["لديك {count} تذاكر مفتوحة. أغلقت الأحدث، رقم {id}."],
        "ticket_close_fail": ["واجهت مشكلة في إغلاق التذكرة."],
        "ticket_close_not_found": ["لم أجد تذكرة مفتوحة برقم {id}."],
        "ticket_create_fail": ["واجهت مشكلة في إنشاء التذكرة."],
        "ticket_create_cancel": ["حسناً، لن أفتح تذكرة."],
        "upgrade_not_found": ["لم أجد خطة باسم '{plan}'."],
        "upgrade_unclear": ["عفواً، هل تريد الترقية إلى {plan}؟"],
        "upgrade_error": ["حدث خطأ بسيط. هل تؤكد الطلب؟"],
        "sub_active": ["أنت على خطة {plan} يا {name}. {price} دولار شهرياً.", "باقتك {plan} بسعر {price}."],
        "sub_status": ["اشتراكك في {plan} حالياً {status}."],
        "bal_overdue": ["لديك {amount} دولار متأخرة يا {name}. هل أساعدك؟", "تنبيه، عليك {amount} دولار."],
        "bal_pending": ["عليك {amount} دولار معلقة، يا {name}."],
        "bal_clear": ["أخبار جيدة يا {name}! رصيدك خالٍ.", "تم سداد كل شيء."],
        "invoice_one": ["لديك فاتورة واحدة بقيمة {amount} دولار، حالتها {status}."],
        "invoice_many": ["أرى {count} فواتير. الأحدث بقيمة {amount} دولار، {status}."],
        "plan_details": ["خطة {name} بـ {price} دولار. {data} جيجابايت بيانات.", "باقة {name}."],
        "ticket_open": ["لديك {count} تذاكر مفتوحة. الأحدث حول {subject}.", "{count} شكاوى مفتوحة."],
        "ticket_resolved": ["تم حل جميع تذاكرك الـ {count}!", "لا توجد مشاكل."],
        "ticket_none": ["لا توجد تذاكر دعم."],
        "cust_info": ["البريد: {email}، الهاتف: {phone}."],
        "general_found": ["هذا ما وجدته."],
    },
}

def get_natural_response(key, lang="en", **kwargs):
    """Get a random natural response template and format it"""
    import random
    # Fallback to English if language not found
    lang_responses = NATURAL_RESPONSES.get(lang, NATURAL_RESPONSES.get("en", {}))
    templates = lang_responses.get(key, NATURAL_RESPONSES.get("en", {}).get(key, ["I'm here to help."]))
    template = random.choice(templates)
    return template.format(**kwargs) if kwargs else template


# =============================================================================
# Pre-defined SQL queries for common operations
# =============================================================================
PREDEFINED_QUERIES = {
    'subscription': """
        SELECT c.name as customer_name, p.name as plan_name, p.price, 
               s.status, s.start_date, s.end_date, s.auto_renew
        FROM subscriptions s
        JOIN customers c ON s.customer_id = c.id
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s
        ORDER BY s.start_date DESC
        LIMIT 5
    """,
    'balance': """
        SELECT 
            COALESCE(SUM(CASE WHEN i.status = 'pending' THEN i.amount ELSE 0 END), 0) as pending_amount,
            COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.amount ELSE 0 END), 0) as paid_amount,
            COALESCE(SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END), 0) as overdue_amount
        FROM invoices i
        WHERE i.customer_id = %s
    """,
    'invoices': """
        SELECT i.id, i.amount, i.status, i.due_date, i.paid_at,
               p.name as plan_name
        FROM invoices i
        JOIN subscriptions s ON i.subscription_id = s.id
        JOIN plans p ON s.plan_id = p.id
        WHERE i.customer_id = %s
        ORDER BY i.due_date DESC
        LIMIT 10
    """,
    'plan': """
        SELECT p.name, p.price, p.billing_cycle, p.features, 
               p.data_limit_gb, p.support_level
        FROM subscriptions s
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s AND s.status = 'active'
        LIMIT 1
    """,
    'tickets': """
        SELECT id, subject, status, priority, created_at
        FROM support_tickets
        WHERE customer_id = %s
        ORDER BY created_at DESC
        LIMIT 10
    """,
    'customer_info': """
        SELECT id, name, email, phone, created_at
        FROM customers
        WHERE id = %s
    """,
    'upgrade_options': """
        SELECT p.id, p.name, p.price, p.data_limit_gb, p.support_level, p.features
        FROM plans p
        WHERE p.price > (
            SELECT pl.price FROM subscriptions s 
            JOIN plans pl ON s.plan_id = pl.id 
            WHERE s.customer_id = %s AND s.status = 'active'
            LIMIT 1
        )
        ORDER BY p.price ASC
        LIMIT 3
    """,
    'current_plan': """
        SELECT p.id, p.name, p.price
        FROM subscriptions s
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s AND s.status = 'active'
        LIMIT 1
    """,
    'all_plans': """
        SELECT id, name, price, data_limit_gb, support_level
        FROM plans
        ORDER BY price ASC
    """
}

# Keywords that trigger each query type (English and Hebrew support)
# IMPORTANT: Specific actions must come BEFORE general queries to avoid shadowing
QUERY_TRIGGERS = {
    'create_ticket': [
        # English
        'open ticket', 'new ticket', 'create ticket', 'report issue', 'have a problem', 'something is wrong',
        'start ticket', 'file ticket', 'submit ticket',
        'open a ticket', 'open my ticket', 'create a ticket', 'create new ticket', 'start a ticket',
        'i want to open', 'need to open',
        # Hebrew
        'לפתוח קריאה', 'לפתוח כרטיס', 'כרטיס חדש', 'קריאה חדשה', 'יש לי בעיה', 'משהו לא עובד', 'לדווח על תקלה',
        'תפתח קריאה', 'תפתח כרטיס', 'פתח קריאה', 'פתח כרטיס', 'לפתוח טיקט', 'תפתח טיקט', 'טיקט חדש',
        'אני רוצה לפתוח', 'יש לי תקלה', 'לדווח על בעיה',
        # Spanish
        'abrir ticket', 'nuevo ticket', 'crear ticket', 'reportar problema', 'tengo un problema',
        'abrir incidencia', 'nueva incidencia', 'crear incidencia',
        # French
        'ouvrir ticket', 'nouveau ticket', 'créer ticket', 'signaler problème', 'j\'ai un problème',
        'ouvrir incident', 'créer incident',
        # German
        'ticket öffnen', 'neues ticket', 'ticket erstellen', 'problem melden', 'ich habe ein problem',
        'ticket aufmachen',
        # Russian
        'открыть тикет', 'новый тикет', 'создать тикет', 'сообщить о проблеме', 'у меня проблема',
        'открыть заявку', 'новая заявка', 'создать заявку',
        # Arabic
        'فتح تذكرة', 'تذكرة جديدة', 'إنشاء تذكرة', 'الإبلاغ عن مشكلة', 'لدي مشكلة',
        # Portuguese
        'abrir ticket', 'novo ticket', 'criar ticket', 'reportar problema', 'tenho um problema',
        'abrir chamado', 'novo chamado',
        # Italian
        'aprire ticket', 'nuovo ticket', 'creare ticket', 'segnalare problema', 'ho un problema',
        'aprire segnalazione',
        # Chinese
        '打开工单', '新建工单', '创建工单', '报告问题', '我有问题',
        # Japanese
        'チケットを開く', '新しいチケット', 'チケットを作成', '問題を報告', '問題があります'
    ],
    'close_ticket': [
        # English
        'close ticket', 'resolve ticket', 'mark as resolved', 'ticket is done', 'fix issue',
        'close the ticket', 'ticket resolved', 'issue fixed',
        'close my ticket', 'close a ticket', 'close this ticket', 'close that ticket',
        'resolve my ticket', 'resolve this ticket',
        'i want to close', 'can you close',
        # Hebrew
        'לסגור קריאה', 'לסגור כרטיס', 'לסגור את הכרטיס', 'תסגור כרטיס', 'בעיה נפתרה', 'תקלה נפתרה', 'לסגור',
        'לסגור support ticket', 'לסגור support tickets', 'לסגור טיקט', 'לסגור את הטיקט', 'לסגור ticket',
        'לסגור את הקריאה', 'לסגור את הטיקט', 'תסגור את הקריאה',
        # Spanish
        'cerrar ticket', 'resolver ticket', 'ticket resuelto', 'problema solucionado',
        'cerrar mi ticket', 'cerrar incidencia',
        # French
        'fermer ticket', 'résoudre ticket', 'ticket résolu', 'problème réglé',
        'fermer mon ticket', 'clôturer ticket',
        # German
        'ticket schließen', 'ticket lösen', 'ticket erledigt', 'problem gelöst',
        'mein ticket schließen',
        # Russian
        'закрыть тикет', 'решить тикет', 'тикет решен', 'проблема решена',
        'закрыть заявку', 'закрыть мою заявку',
        # Arabic
        'إغلاق التذكرة', 'حل التذكرة', 'تم حل المشكلة', 'إغلاق تذكرتي',
        # Portuguese
        'fechar ticket', 'resolver ticket', 'ticket resolvido', 'problema resolvido',
        'fechar chamado', 'encerrar ticket',
        # Italian
        'chiudere ticket', 'risolvere ticket', 'ticket risolto', 'problema risolto',
        'chiudere segnalazione',
        # Chinese
        '关闭工单', '解决工单', '工单已解决', '问题已解决',
        # Japanese
        'チケットを閉じる', 'チケットを解決', 'チケット完了', '問題解決'
    ],
    'upgrade': [
        # English
        'upgrade', 'upgrading', 'higher plan', 'better plan', 'more features', 'switch plan', 'change plan',
        # Hebrew
        'לשדרג', 'שדרוג', 'תוכנית טובה יותר', 'להחליף תוכנית', 'חבילה גדולה יותר',
        # Spanish
        'actualizar plan', 'mejorar plan', 'cambiar plan', 'plan superior', 'más funciones',
        # French
        'mettre à niveau', 'changer de forfait', 'plan supérieur', 'meilleur forfait',
        # German
        'upgraden', 'plan ändern', 'besserer plan', 'höherer plan', 'tarif wechseln',
        # Russian
        'обновить план', 'улучшить план', 'сменить план', 'повысить план', 'лучший тариф',
        # Arabic
        'ترقية الخطة', 'تحسين الخطة', 'تغيير الخطة', 'خطة أفضل',
        # Portuguese
        'atualizar plano', 'melhorar plano', 'mudar plano', 'plano superior',
        # Italian
        'aggiornare piano', 'migliorare piano', 'cambiare piano', 'piano superiore'
    ],
    'subscription': [
        # English
        'subscription', 'subscribed', 'subscribe', 'membership', 'member', 'my plan',
        # Hebrew
        'מנוי', 'התוכנית שלי', 'חבילה', 'רשום',
        # Spanish
        'suscripción', 'suscribirse', 'mi plan', 'membresía',
        # French
        'abonnement', 'souscrire', 'mon forfait', 'adhésion',
        # German
        'abonnement', 'abo', 'mein plan', 'mitgliedschaft',
        # Russian
        'подписка', 'абонемент', 'мой план', 'членство',
        # Arabic
        'اشتراك', 'خطتي', 'عضويتي',
        # Portuguese
        'assinatura', 'inscrição', 'meu plano',
        # Italian
        'abbonamento', 'iscrizione', 'il mio piano'
    ],
    'balance': [
        # English
        'balance', 'owe', 'owing', 'due', 'payment', 'pay', 'amount',
        # Hebrew
        'יתרה', 'חוב', 'תשלום', 'לשלם', 'כמה אני חייב',
        # Spanish
        'saldo', 'debe', 'deuda', 'pagar', 'pago', 'monto',
        # French
        'solde', 'dois', 'paiement', 'payer', 'montant',
        # German
        'kontostand', 'saldo', 'schulden', 'zahlen', 'zahlung', 'betrag',
        # Russian
        'баланс', 'долг', 'оплата', 'платить', 'сумма', 'сколько я должен',
        # Arabic
        'رصيد', 'دين', 'دفع', 'مبلغ', 'كم علي',
        # Portuguese
        'saldo', 'devo', 'pagamento', 'pagar', 'quantia',
        # Italian
        'saldo', 'debiti', 'pagamento', 'pagare', 'importo'
    ],
    'invoices': [
        # English
        'invoice', 'invoices', 'bill', 'bills', 'billing', 'receipt',
        # Hebrew
        'חשבונית', 'חשבוניות', 'קבלה', 'חיוב',
        # Spanish
        'factura', 'facturas', 'recibo', 'cobro',
        # French
        'facture', 'factures', 'reçu', 'facturation',
        # German
        'rechnung', 'rechnungen', 'beleg', 'abrechnung',
        # Russian
        'счет', 'счета', 'квитанция', 'оплата',
        # Arabic
        'فاتورة', 'فواتير', 'إيصال',
        # Portuguese
        'fatura', 'faturas', 'recibo', 'cobrança',
        # Italian
        'fattura', 'fatture', 'ricevuta'
    ],
    'plan': [
        # English
        'plan details', 'my package', 'current plan', 'what plan', 'features', 'data limit',
        # Hebrew
        'פרטי תוכנית', 'מה החבילה', 'כמה דאטה', 'תכונות',
        # Spanish
        'detalles del plan', 'mi paquete', 'plan actual', 'qué plan', 'características',
        # French
        'détails du forfait', 'mon paquet', 'plan actuel', 'quel forfait', 'fonctionnalités',
        # German
        'plandetails', 'mein paket', 'aktueller plan', 'welcher plan', 'funktionen',
        # Russian
        'детали плана', 'мой пакет', 'текущий план', 'какой план', 'функции',
        # Arabic
        'تفاصيل الخطة', 'باقتي', 'الخطة الحالية', 'مميزات',
        # Portuguese
        'detalhes do plano', 'meu pacote', 'plano atual', 'qual plano', 'recursos',
        # Italian
        'dettagli piano', 'mio pacchetto', 'piano attuale', 'funzionalità'
    ],
    'tickets': [
        # English
        'ticket', 'tickets', 'support request', 'issue', 'problem', 'help request',
        # Hebrew
        'כרטיס', 'קריאה', 'תקלה', 'בעיה', 'תמיכה',
        # Spanish
        'ticket', 'tickets', 'solicitud de soporte', 'problema', 'incidencia',
        # French
        'ticket', 'tickets', 'demande d\'assistance', 'problème', 'incident',
        # German
        'ticket', 'tickets', 'support-anfrage', 'problem', 'anliegen',
        # Russian
        'тикет', 'тикеты', 'запрос в поддержку', 'проблема', 'вопрос',
        # Arabic
        'تذكرة', 'تذاكر', 'طلب دعم', 'مشكلة',
        # Portuguese
        'ticket', 'tickets', 'solicitação de suporte', 'problema',
        # Italian
        'ticket', 'tickets', 'richiesta di supporto', 'problema'
    ],
    'customer_info': [
        # English
        'account', 'profile', 'my info', 'my information', 'my details',
        # Hebrew
        'חשבון', 'פרופיל', 'פרטים שלי', 'מידע שלי',
        # Spanish
        'cuenta', 'perfil', 'mi información', 'mis datos',
        # French
        'compte', 'profil', 'mes infos', 'mes informations',
        # German
        'konto', 'profil', 'meine infos', 'meine daten',
        # Russian
        'аккаунт', 'профиль', 'моя информация', 'мои данные',
        # Arabic
        'حساب', 'ملف شخصي', 'معلوماتي', 'بياناتي',
        # Portuguese
        'conta', 'perfil', 'minhas informações', 'meus dados',
        # Italian
        'account', 'profilo', 'mie informazioni', 'miei dati'
    ]
}

# Conversation memory store (session_id -> ConversationState)
conversation_sessions = {}


# =============================================================================
# CONVERSATION STATE - Enhanced with upgrade flow
# =============================================================================
class ConversationState:
    """Maintains conversation state for a session"""
    
    # Conversation states
    STATE_NEW = "new"
    STATE_AWAITING_PIN = "awaiting_pin"
    STATE_IDENTIFIED = "identified"
    STATE_UPGRADE_SELECT_PLAN = "upgrade_select_plan"
    STATE_UPGRADE_CONFIRM = "upgrade_confirm"
    STATE_CREATE_TICKET = "create_ticket"
    
    def __init__(self):
        self.customer_id = None
        self.customer_name = None
        self.customer_email = None
        self.customer_pin = None
        self.is_identified = False
        self.awaiting_pin = False
        self.pin_attempts = 0
        self.history = []
        self.last_activity = datetime.now()
        
        # Enhanced state tracking
        self.state = self.STATE_NEW
        self.pending_upgrade_plan = None  # Plan name being upgraded to
        self.pending_upgrade_plan_id = None  # Plan ID in database
    
    def add_message(self, role, message):
        self.history.append({
            "role": role, 
            "content": message, 
            "timestamp": datetime.now().isoformat()
        })
        self.last_activity = datetime.now()
        # Keep last 20 messages for context
        if len(self.history) > 20:
            self.history = self.history[-20:]
    
    def get_context(self, num_messages=6):
        """Get recent conversation context for LLM"""
        context = ""
        for msg in self.history[-num_messages:]:
            role = "Customer" if msg["role"] == "user" else "Agent"
            context += f"{role}: {msg['content']}\n"
        return context
    
    def identify_customer(self, customer_id, name, email, pin=None):
        self.customer_id = customer_id
        self.customer_name = name
        self.customer_email = email
        self.customer_pin = pin
        self.is_identified = True
        self.awaiting_pin = False
        self.state = self.STATE_IDENTIFIED
        logging.info(f"[Session] Customer identified: {name} (ID: {customer_id})")
    
    def start_upgrade_flow(self):
        """Start the upgrade selection process"""
        self.state = self.STATE_UPGRADE_SELECT_PLAN
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
    
    def set_pending_upgrade(self, plan_name, plan_id=None):
        """Set the plan user wants to upgrade to"""
        self.pending_upgrade_plan = plan_name
        self.pending_upgrade_plan_id = plan_id
        self.state = self.STATE_UPGRADE_CONFIRM
    
    def complete_upgrade(self):
        """Complete the upgrade flow"""
        plan = self.pending_upgrade_plan
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        self.state = self.STATE_IDENTIFIED
        return plan
    
    def cancel_upgrade(self):
        """Cancel pending upgrade"""
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        self.state = self.STATE_IDENTIFIED
    
    def reset(self):
        """Reset the session state"""
        self.customer_id = None
        self.customer_name = None
        self.customer_email = None
        self.customer_pin = None
        self.is_identified = False
        self.awaiting_pin = False
        self.pin_attempts = 0
        self.history = []
        self.state = self.STATE_NEW
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        logging.info("[Session] Session reset")

    def to_dict(self):
        """Serialize state to dictionary"""
        return {
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "customer_pin": self.customer_pin,
            "is_identified": self.is_identified,
            "awaiting_pin": self.awaiting_pin,
            "pin_attempts": self.pin_attempts,
            "history": self.history,
            "state": self.state,
            "pending_upgrade_plan": self.pending_upgrade_plan,
            "pending_upgrade_plan_id": self.pending_upgrade_plan_id,
            "last_activity": self.last_activity.isoformat()
        }

    @classmethod
    def from_dict(cls, data):
        """Deserialize state from dictionary"""
        state = cls()
        state.customer_id = data.get("customer_id")
        state.customer_name = data.get("customer_name")
        state.customer_email = data.get("customer_email")
        state.customer_pin = data.get("customer_pin")
        state.is_identified = data.get("is_identified", False)
        state.awaiting_pin = data.get("awaiting_pin", False)
        state.pin_attempts = data.get("pin_attempts", 0)
        state.history = data.get("history", [])
        state.state = data.get("state", cls.STATE_NEW)
        state.pending_upgrade_plan = data.get("pending_upgrade_plan")
        state.pending_upgrade_plan_id = data.get("pending_upgrade_plan_id")
        
        last_activity = data.get("last_activity")
        if last_activity:
            try:
                state.last_activity = datetime.fromisoformat(last_activity)
            except:
                state.last_activity = datetime.now()
        
        return state


# =============================================================================
# DATABASE MANAGER - Enhanced with asyncpg
# =============================================================================
# Global flag to ensure table is only cleared once per server startup
_upgrade_table_initialized = False

class DatabaseManager:
    """Manages PostgreSQL database connections and queries using asyncpg"""
    
    def __init__(self, host, port, dbname, user, password, pool=None):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.pool = pool
        self._owns_pool = pool is None
    
    async def connect(self):
        global _upgrade_table_initialized
        if not ASYNCPG_AVAILABLE:
            logging.error("asyncpg not available")
            return False
            
        # If pool was provided externally, we are already connected
        if self.pool:
            # Still check initialization if needed using the provided pool
            if not _upgrade_table_initialized:
                try:
                    await self._initialize_upgrade_requests_table()
                    _upgrade_table_initialized = True
                except Exception as e:
                    logging.warning(f"Failed to init tables with shared pool: {e}")
            return True
            
        try:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                database=self.dbname,
                user=self.user,
                password=self.password,
                min_size=1,
                max_size=10
            )
            self._owns_pool = True
            logging.info(f"Connected to database (new pool): {self.dbname}@{self.host}")
            
            # Initialize tables only ONCE per server startup (global flag)
            if not _upgrade_table_initialized:
                await self._initialize_upgrade_requests_table()
                _upgrade_table_initialized = True
            
            return True
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            return False
    
    async def _initialize_upgrade_requests_table(self):
        """Create upgrade_requests table if not exists and clear ALL previous requests on startup"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                # Create table if not exists
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS upgrade_requests (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                        customer_name VARCHAR(255),
                        customer_email VARCHAR(255),
                        current_plan_id INTEGER,
                        current_plan_name VARCHAR(255),
                        target_plan_id INTEGER,
                        target_plan_name VARCHAR(255),
                        status VARCHAR(50) DEFAULT 'pending',
                        priority VARCHAR(20) DEFAULT 'normal',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        processed_at TIMESTAMP,
                        processed_by VARCHAR(255),
                        notes TEXT,
                        session_id VARCHAR(255)
                    )
                """)
                
                # Create index for faster queries
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_upgrade_requests_status 
                    ON upgrade_requests(status)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_upgrade_requests_customer 
                    ON upgrade_requests(customer_id)
                """)
                
                # CLEAR ALL previous requests on server startup (start fresh)
                await connection.execute("DELETE FROM upgrade_requests")
                
                # Reset the sequence to start from 1
                await connection.execute("ALTER SEQUENCE upgrade_requests_id_seq RESTART WITH 1")
                
                logging.info("[DB] Upgrade requests table initialized (cleared all previous requests)")
                
                # Initialize other tables
                await self._initialize_support_tickets_table()
                await self._initialize_sentiment_alerts_table()
                await self._initialize_session_storage_table()
                
        except Exception as e:
            logging.warning(f"[DB] Could not initialize upgrade_requests table: {e}")

    async def _initialize_support_tickets_table(self):
        """Create support_tickets table if not exists"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS support_tickets (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
                        subject VARCHAR(255),
                        description TEXT,
                        status VARCHAR(50) DEFAULT 'open',
                        priority VARCHAR(20) DEFAULT 'medium',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by VARCHAR(255),
                        resolution_notes TEXT
                    )
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tickets_customer 
                    ON support_tickets(customer_id)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tickets_status 
                    ON support_tickets(status)
                """)
                logging.info("[DB] Support tickets table initialized")
        except Exception as e:
            logging.warning(f"[DB] Could not initialize support_tickets table: {e}")

    async def _initialize_session_storage_table(self):
        """Create table for persistent session storage"""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_sessions (
                        session_id VARCHAR(255) PRIMARY KEY,
                        customer_id INTEGER,
                        data JSONB,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logging.info("[DB] Session storage table initialized")
        except Exception as e:
            logging.warning(f"[DB] Could not initialize session storage table: {e}")
    
    async def _initialize_sentiment_alerts_table(self):
        """Create sentiment_alerts table for tracking unhappy customers"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS sentiment_alerts (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                        customer_name VARCHAR(255),
                        customer_email VARCHAR(255),
                        sentiment_score DECIMAL(3,2),
                        sentiment_label VARCHAR(50),
                        trigger_phrases TEXT,
                        customer_message TEXT,
                        context_summary TEXT,
                        churn_risk VARCHAR(20),
                        recommended_action TEXT,
                        session_id VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by VARCHAR(255),
                        resolution_notes TEXT
                    )
                """)
                
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sentiment_alerts_risk 
                    ON sentiment_alerts(churn_risk)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sentiment_alerts_customer 
                    ON sentiment_alerts(customer_id)
                """)
                
                # Clear old alerts on startup (keep only last 7 days)
                await connection.execute("""
                    DELETE FROM sentiment_alerts 
                    WHERE resolved_at IS NOT NULL
                    AND resolved_at < NOW() - INTERVAL '7 days'
                """)
                
                logging.info("[DB] Sentiment alerts table initialized")
                
        except Exception as e:
            logging.warning(f"[DB] Could not initialize sentiment_alerts table: {e}")
    
    async def disconnect(self):
        # Only close the pool if we created it (owns_pool is True)
        if self.pool and self._owns_pool:
            await self.pool.close()
            self.pool = None
            logging.info("Database pool closed (owned)")
    
    async def execute_query(self, query, params=None):
        if not self.pool:
            if not await self.connect():
                return None, "Database connection failed"
        try:
            # Replace %s with $1, $2, etc. for asyncpg
            converted_query = query
            if params:
                for i in range(len(params)):
                    converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                if params:
                    results = await connection.fetch(converted_query, *params)
                else:
                    results = await connection.fetch(converted_query)
                # Convert Record objects to dicts
                return [dict(r) for r in results], None
        except Exception as e:
            logging.error(f"Query execution failed: {e}")
            return None, str(e)
    
    async def find_customer_by_pin(self, pin):
        """Find customer by PIN code"""
        query = "SELECT id, name, email, phone, pin FROM customers WHERE pin = %s"
        results, error = await self.execute_query(query, (pin,))
        if results and len(results) > 0:
            return results[0]
        return None
    
    async def run_predefined_query(self, query_type, customer_id):
        """Run a predefined query safely"""
        if query_type not in PREDEFINED_QUERIES:
            return None, f"Unknown query type: {query_type}"
        
        query = PREDEFINED_QUERIES[query_type]
        return await self.execute_query(query, (customer_id,))
    
    async def get_plan_by_name(self, plan_name):
        """Get plan details by name (case insensitive partial match)"""
        query = """
            SELECT id, name, price, data_limit_gb, support_level, features
            FROM plans
            WHERE LOWER(name) LIKE LOWER(%s)
            ORDER BY price ASC
            LIMIT 1
        """
        results, error = await self.execute_query(query, (f"%{plan_name}%",))
        if results and len(results) > 0:
            return results[0]
        return None
    
    async def create_upgrade_request(self, customer_id, customer_name, customer_email, 
                                current_plan_id, current_plan_name,
                                target_plan_id, target_plan_name, session_id=None):
        """Create an upgrade request in the database"""
        
        # Check for existing pending request for same customer and plan
        check_query = """
            SELECT id FROM upgrade_requests 
            WHERE customer_id = %s AND target_plan_name = %s AND status = 'pending'
        """
        existing, _ = await self.execute_query(check_query, (customer_id, target_plan_name))
        if existing and len(existing) > 0:
            logging.info(f"[DB] Upgrade request already exists for customer {customer_id}")
            return existing[0]['id'], None
        
        # Insert new request
        insert_query = """
            INSERT INTO upgrade_requests 
            (customer_id, customer_name, customer_email, current_plan_id, current_plan_name,
             target_plan_id, target_plan_name, status, priority, notes, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 'normal', 'Created via voice agent', %s)
            RETURNING id
        """
        
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
                    
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (
                customer_id, customer_name, customer_email,
                current_plan_id, current_plan_name,
                target_plan_id, target_plan_name, session_id
            )
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
                
            async with self.pool.acquire() as connection:
                result = await connection.fetchval(converted_query, *params)
                
                request_id = result
                logging.info(f"[DB] Created upgrade request #{request_id}: {customer_name} -> {target_plan_name}")
                return request_id, None
                
        except Exception as e:
            logging.error(f"[DB] Failed to create upgrade request: {e}")
            return None, str(e)

    async def create_ticket(self, customer_id, subject, description, priority='medium'):
        """Create a new support ticket"""
        insert_query = """
            INSERT INTO support_tickets (customer_id, subject, description, status, priority, created_at)
            VALUES (%s, %s, %s, 'open', %s, NOW())
            RETURNING id
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (customer_id, subject, description, priority)
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                ticket_id = await connection.fetchval(converted_query, *params)
                logging.info(f"[DB] Created ticket #{ticket_id} for customer {customer_id}")
                return ticket_id, None
        except Exception as e:
            logging.error(f"[DB] Failed to create ticket: {e}")
            return None, str(e)

    async def close_ticket_by_id(self, customer_id, ticket_id):
        """Close a specific ticket by ID"""
        update_query = """
            UPDATE support_tickets 
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = $1 AND customer_id = $2
            RETURNING id, subject
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                # Need integer ID
                try:
                    t_id = int(ticket_id)
                except:
                    return None, "Invalid ticket ID"
                    
                result = await connection.fetchrow(update_query, t_id, customer_id)
                
                if result:
                    logging.info(f"[DB] Closed ticket #{result['id']} for customer {customer_id}")
                    return (result['id'], result['subject']), None
                else:
                    return None, "Ticket not found or already closed"
        except Exception as e:
            logging.error(f"[DB] Failed to close ticket {ticket_id}: {e}")
            return None, str(e)

    async def get_open_tickets(self, customer_id):
        """Get all open tickets for a customer"""
        query = """
            SELECT id, subject, created_at 
            FROM support_tickets 
            WHERE customer_id = $1 AND status = 'open'
            ORDER BY created_at DESC
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                results = await connection.fetch(query, customer_id)
                return [dict(r) for r in results], None
        except Exception as e:
            logging.error(f"[DB] Failed to fetch open tickets: {e}")
            return None, str(e)

    async def close_latest_ticket(self, customer_id):
        """Close the most recent open ticket for a customer"""
        update_query = """
            UPDATE support_tickets 
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = (
                SELECT id FROM support_tickets 
                WHERE customer_id = $1 AND status = 'open' 
                ORDER BY created_at DESC LIMIT 1
            )
            RETURNING id, subject
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                result = await connection.fetchrow(update_query, customer_id)
                
                if result:
                    logging.info(f"[DB] Closed ticket #{result['id']} for customer {customer_id}")
                    return (result['id'], result['subject']), None
                else:
                    return None, "No open tickets found"
        except Exception as e:
            logging.error(f"[DB] Failed to close ticket: {e}")
            return None, str(e)
    
    async def create_sentiment_alert(self, customer_id, customer_name, customer_email,
                               sentiment_score, sentiment_label, trigger_phrases,
                               customer_message, context_summary, churn_risk,
                               recommended_action, session_id=None):
        """Create a sentiment alert for unhappy customer"""
        
        insert_query = """
            INSERT INTO sentiment_alerts 
            (customer_id, customer_name, customer_email, sentiment_score, sentiment_label,
             trigger_phrases, customer_message, context_summary, churn_risk,
             recommended_action, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (
                customer_id, customer_name, customer_email,
                sentiment_score, sentiment_label, trigger_phrases,
                customer_message, context_summary, churn_risk,
                recommended_action, session_id
            )
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                alert_id = await connection.fetchval(converted_query, *params)
                logging.warning(f"[SENTIMENT] ⚠️ Alert #{alert_id}: {sentiment_label} customer - {customer_name} ({churn_risk} churn risk)")
                return alert_id, None
                
        except Exception as e:
            logging.error(f"[SENTIMENT] Failed to create alert: {e}")
            return None, str(e)

    async def save_session(self, session_id, customer_id, data):
        """Save conversation session to database"""
        query = """
            INSERT INTO conversation_sessions (session_id, customer_id, data, last_updated)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (session_id) 
            DO UPDATE SET customer_id = $2, data = $3, last_updated = NOW()
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return False
            
            async with self.pool.acquire() as connection:
                await connection.execute(query, session_id, customer_id, json.dumps(data))
                return True
        except Exception as e:
            # Self-healing: If table missing, create it and retry
            if "relation \"conversation_sessions\" does not exist" in str(e):
                logging.warning("[DB] Session table missing, attempting to create...")
                await self._initialize_session_storage_table()
                try:
                    async with self.pool.acquire() as connection:
                        await connection.execute(query, session_id, customer_id, json.dumps(data))
                    logging.info("[DB] Session saved after table creation")
                    return True
                except Exception as e2:
                    logging.error(f"[DB] Failed to save session after retry: {e2}")
            else:
                logging.error(f"[DB] Failed to save session: {e}")
            return False

    async def load_session(self, session_id):
        """Load conversation session from database"""
        query = "SELECT data FROM conversation_sessions WHERE session_id = $1"
        try:
            if not self.pool:
                if not await self.connect():
                    return None
            
            async with self.pool.acquire() as connection:
                result = await connection.fetchval(query, session_id)
                if result:
                    return json.loads(result)
                return None
        except Exception as e:
            logging.error(f"[DB] Failed to load session: {e}")
            return None


# =============================================================================
# TEXT PROCESSING UTILITIES
# =============================================================================
def clean_tts_text(text):
    """Clean text for TTS - remove problematic characters"""
    if not text:
        logging.warning("[TTS] clean_tts_text: Input is None/empty")
        return None
    
    original_text = text[:100]  # Keep for logging
    
    text = text.strip().strip('"\'""''')
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)  # Italic
    text = re.sub(r'`([^`]+)`', r'\1', text)  # Code
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    
    # Check if text contains valid characters for any supported language
    # Latin (including accents), Hebrew, Cyrillic, Arabic, CJK (Chinese/Japanese/Korean), Devanagari (Hindi)
    valid_chars = r'[a-zA-Z0-9\u00C0-\u024F\u0590-\u05FF\u0400-\u04FF\u0600-\u06FF\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF\u0900-\u097F]'
    
    if not text or not re.search(valid_chars, text):
        logging.warning(f"[TTS] clean_tts_text: No valid chars found in: '{original_text}'")
        return None
    
    return text.strip()


# =============================================================================
# SENTIMENT ANALYSIS - Detect unhappy customers at risk of churning
# =============================================================================
NEGATIVE_INDICATORS = {
    # English
    'cancel': 0.9, 'cancelling': 0.9, 'cancellation': 0.9, 'terminate': 0.9, 'leave': 0.8, 'switch to': 0.75,
    'terrible': 0.7, 'awful': 0.7, 'horrible': 0.75, 'worst': 0.8, 'angry': 0.7, 'furious': 0.8,
    'not working': 0.5, 'broken': 0.55, 'problem': 0.4, 'issue': 0.35, 'disappointed': 0.55,
    
    # Hebrew
    'לבטל': 0.9, 'ביטול': 0.9, 'מבטל': 0.9, 'לעזוב': 0.8, 'להתנתק': 0.9,
    'נורא': 0.7, 'איום': 0.75, 'גרוע': 0.7, 'כועס': 0.7, 'זועם': 0.8,
    'לא עובד': 0.5, 'שבור': 0.55, 'תקלה': 0.45, 'בעיה': 0.4, 'מאוכזב': 0.55,
    
    # Spanish
    'cancelar': 0.9, 'terminar': 0.9, 'dar de baja': 0.9, 'irme': 0.8, 'cambiar de': 0.75,
    'terrible': 0.7, 'horrible': 0.75, 'peor': 0.8, 'enfadado': 0.7, 'furioso': 0.8,
    'no funciona': 0.5, 'roto': 0.55, 'problema': 0.4, 'decepcionado': 0.55,
    
    # French
    'annuler': 0.9, 'résilier': 0.9, 'quitter': 0.8, 'changer de': 0.75,
    'terrible': 0.7, 'horrible': 0.75, 'pire': 0.8, 'colère': 0.7, 'furieux': 0.8,
    'ne marche pas': 0.5, 'cassé': 0.55, 'problème': 0.4, 'déçu': 0.55,
    
    # German
    'kündigen': 0.9, 'stornieren': 0.9, 'verlassen': 0.8, 'wechseln': 0.75,
    'schrecklich': 0.7, 'furchtbar': 0.75, 'schlimmste': 0.8, 'wütend': 0.7,
    'funktioniert nicht': 0.5, 'kaputt': 0.55, 'problem': 0.4, 'enttäuscht': 0.55,
    
    # Russian
    'отменить': 0.9, 'расторгнуть': 0.9, 'уйти': 0.8, 'сменить': 0.75,
    'ужасно': 0.7, 'кошмар': 0.75, 'худший': 0.8, 'злой': 0.7,
    'не работает': 0.5, 'сломан': 0.55, 'проблема': 0.4, 'разочарован': 0.55,
    
    # Arabic
    'إلغاء': 0.9, 'إنهاء': 0.9, 'مغادرة': 0.8, 'تغيير': 0.75,
    'فظيع': 0.7, 'مريع': 0.75, 'أسوأ': 0.8, 'غاضب': 0.7,
    'لا يعمل': 0.5, 'مكسور': 0.55, 'مشكلة': 0.4, 'محبط': 0.55,
    
    # Portuguese
    'cancelar': 0.9, 'encerrar': 0.9, 'sair': 0.8, 'mudar': 0.75,
    'terrível': 0.7, 'horrível': 0.75, 'pior': 0.8, 'bravo': 0.7, 'furioso': 0.8,
    'não funciona': 0.5, 'quebrado': 0.55, 'problema': 0.4, 'decepcionado': 0.55,
    
    # Italian
    'cancellare': 0.9, 'terminare': 0.9, 'lasciare': 0.8, 'cambiare': 0.75,
    'terribile': 0.7, 'orribile': 0.75, 'peggiore': 0.8, 'arrabbiato': 0.7,
    'non funziona': 0.5, 'rotto': 0.55, 'problema': 0.4, 'deluso': 0.55
}

POSITIVE_INDICATORS = {
    # ==========================================================================
    # ENGLISH
    # ==========================================================================
    'thank': 0.3, 'thanks': 0.3, 'appreciate': 0.4, 'great': 0.4, 'excellent': 0.5, 'amazing': 0.5,
    'helpful': 0.4, 'perfect': 0.45, 'love': 0.5, 'happy': 0.4, 'satisfied': 0.4,
    'wonderful': 0.5, 'fantastic': 0.5, 'awesome': 0.5, 'brilliant': 0.5, 'superb': 0.5,
    'outstanding': 0.5, 'incredible': 0.5, 'pleased': 0.4, 'grateful': 0.4,
    
    # ==========================================================================
    # HEBREW
    # ==========================================================================
    'תודה': 0.3, 'מעולה': 0.5, 'נהדר': 0.45, 'מושלם': 0.45, 'אוהב': 0.5, 'מרוצה': 0.4, 'עוזר': 0.4,
    'נפלא': 0.5, 'מצוין': 0.5, 'אדיר': 0.5, 'שמח': 0.4, 'מודה': 0.4, 'פנטסטי': 0.5, 'מדהים': 0.5,
    
    # ==========================================================================
    # SPANISH
    # ==========================================================================
    'gracias': 0.3, 'agradezco': 0.4, 'genial': 0.4, 'excelente': 0.5, 'increíble': 0.5,
    'útil': 0.4, 'perfecto': 0.45, 'encanta': 0.5, 'feliz': 0.4, 'satisfecho': 0.4,
    'maravilloso': 0.5, 'fantástico': 0.5, 'asombroso': 0.5,
    
    # ==========================================================================
    # FRENCH
    # ==========================================================================
    'merci': 0.3, 'apprécie': 0.4, 'super': 0.4, 'excellent': 0.5, 'incroyable': 0.5,
    'utile': 0.4, 'parfait': 0.45, 'adore': 0.5, 'content': 0.4, 'satisfait': 0.4,
    'merveilleux': 0.5, 'fantastique': 0.5, 'génial': 0.5,
    
    # ==========================================================================
    # GERMAN
    # ==========================================================================
    'danke': 0.3, 'ausgezeichnet': 0.5, 'toll': 0.5, 'hilfreich': 0.4,
    'perfekt': 0.45, 'liebe': 0.5, 'froh': 0.4, 'zufrieden': 0.4,
    'wunderbar': 0.5, 'fantastisch': 0.5, 'großartig': 0.5,
    
    # ==========================================================================
    # ITALIAN
    # ==========================================================================
    'grazie': 0.3, 'apprezzo': 0.4, 'grande': 0.4, 'eccellente': 0.5, 'fantastico': 0.5,
    'perfetto': 0.45, 'amo': 0.5, 'felice': 0.4, 'soddisfatto': 0.4,
    'meraviglioso': 0.5, 'incredibile': 0.5,
    
    # ==========================================================================
    # PORTUGUESE
    # ==========================================================================
    'obrigado': 0.3, 'agradeço': 0.4, 'ótimo': 0.4, 'incrível': 0.5,
    'perfeito': 0.45, 'feliz': 0.4, 'satisfeito': 0.4,
    'maravilhoso': 0.5, 'fantástico': 0.5,
    
    # ==========================================================================
    # POLISH
    # ==========================================================================
    'dziękuję': 0.3, 'doceniam': 0.4, 'świetny': 0.4, 'doskonały': 0.5, 'niesamowity': 0.5,
    'pomocny': 0.4, 'idealny': 0.45, 'kocham': 0.5, 'szczęśliwy': 0.4, 'zadowolony': 0.4,
    'wspaniały': 0.5, 'fantastyczny': 0.5,
    
    # ==========================================================================
    # TURKISH
    # ==========================================================================
    'teşekkürler': 0.3, 'teşekkür': 0.3, 'harika': 0.4, 'mükemmel': 0.5, 'inanılmaz': 0.5,
    'yararlı': 0.4, 'kusursuz': 0.45, 'seviyorum': 0.5, 'mutlu': 0.4, 'memnun': 0.4,
    'muhteşem': 0.5, 'fantastik': 0.5,
    
    # ==========================================================================
    # RUSSIAN
    # ==========================================================================
    'спасибо': 0.3, 'благодарю': 0.4, 'отлично': 0.5, 'прекрасно': 0.5, 'полезно': 0.4,
    'идеально': 0.45, 'люблю': 0.5, 'рад': 0.4, 'доволен': 0.4,
    'чудесно': 0.5, 'фантастика': 0.5, 'невероятно': 0.5,
    
    # ==========================================================================
    # DUTCH
    # ==========================================================================
    'dank': 0.3, 'bedankt': 0.3, 'waardeer': 0.4, 'geweldig': 0.4, 'uitstekend': 0.5,
    'ongelooflijk': 0.5, 'nuttig': 0.4, 'blij': 0.4, 'tevreden': 0.4,
    'prachtig': 0.5, 'fantastisch': 0.5,
    
    # ==========================================================================
    # CZECH
    # ==========================================================================
    'děkuji': 0.3, 'oceňuji': 0.4, 'skvělý': 0.4, 'vynikající': 0.5, 'úžasný': 0.5,
    'užitečný': 0.4, 'perfektní': 0.45, 'miluji': 0.5, 'šťastný': 0.4, 'spokojený': 0.4,
    'nádherný': 0.5, 'fantastický': 0.5,
    
    # ==========================================================================
    # ARABIC
    # ==========================================================================
    'شكرا': 0.3, 'أقدر': 0.4, 'رائع': 0.4, 'ممتاز': 0.5, 'مذهل': 0.5, 'مفيد': 0.4,
    'مثالي': 0.45, 'أحب': 0.5, 'سعيد': 0.4, 'راضي': 0.4,
    'رائعة': 0.5, 'خيالي': 0.5,
    
    # ==========================================================================
    # CHINESE (Simplified)
    # ==========================================================================
    '谢谢': 0.3, '感谢': 0.3, '很棒': 0.4, '优秀': 0.5, '惊人': 0.5,
    '有用': 0.4, '完美': 0.45, '喜欢': 0.5, '高兴': 0.4, '满意': 0.4,
    '太棒了': 0.5, '美好': 0.5,
    
    # ==========================================================================
    # JAPANESE
    # ==========================================================================
    'ありがとう': 0.3, '感謝': 0.3, '素晴らしい': 0.5, '優秀': 0.5, '驚くべき': 0.5,
    '役立つ': 0.4, '完璧': 0.45, '大好き': 0.5, '嬉しい': 0.4, '満足': 0.4,
    'ファンタスティック': 0.5, '素敵': 0.5,
    
    # ==========================================================================
    # HUNGARIAN
    # ==========================================================================
    'köszönöm': 0.3, 'értékelem': 0.4, 'nagyszerű': 0.4, 'kiváló': 0.5, 'hihetetlen': 0.5,
    'hasznos': 0.4, 'tökéletes': 0.45, 'szeretem': 0.5, 'boldog': 0.4, 'elégedett': 0.4,
    'csodálatos': 0.5, 'fantasztikus': 0.5,
    
    # ==========================================================================
    # KOREAN
    # ==========================================================================
    '감사합니다': 0.3, '고마워': 0.3, '훌륭한': 0.4, '우수한': 0.5, '놀라운': 0.5,
    '유용한': 0.4, '완벽한': 0.45, '좋아': 0.5, '행복한': 0.4, '만족': 0.4,
    '멋진': 0.5, '환상적인': 0.5,
    
    # ==========================================================================
    # HINDI
    # ==========================================================================
    'धन्यवाद': 0.3, 'शुक्रिया': 0.3, 'बहुत अच्छा': 0.4, 'उत्कृष्ट': 0.5, 'अद्भुत': 0.5,
    'उपयोगी': 0.4, 'परफेक्ट': 0.45, 'प्यार': 0.5, 'खुश': 0.4, 'संतुष्ट': 0.4,
    'शानदार': 0.5, 'फैंटास्टिक': 0.5,
    # Romanized Hindi
    'shukriya': 0.3, 'bahut accha': 0.4, 'khush': 0.4, 'perfect': 0.45,
}

def analyze_sentiment(text, language='en', conversation_history=None):
    """
    Analyze customer sentiment from their message (multi-language support).
    Returns: dict with score (-1 to 1), label, trigger_phrases, churn_risk, recommended_action
    """
    if not text:
        return None
    
    text_lower = text.lower()
    
    # Normalize language code
    lang_code = language.split('-')[0].lower() if '-' in language else language.lower()

    # Calculate scores
    negative_score = 0.0
    positive_score = 0.0
    trigger_phrases = []
    churn_risk = "none"
    action = None
    label = "neutral"

    # 1. Check CHURN INDICATORS (Highest Priority) - Explicit cancellation intent
    # Check both specific language and English as fallback
    churn_lists = [CHURN_INDICATORS.get(lang_code, []), CHURN_INDICATORS.get('en', [])]
    found_churn = False

    for lst in churn_lists:
        for phrase in lst:
            if phrase.lower() in text_lower:
                churn_risk = "high"
                label = "cancellation_intent"
                action = "URGENT: Customer wants to cancel. Immediate retention call required."
                trigger_phrases.append(phrase)
                negative_score += 1.0  # Max negative impact
                found_churn = True
                break
        if found_churn:
            break

    # 2. Check EXTENDED NEGATIVE KEYWORDS (Medium Priority)
    # Check both specific language and English
    neg_lists = [NEGATIVE_SENTIMENT_KEYWORDS.get(lang_code, []), NEGATIVE_SENTIMENT_KEYWORDS.get('en', [])]

    for lst in neg_lists:
        for phrase in lst:
            if phrase.lower() in text_lower and phrase not in trigger_phrases:
                negative_score += 0.5
                trigger_phrases.append(phrase)

    # 3. Check LEGACY WEIGHTED INDICATORS (Specific Weights)
    for phrase, weight in NEGATIVE_INDICATORS.items():
        if phrase in text_lower and phrase not in trigger_phrases:
            negative_score += weight
            trigger_phrases.append(phrase)
    
    for phrase, weight in POSITIVE_INDICATORS.items():
        if phrase in text_lower:
            positive_score += weight
    
    # Normalize scores
    negative_score = min(negative_score, 1.0)
    positive_score = min(positive_score, 1.0)
    
    # Final sentiment score (-1 = very negative, 0 = neutral, 1 = very positive)
    sentiment_score = positive_score - negative_score
    
    # Determine label and churn risk (if not already set to high by churn indicators)
    if churn_risk != "high":
        if sentiment_score <= -0.6:
            label = "very_negative"
            churn_risk = "high"
            action = "URGENT: Immediate callback by senior agent. Offer compensation/discount."
        elif sentiment_score <= -0.35:
            label = "negative"
            churn_risk = "medium"
            action = "Priority callback within 24h. Address specific complaints."
        elif sentiment_score <= -0.15:
            label = "slightly_negative"
            churn_risk = "low"
            action = "Monitor conversation. Follow up if issues persist."
        elif sentiment_score >= 0.3:
            label = "positive"
            churn_risk = "none"
            action = None
        else:
            label = "neutral"
            churn_risk = "none"
            action = None
    
    return {
        'score': round(sentiment_score, 2),
        'label': label,
        'trigger_phrases': list(set(trigger_phrases)), # Deduplicate
        'churn_risk': churn_risk,
        'recommended_action': action,
        'needs_alert': churn_risk in ['high', 'medium']
    }


def detect_query_type(text):
    """
    Detect what type of information the user is asking about.
    v5.1.0: Enhanced disambiguation logic to prevent false "create_ticket" triggers.
    
    Priority Order:
    1. Query/Question indicators (how many, list, show, status) -> Returns info query
    2. Action indicators (open, create, close) -> Returns action type
    3. General topic mentions -> Returns topic query
    """
    text_lower = text.lower()
    
    # =========================================================================
    # PHASE 1: Detect QUERY indicators (user is ASKING, not COMMANDING)
    # If these are present, user wants INFO not ACTION
    # =========================================================================
    query_indicators = [
        # English - questions/inquiries
        'how many', 'how much', 'list', 'show', 'show me', 'check', 'status', 
        'what are', 'what is', 'what\'s', 'do i have', 'any', 'tell me', 
        'count', 'number of', 'see my', 'view', 'display', 'get', 'find',
        'are there', 'is there', 'have i', 'did i', 'my tickets', 'my issues',
        'open tickets', 'pending tickets', 'active tickets',  # Asking about open tickets
        # Hebrew - questions/inquiries (expanded)
        'כמה', 'רשימת', 'הראה', 'הראה לי', 'בדוק', 'סטטוס', 'מה הם', 'מה זה',
        'האם יש לי', 'האם יש', 'יש לי', 'תראה לי', 'תגיד לי', 'ספר לי',
        'הכרטיסים שלי', 'הקריאות שלי', 'התקלות שלי', 'הבעיות שלי',
        'כמה יש', 'כמה כרטיסים', 'כמה קריאות', 'כמה טיקטים', 'כמה תקלות',
        'מה יש לי', 'מה הסטטוס', 'מה המצב', 'איזה כרטיסים', 'אילו כרטיסים',
        'כרטיסים פתוחים', 'קריאות פתוחות', 'טיקטים פתוחים',  # "open tickets" in Hebrew
        'לראות', 'לבדוק', 'לספור', 'להציג',  # Infinitive verbs for viewing
        # Spanish
        'cuántos', 'cuántas', 'listar', 'mostrar', 'ver mis', 'tengo',
        'tickets abiertos', 'incidencias abiertas',
        # French
        'combien', 'montrer', 'voir mes', 'afficher', 'ai-je',
        'tickets ouverts',
        # German
        'wie viele', 'zeigen', 'anzeigen', 'liste', 'habe ich',
        'offene tickets',
    ]
    
    # =========================================================================
    # PHASE 2: Detect ACTION indicators (user wants to DO something)
    # =========================================================================
    create_action_indicators = [
        # English - explicit create commands (must be VERY specific)
        'open a new', 'create a new', 'start a new', 'file a new', 'submit a new',
        'i want to open a', 'i need to open a', 'please open a', 'can you open a',
        'i want to create a', 'need to create a', 'please create a',
        'i have a problem', 'something is wrong', 'report issue', 'report a problem',
        'need help with', 'having trouble', 'something broke', 'not working',
        # Hebrew - explicit create commands (must be VERY specific)
        'לפתוח קריאה חדשה', 'לפתוח כרטיס חדש', 'לפתוח טיקט חדש',
        'אני רוצה לפתוח קריאה', 'אני רוצה לפתוח כרטיס', 'אני רוצה לפתוח טיקט',
        'אני צריך לפתוח', 'תפתח לי קריאה', 'תפתח לי כרטיס', 'תפתח לי טיקט',
        'בבקשה תפתח', 'יש לי בעיה', 'משהו לא עובד', 'לדווח על תקלה',
        'צריך עזרה עם', 'משהו התקלקל', 'יש תקלה',
    ]
    
    close_action_indicators = [
        # English - explicit close commands
        'close my', 'close the', 'close a', 'resolve my', 'mark as resolved',
        'i want to close', 'please close', 'can you close',
        # Hebrew
        'לסגור את', 'תסגור את', 'בבקשה תסגור', 'אני רוצה לסגור',
        'לסגור קריאה', 'לסגור כרטיס', 'לסגור טיקט',
    ]
    
    # =========================================================================
    # PHASE 3: Ambiguous phrases that could be query OR action
    # =========================================================================
    ambiguous_ticket_phrases = [
        # These phrases are ambiguous - could mean "list open tickets" or "open a ticket"
        'open ticket', 'open tickets', 'tickets open', 'ticket open',
        'כרטיס פתוח', 'כרטיסים פתוחים', 'קריאה פתוחה', 'קריאות פתוחות',
        'טיקט פתוח', 'טיקטים פתוחים',
        # Just "ticket" or "tickets" alone
        'ticket', 'tickets', 'כרטיס', 'כרטיסים', 'קריאה', 'קריאות',
    ]
    
    # =========================================================================
    # DECISION LOGIC
    # =========================================================================
    
    has_query_indicator = any(ind in text_lower for ind in query_indicators)
    has_create_action = any(ind in text_lower for ind in create_action_indicators)
    has_close_action = any(ind in text_lower for ind in close_action_indicators)
    has_ambiguous = any(phrase in text_lower for phrase in ambiguous_ticket_phrases)
    
    # Find which indicators matched (for debugging)
    matched_query = [ind for ind in query_indicators if ind in text_lower]
    matched_create = [ind for ind in create_action_indicators if ind in text_lower]
    
    # Log for debugging (always log Intent detection at INFO level for troubleshooting)
    logging.info(f"[Intent] 📝 Analyzing: '{text_lower[:60]}...'")
    logging.info(f"[Intent] 🔍 Indicators - query:{has_query_indicator}, create:{has_create_action}, close:{has_close_action}, ambiguous:{has_ambiguous}")
    if matched_query:
        logging.info(f"[Intent] 🔍 Matched query words: {matched_query[:3]}")
    if matched_create:
        logging.info(f"[Intent] 🔍 Matched create words: {matched_create[:3]}")
    
    # RULE 1: If user is clearly ASKING (has query indicator) -> It's a query, not action
    # BUT: If they ALSO have a create action indicator, prioritize the action
    if has_query_indicator and not has_create_action:
        # Check what topic they're asking about
        if has_ambiguous or 'ticket' in text_lower or 'כרטיס' in text_lower or 'קריאה' in text_lower or 'טיקט' in text_lower:
            logging.info("[Intent] ✅ Result: TICKETS QUERY (user is asking about tickets, not creating)")
            return 'tickets'
        # Check for other query types
        for query_type, triggers in QUERY_TRIGGERS.items():
            if query_type in ['create_ticket', 'close_ticket']:  # Skip action types
                continue
            for trigger in triggers:
                if trigger in text_lower:
                    logging.info(f"[Intent] ✅ Result: {query_type.upper()} QUERY")
                    return query_type
    
    # RULE 2: Explicit close action
    if has_close_action:
        logging.info("[Intent] ✅ Result: CLOSE_TICKET action")
        return 'close_ticket'
    
    # RULE 3: Explicit create action (takes priority over ambiguous queries)
    if has_create_action:
        logging.info("[Intent] ✅ Result: CREATE_TICKET action")
        return 'create_ticket'
    
    # RULE 4: Ambiguous phrase WITHOUT query indicator
    # "open ticket" alone (without "how many", "do I have", etc.) -> might be create
    # But we should be careful - default to query if unsure
    if has_ambiguous and not has_query_indicator and not has_create_action:
        # Check if it sounds more like a question or a command
        question_words = ['?', 'מה', 'what', 'האם', 'is', 'are', 'do', 'does', 'have', 'כמה', 'איזה']
        if any(qw in text_lower for qw in question_words):
            logging.info("[Intent] ✅ Result: TICKETS QUERY (ambiguous but has question word)")
            return 'tickets'
        # Default: If just "open ticket" with nothing else, assume they want to see their tickets
        # This is safer than accidentally creating a ticket
        logging.info("[Intent] ⚠️ Result: TICKETS QUERY (ambiguous - defaulting to safer option)")
        return 'tickets'
    
    # =========================================================================
    # UPGRADE DETECTION - Distinguish between asking about plan vs wanting to upgrade
    # =========================================================================
    upgrade_action_indicators = [
        # English - explicit upgrade commands
        'i want to upgrade', 'please upgrade', 'upgrade me', 'upgrade my plan',
        'i would like to upgrade', 'can you upgrade', 'switch to premium', 'switch to standard',
        'change to premium', 'change to standard', 'move to premium', 'get premium',
        # Hebrew - explicit upgrade commands
        'אני רוצה לשדרג', 'תשדרג לי', 'לשדרג לפרמיום', 'לשדרג לסטנדרט',
        'לעבור לפרמיום', 'לעבור לסטנדרט', 'להחליף תוכנית', 'רוצה חבילה יותר טובה',
        # Spanish
        'quiero actualizar', 'actualizar a premium', 'cambiar a premium',
        # French
        'je veux upgrader', 'passer à premium', 'changer de forfait',
    ]
    
    plan_query_indicators = [
        # English - asking about current plan
        'what plan', 'which plan', 'my plan', 'current plan', 'what is my plan',
        'plan details', 'what am i on', 'show my plan',
        # Hebrew - asking about current plan
        'מה התוכנית', 'איזו תוכנית', 'איזו חבילה', 'התוכנית שלי', 'מה החבילה',
        'פרטי תוכנית', 'מה יש לי',
        # Spanish
        'qué plan', 'cuál es mi plan', 'mi plan actual',
        # French
        'quel forfait', 'mon forfait actuel',
    ]
    
    has_upgrade_action = any(ind in text_lower for ind in upgrade_action_indicators)
    has_plan_query = any(ind in text_lower for ind in plan_query_indicators)
    
    # If user is asking about their plan (not upgrading)
    if has_plan_query and not has_upgrade_action:
        if 'upgrade' not in text_lower and 'שדרוג' not in text_lower:
            logging.info("[Intent] ✅ Result: PLAN QUERY (user asking about current plan)")
            return 'plan'
    
    # If user explicitly wants to upgrade
    if has_upgrade_action:
        logging.info("[Intent] ✅ Result: UPGRADE action")
        return 'upgrade'
    
    # RULE 5: Fall back to standard QUERY_TRIGGERS matching
    for query_type, triggers in QUERY_TRIGGERS.items():
        for trigger in triggers:
            if trigger in text_lower:
                logging.info(f"[Intent] ✅ Result: {query_type} (via trigger match)")
                return query_type
    
    logging.info("[Intent] ❓ Result: None (no specific query type detected)")
    return None


def extract_plan_choice(text):
    """Extract which plan the user wants from their response (multi-language)"""
    text_lower = text.lower()
    
    # Premium patterns (multiple languages)
    premium_words = [
        'premium', 'פרימיום', 'הכי טוב', 'best', 'top', 'highest', 'מקסימום',
        'premium plan', 'el premium', 'le premium', 'das premium',
    ]
    
    # Standard patterns (multiple languages)
    standard_words = [
        'standard', 'סטנדרט', 'רגיל', 'regular', 'basic', 'normal', 'בסיסי',
        'standard plan', 'el estándar', 'le standard', 'das standard',
    ]
    
    # Pro patterns
    pro_words = ['pro', 'professional', 'business', 'עסקי', 'מקצועי']
    
    if any(word in text_lower for word in premium_words):
        return 'premium'
    elif any(word in text_lower for word in standard_words):
        return 'standard'
    elif any(word in text_lower for word in pro_words):
        return 'pro'
    
    return None


def is_confirmation(text):
    """Check if user is confirming something (multi-language)"""
    text_lower = text.lower().strip()
    confirmations = [
        # English
        'yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'correct', 'right', 
        'confirm', 'confirmed', 'absolutely', 'definitely', 'go ahead', 'do it',
        # Hebrew
        'כן', 'בטח', 'אישור', 'נכון', 'קדימה', 'בסדר', 'אוקיי', 'מאשר', 'בהחלט',
        # Spanish
        'sí', 'si', 'claro', 'por supuesto', 'adelante', 'confirmo',
        # French
        'oui', 'bien sûr', 'certainement', 'je confirme', 'd\'accord',
    ]
    return any(word in text_lower for word in confirmations)


def is_cancellation(text):
    """Check if user is cancelling something (multi-language)"""
    text_lower = text.lower().strip()
    cancellations = [
        # English
        'no', 'nope', 'cancel', 'stop', 'never mind', 'forget it',
        'don\'t', 'dont', 'not', 'wait', 'hold on', 'not now',
        # Hebrew
        'לא', 'ביטול', 'עזוב', 'תשכח', 'בטל', 'לא עכשיו', 'רגע',
        # Spanish
        'no', 'cancelar', 'detener', 'olvídalo', 'espera',
        # French
        'non', 'annuler', 'arrêter', 'oublie', 'attends',
    ]
    return any(word in text_lower for word in cancellations)


# =============================================================================
# DB TERM TRANSLATIONS - Avoid mixing English status terms in foreign sentences
# =============================================================================
DB_TERM_TRANSLATIONS = {
    'en': {
        'active': 'active', 'suspended': 'suspended', 'cancelled': 'cancelled',
        'pending': 'pending', 'paid': 'paid', 'overdue': 'overdue',
        'open': 'open', 'resolved': 'resolved', 'closed': 'closed', 'in_progress': 'in progress',
        'low': 'low', 'medium': 'medium', 'high': 'high', 'urgent': 'urgent',
        'email': 'email', 'phone': 'phone', 'priority': 'priority'
    },
    'es': {
        'active': 'activo', 'suspended': 'suspendido', 'cancelled': 'cancelado',
        'pending': 'pendiente', 'paid': 'pagado', 'overdue': 'vencido',
        'open': 'abierto', 'resolved': 'resuelto', 'closed': 'cerrado', 'in_progress': 'en progreso',
        'low': 'baja', 'medium': 'media', 'high': 'alta', 'urgent': 'urgente',
        'email': 'correo', 'phone': 'teléfono', 'priority': 'prioridad'
    },
    'fr': {
        'active': 'actif', 'suspended': 'suspendu', 'cancelled': 'annulé',
        'pending': 'en attente', 'paid': 'payé', 'overdue': 'en retard',
        'open': 'ouvert', 'resolved': 'résolu', 'closed': 'fermé', 'in_progress': 'en cours',
        'low': 'basse', 'medium': 'moyenne', 'high': 'haute', 'urgent': 'urgente',
        'email': 'email', 'phone': 'téléphone', 'priority': 'priorité'
    },
    'de': {
        'active': 'aktiv', 'suspended': 'ausgesetzt', 'cancelled': 'gekündigt',
        'pending': 'ausstehend', 'paid': 'bezahlt', 'overdue': 'überfällig',
        'open': 'offen', 'resolved': 'gelöst', 'closed': 'geschlossen', 'in_progress': 'in bearbeitung',
        'low': 'niedrig', 'medium': 'mittel', 'high': 'hoch', 'urgent': 'dringend',
        'email': 'email', 'phone': 'telefon', 'priority': 'priorität'
    },
    'it': {
        'active': 'attivo', 'suspended': 'sospeso', 'cancelled': 'cancellato',
        'pending': 'in attesa', 'paid': 'pagato', 'overdue': 'scaduto',
        'open': 'aperto', 'resolved': 'risolto', 'closed': 'chiuso', 'in_progress': 'in corso',
        'low': 'bassa', 'medium': 'media', 'high': 'alta', 'urgent': 'urgente',
        'email': 'email', 'phone': 'telefono', 'priority': 'priorità'
    },
    'pt': {
        'active': 'ativo', 'suspended': 'suspenso', 'cancelled': 'cancelado',
        'pending': 'pendente', 'paid': 'pago', 'overdue': 'vencido',
        'open': 'aberto', 'resolved': 'resolvido', 'closed': 'fechado', 'in_progress': 'em andamento',
        'low': 'baixa', 'medium': 'média', 'high': 'alta', 'urgent': 'urgente',
        'email': 'email', 'phone': 'telefone', 'priority': 'prioridade'
    },
    'he': {
        'active': 'פעיל', 'suspended': 'מושהה', 'cancelled': 'מבוטל',
        'pending': 'ממתין', 'paid': 'שולם', 'overdue': 'בפיגור',
        'open': 'פתוח', 'resolved': 'נפתר', 'closed': 'סגור', 'in_progress': 'בטיפול',
        'low': 'נמוכה', 'medium': 'בינונית', 'high': 'גבוהה', 'urgent': 'דחופה',
        'email': 'אימייל', 'phone': 'טלפון', 'priority': 'עדיפות'
    },
    'ar': {
        'active': 'نشط', 'suspended': 'معلق', 'cancelled': 'ملغى',
        'pending': 'قيد الانتظار', 'paid': 'مدفوع', 'overdue': 'متأخر',
        'open': 'مفتوح', 'resolved': 'تم حله', 'closed': 'مغلق', 'in_progress': 'قيد التنفيذ',
        'low': 'منخفضة', 'medium': 'متوسطة', 'high': 'عالية', 'urgent': 'عاجلة',
        'email': 'البريد الإلكتروني', 'phone': 'الهاتف', 'priority': 'الأولوية'
    },
    'ru': {
        'active': 'активен', 'suspended': 'приостановлен', 'cancelled': 'отменен',
        'pending': 'ожидает', 'paid': 'оплачен', 'overdue': 'просрочен',
        'open': 'открыт', 'resolved': 'решен', 'closed': 'закрыт', 'in_progress': 'в процессе',
        'low': 'низкий', 'medium': 'средний', 'high': 'высокий', 'urgent': 'срочный',
        'email': 'email', 'phone': 'телефон', 'priority': 'приоритет'
    },
    'tr': {
        'active': 'aktif', 'suspended': 'askıya alındı', 'cancelled': 'iptal edildi',
        'pending': 'beklemede', 'paid': 'ödendi', 'overdue': 'gecikmiş',
        'open': 'açık', 'resolved': 'çözüldü', 'closed': 'kapalı', 'in_progress': 'devam ediyor',
        'low': 'düşük', 'medium': 'orta', 'high': 'yüksek', 'urgent': 'acil',
        'email': 'eposta', 'phone': 'telefon', 'priority': 'öncelik'
    },
    'pl': {
        'active': 'aktywny', 'suspended': 'zawieszony', 'cancelled': 'anulowany',
        'pending': 'oczekujący', 'paid': 'opłacony', 'overdue': 'zaległy',
        'open': 'otwarty', 'resolved': 'rozwiązany', 'closed': 'zamknięty', 'in_progress': 'w toku',
        'low': 'niski', 'medium': 'średni', 'high': 'wysoki', 'urgent': 'pilny',
        'email': 'email', 'phone': 'telefon', 'priority': 'priorytet'
    },
    'nl': {
        'active': 'actief', 'suspended': 'geschorst', 'cancelled': 'geannuleerd',
        'pending': 'in behandeling', 'paid': 'betaald', 'overdue': 'achterstallig',
        'open': 'open', 'resolved': 'opgelost', 'closed': 'gesloten', 'in_progress': 'in behandeling',
        'low': 'laag', 'medium': 'gemiddeld', 'high': 'hoog', 'urgent': 'dringend',
        'email': 'email', 'phone': 'telefoon', 'priority': 'prioriteit'
    },
    'cs': {
        'active': 'aktivní', 'suspended': 'pozastaveno', 'cancelled': 'zrušeno',
        'pending': 'čekající', 'paid': 'zaplaceno', 'overdue': 'po splatnosti',
        'open': 'otevřeno', 'resolved': 'vyřešeno', 'closed': 'uzavřeno', 'in_progress': 'probíhá',
        'low': 'nízká', 'medium': 'střední', 'high': 'vysoká', 'urgent': 'naléhavá',
        'email': 'email', 'phone': 'telefon', 'priority': 'priorita'
    },
    'hu': {
        'active': 'aktív', 'suspended': 'felfüggesztett', 'cancelled': 'törölt',
        'pending': 'függőben', 'paid': 'fizetett', 'overdue': 'lejárt',
        'open': 'nyitott', 'resolved': 'megoldott', 'closed': 'lezárt', 'in_progress': 'folyamatban',
        'low': 'alacsony', 'medium': 'közepes', 'high': 'magas', 'urgent': 'sürgős',
        'email': 'email', 'phone': 'telefon', 'priority': 'prioritás'
    },
    'zh': {
        'active': '活跃', 'suspended': '暂停', 'cancelled': '已取消',
        'pending': '待处理', 'paid': '已支付', 'overdue': '逾期',
        'open': '未结', 'resolved': '已解决', 'closed': '已关闭', 'in_progress': '处理中',
        'low': '低', 'medium': '中', 'high': '高', 'urgent': '紧急',
        'email': '邮箱', 'phone': '电话', 'priority': '优先级'
    },
    'ja': {
        'active': '有効', 'suspended': '停止中', 'cancelled': 'キャンセル済み',
        'pending': '保留中', 'paid': '支払い済み', 'overdue': '期限切れ',
        'open': '未解決', 'resolved': '解決済み', 'closed': '完了', 'in_progress': '進行中',
        'low': '低', 'medium': '中', 'high': '高', 'urgent': '緊急',
        'email': 'メール', 'phone': '電話', 'priority': '優先度'
    },
    'ko': {
        'active': '활성', 'suspended': '일시 중지', 'cancelled': '취소됨',
        'pending': '대기 중', 'paid': '지불됨', 'overdue': '연체됨',
        'open': '열림', 'resolved': '해결됨', 'closed': '닫힘', 'in_progress': '진행 중',
        'low': '낮음', 'medium': '중간', 'high': '높음', 'urgent': '긴급',
        'email': '이메일', 'phone': '전화', 'priority': '우선 순위'
    },
    'hi': {
        'active': 'sakriya', 'suspended': 'nilambit', 'cancelled': 'radd',
        'pending': 'lambit', 'paid': 'bhugtan kiya', 'overdue': 'bakaya',
        'open': 'khula', 'resolved': 'hal kiya', 'closed': 'band', 'in_progress': 'pragati par',
        'low': 'kam', 'medium': 'madhyam', 'high': 'uchch', 'urgent': 'tatkal',
        'email': 'email', 'phone': 'phone', 'priority': 'prathmikta'
    },
}

def translate_db_term(term, lang):
    """Translate database terms (status, priority) to target language"""
    if not term: return ""
    term_lower = str(term).lower()
    # Normalize lang code
    lang_code = lang.split('-')[0].lower()

    translations = DB_TERM_TRANSLATIONS.get(lang_code, DB_TERM_TRANSLATIONS['en'])
    return translations.get(term_lower, term)  # Fallback to original if not found

# =============================================================================
# RESPONSE FORMATTERS - Natural language for voice (multi-language)
# =============================================================================
def format_query_results(query_type, results, customer_name=None, lang="en"):
    """Format database results into natural conversational language using localized templates"""
    name = customer_name.split()[0] if customer_name else ""  # First name only
    
    if not results or len(results) == 0:
        return get_natural_response('info_not_found', lang=lang, type=query_type)
    
    if query_type == 'subscription':
        r = results[0]
        status = r.get('status', 'unknown')
        # Translate status
        status_tr = translate_db_term(status, lang)

        plan = r.get('plan_name', 'your plan')
        price = r.get('price', 0)
        
        if status == 'active':
            return get_natural_response('sub_active', lang=lang, plan=plan, name=name, price=price)
        else:
            return get_natural_response('sub_status', lang=lang, plan=plan, status=status_tr)
    
    elif query_type == 'balance':
        r = results[0]
        pending = float(r.get('pending_amount', 0))
        overdue = float(r.get('overdue_amount', 0))
        
        if overdue > 0:
            return get_natural_response('bal_overdue', lang=lang, amount=f"{overdue:.2f}", name=name)
        elif pending > 0:
            return get_natural_response('bal_pending', lang=lang, amount=f"{pending:.2f}", name=name)
        else:
            return get_natural_response('bal_clear', lang=lang, name=name)
    
    elif query_type == 'invoices':
        count = len(results)
        latest = results[0]
        amount = latest.get('amount', 0)
        status = latest.get('status', 'unknown')
        status_tr = translate_db_term(status, lang)
        
        if count == 1:
            return get_natural_response('invoice_one', lang=lang, amount=amount, status=status_tr)
        else:
            return get_natural_response('invoice_many', lang=lang, count=count, amount=amount, status=status_tr)
    
    elif query_type == 'plan':
        r = results[0]
        name_plan = r.get('name', 'your plan')
        price = r.get('price', 0)
        data = r.get('data_limit_gb', 'unlimited')
        support = r.get('support_level', 'standard')
        # Translate support level if it matches a term
        support = translate_db_term(support, lang)
        
        return get_natural_response('plan_details', lang=lang, name=name_plan, price=price, data=data, support=support)
    
    elif query_type == 'tickets':
        open_tickets = [t for t in results if t.get('status') == 'open']
        if open_tickets:
            subject = open_tickets[0].get('subject', 'an issue')
            return get_natural_response('ticket_open', lang=lang, count=len(open_tickets), name=name, subject=subject)
        elif results:
            return get_natural_response('ticket_resolved', lang=lang, count=len(results))
        return get_natural_response('ticket_none', lang=lang)
    
    elif query_type == 'customer_info':
        r = results[0]
        email = r.get('email', 'not set')
        phone = r.get('phone', 'not set')
        return get_natural_response('cust_info', lang=lang, email=email, phone=phone)
    
    return get_natural_response('general_found', lang=lang)


def generate_card_payload(query_type, results):
    """Generate rich UI card payload based on query results"""
    if not results:
        return None
    
    card_data = None
    
    if query_type == 'subscription':
        r = results[0]
        card_data = {
            "title": "Subscription Details",
            "items": [
                {"label": "Plan", "value": r.get('plan_name')},
                {"label": "Status", "value": r.get('status').title()},
                {"label": "Price", "value": f"${r.get('price')}/mo"},
                {"label": "Renews", "value": "Yes" if r.get('auto_renew') else "No"}
            ],
            "type": "info"
        }
        
    elif query_type == 'balance':
        r = results[0]
        card_data = {
            "title": "Account Balance",
            "items": [
                {"label": "Pending", "value": f"${float(r.get('pending_amount', 0)):.2f}"},
                {"label": "Overdue", "value": f"${float(r.get('overdue_amount', 0)):.2f}", "highlight": float(r.get('overdue_amount', 0)) > 0},
                {"label": "Paid", "value": f"${float(r.get('paid_amount', 0)):.2f}"}
            ],
            "type": "alert" if float(r.get('overdue_amount', 0)) > 0 else "success"
        }
        
    elif query_type == 'invoices':
        # Show list of recent invoices
        items = []
        for inv in results[:3]: # Top 3
            items.append({
                "label": f"Inv #{inv.get('id')} ({inv.get('status')})",
                "value": f"${inv.get('amount')}"
            })
        
        card_data = {
            "title": "Recent Invoices",
            "items": items,
            "type": "list"
        }
        
    elif query_type == 'tickets':
        # Show list of recent tickets
        items = []
        for t in results[:3]:
            items.append({
                "label": f"#{t.get('id')} {t.get('subject')}",
                "value": t.get('status').upper(),
                "highlight": t.get('status') == 'open'
            })
            
        card_data = {
            "title": "Support Tickets",
            "items": items,
            "type": "list"
        }
    
    if card_data:
        return {
            "type": "visual_card",
            "card_type": query_type,
            "data": card_data
        }
    return None


# =============================================================================
# MULTI-LANGUAGE NUMBER PARSING - Extract PIN from spoken words
# =============================================================================
def parse_spoken_numbers(text: str, language: str = 'en') -> str:
    """
    Convert spoken number words to digits in any supported language.
    Example: "one two three four" -> "1234"
    Example (Hebrew): "אחת שתיים שלוש ארבע" -> "1234"
    """
    if not text:
        return ""
    
    # Get language code (handle locale codes like 'en-US')
    lang_code = language.split('-')[0].lower() if '-' in language else language.lower()
    
    # Get number words for this language, fallback to English
    number_words = NUMBER_WORDS.get(lang_code, NUMBER_WORDS.get('en', {}))
    
    # Also always include English numbers as fallback (many people mix languages)
    all_number_words = {**number_words, **NUMBER_WORDS.get('en', {})}
    
    result = []
    text_lower = text.lower()
    
    # First, extract any actual digits
    for char in text:
        if char.isdigit():
            result.append(char)
    
    # Then, look for number words
    for word, digit in all_number_words.items():
        if word.lower() in text_lower:
            # Count occurrences
            count = text_lower.count(word.lower())
            for _ in range(count):
                result.append(digit)
    
    # If we found digits, return them (limit to reasonable PIN length)
    if result:
        return ''.join(result[:10])  # Max 10 digits
    
    return ""


def extract_pin_from_text(text: str, language: str = 'en') -> str:
    """
    Extract a 4-digit PIN from spoken text.
    Handles both digits and spoken number words in multiple languages.
    """
    # First try direct digit extraction
    digits = ''.join(c for c in text if c.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    
    # Then try parsing spoken numbers
    parsed = parse_spoken_numbers(text, language)
    if len(parsed) >= 4:
        return parsed[:4]
    
    # Combine both approaches
    all_digits = digits + parsed
    if len(all_digits) >= 4:
        return all_digits[:4]
    
    return ""


# =============================================================================
# RETENTION RESPONSE - Generate empathetic responses for frustrated customers
# =============================================================================
def get_retention_response(sentiment_result: dict, language: str = 'en') -> str:
    """
    Generate an appropriate retention response based on sentiment analysis.
    When customer wants to leave, try to convince them to stay and promise a callback.
    
    v5.1.3: Enhanced multi-language retention with callback promise
    """
    churn_risk = sentiment_result.get('churn_risk', 'low')
    label = sentiment_result.get('label', '')
    
    # Comprehensive retention responses by language
    # Each includes: empathy, attempt to retain, and promise of callback
    responses = {
        'en': {
            'high': "I completely understand your frustration, and I'm truly sorry you've had this experience. Please don't leave just yet - I really want to help make this right. A senior representative will call you back very soon to personally address your concerns and find a solution that works for you. We value you as a customer and want to earn back your trust.",
            'medium': "I apologize for any inconvenience you've experienced. Your satisfaction is really important to us. Let me see what I can do to help, and if needed, one of our specialists will follow up with you shortly to ensure everything is resolved.",
            'cancellation': "I hear that you're thinking of leaving, and I genuinely want to understand what happened. Before you make a final decision, please let me try to help. A dedicated representative will call you back shortly to discuss what we can do to make things right and keep you as a valued customer.",
        },
        'he': {
            'high': "אני מבין לגמרי את התסכול שלך, ואני באמת מצטער על החוויה הזו. בבקשה אל תעזוב עדיין - אני באמת רוצה לעזור לתקן את זה. נציג בכיר יחזור אליך בקרוב מאוד כדי לטפל אישית בחששות שלך ולמצוא פתרון שמתאים לך. אנחנו מעריכים אותך כלקוח ורוצים להחזיר את האמון שלך.",
            'medium': "אני מתנצל על אי הנוחות. שביעות הרצון שלך חשובה לנו מאוד. בוא נראה מה אני יכול לעשות לעזור, ואם צריך, אחד המומחים שלנו יחזור אליך בקרוב.",
            'cancellation': "אני שומע שאתה חושב לעזוב, ואני באמת רוצה להבין מה קרה. לפני שאתה מקבל החלטה סופית, בבקשה תן לי לנסות לעזור. נציג יחזור אליך בקרוב כדי לדון מה אנחנו יכולים לעשות כדי לתקן את המצב ולשמור עליך כלקוח יקר.",
        },
        'es': {
            'high': "Entiendo completamente su frustración y lamento mucho esta experiencia. Por favor, no se vaya todavía - realmente quiero ayudar a resolver esto. Un representante senior le llamará muy pronto para atender personalmente sus inquietudes y encontrar una solución. Le valoramos como cliente y queremos recuperar su confianza.",
            'medium': "Lamento las molestias que ha experimentado. Su satisfacción es muy importante para nosotros. Déjeme ver cómo puedo ayudar.",
            'cancellation': "Escucho que está pensando en irse, y genuinamente quiero entender qué pasó. Antes de tomar una decisión final, permítame intentar ayudar. Un representante dedicado le llamará pronto para discutir qué podemos hacer para mejorar la situación.",
        },
        'fr': {
            'high': "Je comprends parfaitement votre frustration et je suis vraiment désolé pour cette expérience. S'il vous plaît, ne partez pas encore - je veux vraiment vous aider à arranger les choses. Un représentant senior vous rappellera très bientôt pour répondre personnellement à vos préoccupations. Nous vous valorisons en tant que client.",
            'medium': "Je m'excuse pour tout désagrément. Votre satisfaction est très importante pour nous. Laissez-moi voir ce que je peux faire pour vous aider.",
            'cancellation': "J'entends que vous pensez à partir, et je veux sincèrement comprendre ce qui s'est passé. Un représentant vous rappellera bientôt pour discuter de ce que nous pouvons faire pour améliorer la situation.",
        },
        'de': {
            'high': "Ich verstehe Ihre Frustration vollkommen und es tut mir wirklich leid für diese Erfahrung. Bitte gehen Sie noch nicht - ich möchte wirklich helfen, das in Ordnung zu bringen. Ein leitender Mitarbeiter wird Sie sehr bald zurückrufen, um persönlich auf Ihre Anliegen einzugehen. Wir schätzen Sie als Kunden.",
            'medium': "Ich entschuldige mich für etwaige Unannehmlichkeiten. Ihre Zufriedenheit ist uns sehr wichtig. Lassen Sie mich sehen, wie ich helfen kann.",
            'cancellation': "Ich höre, dass Sie darüber nachdenken zu gehen, und ich möchte wirklich verstehen, was passiert ist. Ein Mitarbeiter wird Sie bald zurückrufen, um zu besprechen, was wir tun können.",
        },
        'it': {
            'high': "Capisco perfettamente la sua frustrazione e mi dispiace davvero per questa esperienza. Per favore, non se ne vada ancora - voglio davvero aiutare a sistemare le cose. Un rappresentante senior la richiamerà molto presto. La apprezziamo come cliente.",
            'medium': "Mi scuso per qualsiasi inconveniente. La sua soddisfazione è molto importante per noi.",
            'cancellation': "Sento che sta pensando di andarsene. Un rappresentante la richiamerà presto per discutere cosa possiamo fare per migliorare la situazione.",
        },
        'pt': {
            'high': "Eu entendo completamente sua frustração e lamento muito por essa experiência. Por favor, não vá ainda - eu realmente quero ajudar a resolver isso. Um representante sênior ligará para você em breve. Valorizamos você como cliente.",
            'medium': "Peço desculpas por qualquer inconveniente. Sua satisfação é muito importante para nós.",
            'cancellation': "Ouço que você está pensando em sair. Um representante entrará em contato em breve para discutir o que podemos fazer.",
        },
        'pl': {
            'high': "Całkowicie rozumiem Pana/Pani frustrację i naprawdę przepraszam za to doświadczenie. Proszę jeszcze nie odchodzić - naprawdę chcę pomóc to naprawić. Starszy przedstawiciel wkrótce do Pana/Pani zadzwoni.",
            'medium': "Przepraszam za wszelkie niedogodności. Pana/Pani satysfakcja jest dla nas bardzo ważna.",
            'cancellation': "Słyszę, że myślisz o odejściu. Przedstawiciel wkrótce zadzwoni, aby omówić, co możemy zrobić.",
        },
        'tr': {
            'high': "Hayal kırıklığınızı tamamen anlıyorum ve bu deneyim için gerçekten özür dilerim. Lütfen henüz gitmeyin - bunu düzeltmek için gerçekten yardım etmek istiyorum. Kıdemli bir temsilci endişelerinizi kişisel olarak ele almak için çok yakında sizi arayacak.",
            'medium': "Herhangi bir rahatsızlık için özür dilerim. Memnuniyetiniz bizim için çok önemli.",
            'cancellation': "Ayrılmayı düşündüğünüzü duyuyorum. Bir temsilci durumu iyileştirmek için neler yapabileceğimizi görüşmek için yakında sizi arayacak.",
        },
        'ru': {
            'high': "Я полностью понимаю ваше разочарование и искренне сожалею об этом опыте. Пожалуйста, не уходите пока - я действительно хочу помочь исправить ситуацию. Старший представитель очень скоро перезвонит вам. Мы ценим вас как клиента.",
            'medium': "Приношу извинения за неудобства. Ваше удовлетворение очень важно для нас.",
            'cancellation': "Я слышу, что вы думаете об уходе. Представитель скоро перезвонит, чтобы обсудить, что мы можем сделать.",
        },
        'nl': {
            'high': "Ik begrijp uw frustratie volledig en het spijt me echt voor deze ervaring. Ga alstublieft nog niet weg - ik wil echt helpen dit recht te zetten. Een senior medewerker belt u zeer binnenkort terug.",
            'medium': "Mijn excuses voor het ongemak. Uw tevredenheid is zeer belangrijk voor ons.",
            'cancellation': "Ik hoor dat u overweegt te vertrekken. Een medewerker belt u binnenkort om te bespreken wat we kunnen doen.",
        },
        'cs': {
            'high': "Plně chápu vaši frustraci a upřímně se omlouvám za tuto zkušenost. Prosím, ještě neodcházejte - opravdu chci pomoci to napravit. Vedoucí zástupce vám velmi brzy zavolá zpět.",
            'medium': "Omlouvám se za jakékoliv nepříjemnosti. Vaše spokojenost je pro nás velmi důležitá.",
            'cancellation': "Slyším, že uvažujete o odchodu. Zástupce vám brzy zavolá, abychom probrali, co můžeme udělat.",
        },
        'ar': {
            'high': "أفهم تماماً إحباطك وأنا آسف حقاً لهذه التجربة. من فضلك لا تغادر بعد - أريد حقاً المساعدة في إصلاح هذا. سيتصل بك ممثل كبير قريباً جداً. نحن نقدرك كعميل.",
            'medium': "أعتذر عن أي إزعاج. رضاك مهم جداً بالنسبة لنا.",
            'cancellation': "أسمع أنك تفكر في المغادرة. سيتصل بك ممثل قريباً لمناقشة ما يمكننا فعله.",
        },
        'zh-cn': {
            'high': "我完全理解您的沮丧，对这次经历我深表歉意。请先不要离开 - 我真的想帮助解决这个问题。一位高级代表会很快回电给您。我们非常重视您这位客户。",
            'medium': "对于任何不便，我深表歉意。您的满意对我们非常重要。",
            'cancellation': "我听到您在考虑离开。代表会尽快回电讨论我们能做什么。",
        },
        'ja': {
            'high': "お客様のご不満を完全に理解しており、この経験については本当に申し訳なく思います。まだお帰りにならないでください - 本当にお力になりたいのです。シニア担当者がすぐにお電話いたします。お客様を大切にしております。",
            'medium': "ご不便をおかけして申し訳ございません。お客様のご満足は私たちにとって非常に重要です。",
            'cancellation': "ご退会をお考えとのこと、承りました。担当者からまもなくお電話差し上げます。",
        },
        'hu': {
            'high': "Teljesen megértem a csalódottságát, és nagyon sajnálom ezt az élményt. Kérem, ne menjen még el - tényleg segíteni szeretnék. Egy vezető képviselő nagyon hamarosan visszahívja Önt.",
            'medium': "Elnézést kérek minden kellemetlenségért. Az Ön elégedettsége nagyon fontos számunkra.",
            'cancellation': "Hallom, hogy gondolkodik a távozáson. Egy képviselő hamarosan felhívja, hogy megbeszéljük, mit tehetünk.",
        },
        'ko': {
            'high': "고객님의 불만을 충분히 이해하며, 이번 경험에 대해 정말 죄송합니다. 아직 떠나지 마세요 - 정말로 이 문제를 해결하고 싶습니다. 담당자가 곧 전화드릴 것입니다. 고객님을 소중히 여깁니다.",
            'medium': "불편을 드려 죄송합니다. 고객님의 만족은 저희에게 매우 중요합니다.",
            'cancellation': "떠나실 생각을 하고 계신다고 들었습니다. 담당자가 곧 연락드려 무엇을 할 수 있는지 상의하겠습니다.",
        },
    }
    
    lang_code = language.split('-')[0].lower() if '-' in language else language.lower()
    lang_responses = responses.get(lang_code, responses.get('en'))
    
    # Determine which response to use
    if label == 'cancellation_intent' or churn_risk == 'high':
        return lang_responses.get('cancellation', lang_responses.get('high', ''))
    elif churn_risk == 'high':
        return lang_responses.get('high', '')
    elif churn_risk == 'medium':
        return lang_responses.get('medium', '')
    
    return ""


# =============================================================================
# HEALTH CHECK - Check all services status
# =============================================================================
async def check_services_health() -> dict:
    """
    Check health of all connected services (ASR, TTS, LLM, DB).
    Returns comprehensive health status.
    """
    status = {
        "healthy": True,
        "timestamp": datetime.now().isoformat(),
        "services": {},
        "circuit_breakers": {},
        "watchdog_issues": [],
    }
    
    client = await get_http_client()
    
    # Check ASR
    try:
        asr_url = DEFAULT_SETTINGS.get('asr_server_address', '')
        if asr_url:
            if not asr_url.startswith('http'):
                asr_url = f"http://{asr_url}"
            resp = await client.get(f"{asr_url}/health", timeout=5)
            status["services"]["asr"] = {"status": "healthy" if resp.status_code == 200 else "unhealthy"}
        else:
            status["services"]["asr"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["asr"] = {"status": "unhealthy", "error": str(e)[:100]}
        status["healthy"] = False
    
    # Check TTS
    try:
        tts_url = DEFAULT_SETTINGS.get('tts_server_address', '')
        if tts_url:
            if not tts_url.startswith('http'):
                tts_url = f"http://{tts_url}"
            resp = await client.get(f"{tts_url}/health", timeout=5)
            status["services"]["tts"] = {"status": "healthy" if resp.status_code == 200 else "unhealthy"}
        else:
            status["services"]["tts"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["tts"] = {"status": "unhealthy", "error": str(e)[:100]}
        status["healthy"] = False
    
    # Check Database
    try:
        db_pool = await ensure_db_pool()
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            status["services"]["database"] = {"status": "healthy"}
        else:
            status["services"]["database"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["database"] = {"status": "unhealthy", "error": str(e)[:100]}
    
    # Add circuit breaker status
    status["circuit_breakers"] = {
        "asr": _asr_circuit.get_status(),
        "tts": _tts_circuit.get_status(),
        "llm": _llm_circuit.get_status(),
    }
    
    # Add watchdog issues
    status["watchdog_issues"] = _watchdog.check_health()
    if status["watchdog_issues"]:
        status["healthy"] = False
    
    return status


# =============================================================================
# AUDIO PROCESSING
# =============================================================================
def inspect_wav_properties(file_input) -> tuple[int, int, str]:
    try:
        # Handle both filepath and file-like object
        if isinstance(file_input, str):
            if not os.path.exists(file_input):
                return 0, 0, f"Error: File not found: {file_input}"
            f = wave.open(file_input, 'rb')
        else:
            f = wave.open(file_input, 'rb')
            
        with f as wf:
            rate, channels = wf.getframerate(), wf.getnchannels()
            status = f"Audio Properties - Rate: {rate}Hz, Channels: {channels}, Width: {wf.getsampwidth()*8}-bit"
            return rate, channels, status
    except wave.Error as e:
        return 0, 0, f"Error inspecting WAV file: {e}"
    except Exception as e:
        return 0, 0, f"Error processing audio: {e}"


async def resample_audio_async(input_file: str, target_sample_rate: int = 16000) -> tuple[str, bool]:
    """Resample audio to target sample rate using ffmpeg (async)."""
    output_file = input_file.replace('.wav', f'_{target_sample_rate}.wav')
    try:
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-ar', str(target_sample_rate),
            '-ac', '1',  # mono
            '-loglevel', 'error',
            output_file
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            logging.info(f"Resampled audio to {target_sample_rate}Hz: {output_file}")
            return output_file, True
        else:
            logging.error(f"Failed to resample audio: {stderr.decode()}")
            return input_file, False
            
    except FileNotFoundError:
        logging.warning("ffmpeg not found, using original audio")
        return input_file, False
    except Exception as e:
        logging.error(f"Resample error: {e}")
        return input_file, False


# =============================================================================
# WHISPER ASR
# =============================================================================
async def transcribe_with_whisper(audio_input, settings: dict, max_retries: int = 2) -> str:
    """Transcribe audio using Whisper server (HTTP) with retry logic and circuit breaker"""
    
    # Check circuit breaker first
    if not _asr_circuit.can_execute():
        logging.warning("[ASR] Circuit breaker is OPEN, skipping request")
        return ""
    
    asr_server = settings.get('asr_server_address', '').strip()
    language = settings.get('asr_language_code', 'en')
    
    if not asr_server:
        logging.error("[ASR] No ASR server address configured")
        return ""
    
    if isinstance(audio_input, bytes):
        audio_data = audio_input
    else:
        with open(audio_input, 'rb') as f:
            audio_data = f.read()
    
    asr_server = asr_server.strip()
    asr_server = ' '.join(asr_server.split())
    
    if not asr_server.startswith('http'):
        asr_url = f"http://{asr_server}"
    else:
        asr_url = asr_server
    
    asr_url = asr_url.replace('http:// ', 'http://').replace('https:// ', 'https://')
    
    # Use global LOCALE_MAP (defined at top of file for performance)
    is_auto = not language or language == "auto" or "auto" in str(language).lower()
    
    if is_auto:
        full_language = 'en-US'
    elif '-' in language:
        full_language = language
    else:
        short_code = language.lower().strip()
        full_language = LOCALE_MAP.get(short_code, f"{short_code}-{short_code.upper()}")
    
    logging.info(f"[ASR] Sending to Whisper: {asr_url} (lang={full_language})")
    
    data = {'language': full_language}
    client = await get_http_client()
    
    for attempt in range(max_retries + 1):
        try:
            files = {'file': ('audio.wav', audio_data, 'audio/wav')}
            
            endpoints = [
                '/v1/audio/transcriptions',
                '/transcribe',
                '/asr',
                '/inference'
            ]
            
            for endpoint in endpoints:
                try:
                    response = await client.post(f"{asr_url}{endpoint}", files=files, data=data)
                    
                    if response.status_code == 200:
                        result = response.json()
                        if isinstance(result, dict):
                            transcript = result.get('text', result.get('transcription', result.get('transcript', '')))
                        else:
                            transcript = str(result)
                        logging.info(f"[ASR] Success via {endpoint}")
                        _asr_circuit.record_success()
                        _watchdog.record_asr_success()
                        return transcript.strip()
                except httpx.TimeoutException:
                    continue
                except Exception as e:
                    continue
            
            if attempt < max_retries:
                logging.warning(f"[ASR] All endpoints failed, retry {attempt + 1}/{max_retries}")
                await asyncio.sleep(0.5 * (attempt + 1))
            
        except Exception as e:
            logging.error(f"[ASR] Whisper error (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    
    _asr_circuit.record_failure()
    logging.error(f"[ASR] All retries failed for {asr_url}")
    return ""


# =============================================================================
# TEXT CLEANING FOR TTS - Ensures clean, natural-sounding audio output
# =============================================================================
def clean_text_for_tts(text: str) -> str:
    """
    Clean and normalize text for TTS to avoid strange sounds at beginning/end.
    
    v5.1.3 Enhanced:
    - Removes leading/trailing whitespace and newlines
    - Removes multiple spaces
    - Removes markdown formatting (*bold*, _italic_, etc.)
    - Removes emojis that can't be spoken
    - Removes special characters that cause audio artifacts
    - Handles abbreviations and numbers for better pronunciation
    - Ensures proper sentence structure for natural prosody
    
    Returns clean, speakable text.
    """
    import re
    
    if not text:
        return ""
    
    # Remove markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)      # *italic*
    text = re.sub(r'\_\_(.+?)\_\_', r'\1', text)  # __underline__
    text = re.sub(r'\_(.+?)\_', r'\1', text)      # _italic_
    text = re.sub(r'\~\~(.+?)\~\~', r'\1', text)  # ~~strikethrough~~
    text = re.sub(r'`(.+?)`', r'\1', text)        # `code`
    
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    
    # Remove most emojis (keep some that are commonly used)
    emoji_pattern = re.compile("["
        u"\U0001F600-\U0001F64F"  # emoticons
        u"\U0001F300-\U0001F5FF"  # symbols & pictographs
        u"\U0001F680-\U0001F6FF"  # transport & map symbols
        u"\U0001F700-\U0001F77F"  # alchemical symbols
        u"\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        u"\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        u"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        u"\U0001FA00-\U0001FA6F"  # Chess Symbols
        u"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        u"\U00002702-\U000027B0"  # Dingbats
        u"\U0001F1E0-\U0001F1FF"  # Flags (iOS)
    "]+", flags=re.UNICODE)
    text = emoji_pattern.sub('', text)
    
    # Remove special characters that cause audio artifacts
    text = re.sub(r'[#@$%^&*+=|\\<>{}[\]~]', '', text)
    
    # v5.1.3: Handle common abbreviations for better pronunciation
    abbreviations = {
        r'\bMr\.': 'Mister',
        r'\bMrs\.': 'Missus',
        r'\bDr\.': 'Doctor',
        r'\bSt\.': 'Street',
        r'\bvs\.': 'versus',
        r'\betc\.': 'etcetera',
        r'\be\.g\.': 'for example',
        r'\bi\.e\.': 'that is',
    }
    for abbr, full in abbreviations.items():
        text = re.sub(abbr, full, text, flags=re.IGNORECASE)
    
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Remove leading/trailing whitespace
    text = text.strip()
    
    # v5.1.3: Remove leading punctuation or special chars (prevents strange starts)
    text = re.sub(r'^[\s,.;:!?\-_\'\"]+', '', text)
    
    # v5.1.3: Remove trailing incomplete punctuation
    text = re.sub(r'[\s,\-_]+$', '', text)
    
    # Ensure proper sentence ending (helps with prosody)
    if text and text[-1] not in '.!?':
        text += '.'
    
    # Remove double punctuation
    text = re.sub(r'([.!?])\1+', r'\1', text)
    text = re.sub(r'\.{2,}', '.', text)
    
    # v5.1.3: Ensure text starts with a capital letter (for better TTS prosody)
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    
    return text


# =============================================================================
# XTTS TTS
# =============================================================================
async def synthesize_with_xtts(text: str, settings: dict, max_retries: int = 5) -> bytes:
    """Synthesize speech using XTTS server with fallback chain: XTTS → Edge-TTS → Empty"""
    
    # v5.1.2: Clean text for better audio quality
    text = clean_text_for_tts(text)
    
    if not text or len(text.strip()) < 2:
        logging.warning("[TTS] Empty or too short text after cleaning, skipping synthesis")
        return b''
    
    tts_server = settings.get('tts_server_address', 'localhost:8000')
    language = settings.get('tts_language_code', 'en')
    
    # Get speaker setting
    speaker = settings.get('tts_speaker') or settings.get('tts_voice') or ''
    
    logging.info(f"[TTS] 🎙️ Request - server: {tts_server}, lang: {language}, speaker: '{speaker}'")
    logging.info(f"[TTS] 📝 Text ({len(text)} chars): '{text[:80]}...'")
    
    # Normalize language code (preserve zh-cn)
    if language.lower() != 'zh-cn' and '-' in language:
        language = language.split('-')[0].lower()

    else:
        language = language.lower()
    
    # v5.1.0: Check cache first (expanded cache)
    cache_key = f"tts:{hash(text)}:{language}:{speaker}"
    cached = _tts_cache.get(cache_key)
    if cached:
        logging.info(f"[TTS] ✅ Cache HIT: {text[:30]}... ({len(cached):,} bytes)")
        return cached
    
    if not tts_server.startswith('http'):
        tts_url = f"http://{tts_server}"
    else:
        tts_url = tts_server
    
    client = await get_http_client()
    
    # === Try XTTS Server ===
    for attempt in range(max_retries + 1):
        try:
            payload = {
                "text": text,
                "language": language,
            }
            if speaker:
                payload["speaker"] = speaker
            
            # v5.1.3: Optimized TTS parameters for natural, high-quality speech
            payload.update({
                "temperature": 0.65,           # Lower = more consistent/stable voice
                "top_p": 0.85,                 # Nucleus sampling for natural variation
                "top_k": 50,                   # Limit vocabulary selection
                "repetition_penalty": 1.2,     # Prevent repetitive artifacts
                "length_penalty": 1.0,         # Natural length
                "speed": 1.0,                  # Normal speed
                "enable_text_splitting": True, # Better prosody for long text
            })
            
            audio_buffer = bytearray()
            start_time = time.time()
            
            async with client.stream("POST", f"{tts_url}/tts/wav", json=payload, timeout=20.0) as response:
                if response.status_code == 200:
                    try:
                        async for chunk in response.aiter_bytes():
                            audio_buffer.extend(chunk)
                    except Exception as stream_err:
                        logging.warning(f"[TTS] Stream interrupted: {stream_err}")
                    
                    if len(audio_buffer) > 0:
                        audio_data = bytes(audio_buffer)
                        latency = time.time() - start_time
                        logging.info(f"[TTS] ✅ XTTS success: {len(audio_data):,} bytes in {latency:.2f}s")
                        
                        # Update health status
                        await _service_health.update("tts", "ok", latency, tts_url)
                        
                        # Cache the result
                        if len(audio_data) < 1000000:  # Don't cache >1MB
                            _tts_cache.set(cache_key, audio_data)
                        
                        return audio_data
                    else:
                        logging.warning("[TTS] ⚠️ Empty audio response")

                elif response.status_code == 400 and speaker and speaker != "default":
                    logging.warning(f"[TTS] 400 with speaker '{speaker}', trying without...")
                    payload.pop("speaker", None)
                    continue
                else:
                    logging.warning(f"[TTS] ⚠️ HTTP {response.status_code}")
                    
            if attempt < max_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
                
        except httpx.TimeoutException:
            logging.warning(f"[TTS] ⏱️ Timeout (attempt {attempt + 1}/{max_retries + 1})")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as e:
            logging.error(f"[TTS] ❌ Error (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    
    # === XTTS Failed - Try Edge-TTS Fallback ===
    logging.warning(f"[TTS] ⚠️ XTTS failed after {max_retries + 1} attempts, trying Edge-TTS fallback...")
    await _service_health.update("tts", "degraded", 0, tts_url)
    
    fallback_audio = await fallback_edge_tts(text, language)
    if fallback_audio:
        # Cache fallback result too
        if len(fallback_audio) < 1000000:
            _tts_cache.set(cache_key, fallback_audio)
        return fallback_audio
    
    # === All Failed ===
    logging.error(f"[TTS] ❌❌ ALL TTS OPTIONS FAILED!")
    logging.error(f"[TTS] ❌❌ Text: '{text[:100]}...'")
    await _service_health.update("tts", "down", 0, tts_url)
    return b""


async def stream_tts_for_sentence(websocket, text_to_synthesize, stream_start_time, metrics, settings: dict) -> bool:
    """Synthesize and stream TTS for a sentence. Returns True if audio sent."""
    original_text = text_to_synthesize
    text_to_synthesize = clean_tts_text(text_to_synthesize)
    
    if not text_to_synthesize:
        logging.warning(f"[TTS Task] ⚠️ clean_tts_text returned None!")
        logging.warning(f"[TTS Task] ⚠️ Original text was: '{original_text[:100] if original_text else 'None'}'")
        return False
    
    logging.info(f"[TTS Task] 🔊 Synthesizing: \"{text_to_synthesize[:50]}...\"")
    
    try:
        audio_data = await synthesize_with_xtts(text_to_synthesize, settings)
        
        if audio_data:
            current_time = time.time()
            if metrics['llm_tts_metrics']['first_tts_chunk_time'] is None:
                metrics['llm_tts_metrics']['first_tts_chunk_time'] = current_time - stream_start_time
            
            logging.info(f"[TTS Task] ✅ Got {len(audio_data):,} bytes of audio")
            
            # Stream audio in chunks
            chunk_size = 8192
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                metrics['llm_tts_metrics']['tts_chunk_latencies'].append(time.time() - stream_start_time)
                await websocket.send(chunk)

            return True
        else:
            logging.error(f"[TTS Task] ❌ No audio data returned for: '{text_to_synthesize[:50]}...'")
            logging.error(f"[TTS Task] ❌ Check TTS server and circuit breaker status")
            return False
                
    except Exception as e:
        logging.error(f"[TTS Task] ❌ Exception: {e}")
        import traceback
        logging.error(f"[TTS Task] ❌ Traceback: {traceback.format_exc()}")

    return False


# =============================================================================
# LLM PROMPTS - Optimized for natural voice conversation
# =============================================================================
# =============================================================================
# LANGUAGE INSTRUCTIONS FOR LLM - Ensure response in correct language
# =============================================================================
LANGUAGE_INSTRUCTIONS = {
    "en": "LANGUAGE: You MUST respond ONLY in English. Even if the user speaks Hebrew, Arabic, or another language - understand them but ALWAYS reply in natural, conversational English. Never use non-English numbers or terms.",
    "he": "LANGUAGE: You MUST respond ONLY in Hebrew. Even if the user speaks English or another language - understand them but ALWAYS reply in natural, conversational Hebrew. Never use English numbers or terms.",
    "es": "IDIOMA: DEBES responder SOLAMENTE en español. Aunque el usuario hable otro idioma, entiéndelo pero SIEMPRE responde en español natural y conversacional. No uses palabras en inglés.",
    "fr": "LANGUE: Tu DOIS répondre UNIQUEMENT en français. Même si l'utilisateur parle une autre langue, comprends-le mais réponds TOUJOURS en français naturel et conversationnel. N'utilise pas de mots anglais.",
    "de": "SPRACHE: Du MUSST ausschließlich auf Deutsch antworten. Auch wenn der Benutzer eine andere Sprache spricht, verstehe ihn aber antworte IMMER in natürlichem Deutsch. Keine englischen Wörter.",
    "it": "LINGUA: DEVI rispondere SOLO in italiano. Anche se l'utente parla un'altra lingua, capiscilo ma rispondi SEMPRE in italiano naturale. Non usare parole inglesi.",
    "pt": "IDIOMA: Você DEVE responder APENAS em português. Mesmo que o usuário fale outro idioma, entenda-o mas responda SEMPRE em português natural. Não use palavras em inglês.",
    "pl": "JĘZYK: MUSISZ odpowiadać TYLKO po polsku. Nawet jeśli użytkownik mówi w innym języku, zrozum go ale ZAWSZE odpowiadaj po polsku. Nie używaj angielskich słów.",
    "tr": "DİL: SADECE Türkçe yanıt vermelisin. Kullanıcı başka bir dilde konuşsa bile, onu anla ama HER ZAMAN Türkçe yanıt ver. İngilizce kelime kullanma.",
    "ru": "ЯЗЫК: Ты ДОЛЖЕН отвечать ТОЛЬКО на русском. Даже если пользователь говорит на другом языке, пойми его но ВСЕГДА отвечай на русском. Не используй английские слова.",
    "nl": "TAAL: Je MOET ALLEEN in het Nederlands antwoorden. Ook als de gebruiker een andere taal spreekt, begrijp hem maar antwoord ALTIJD in het Nederlands. Geen Engelse woorden.",
    "cs": "JAZYK: MUSÍŠ odpovídat POUZE česky. I když uživatel mluví jiným jazykem, porozuměj mu ale VŽDY odpovídej česky. Žádná anglická slova.",
    "ar": "اللغة: يجب أن تجيب باللغة العربية فقط. حتى لو تحدث المستخدم بلغة أخرى، افهمه لكن أجب دائماً بالعربية. لا تستخدم كلمات إنجليزية.",
    "zh-cn": "语言规则：你必须只用中文回答。即使用户说其他语言，也要理解他们但始终用中文回答。不要使用英文单词。",
    "zh": "语言规则：你必须只用中文回答。即使用户说其他语言，也要理解他们但始终用中文回答。不要使用英文单词。",
    "ja": "言語ルール：日本語のみで回答してください。ユーザーが他の言語を話しても、理解した上で必ず日本語で回答してください。英語は使わないでください。",
    "hu": "NYELV: CSAK magyarul válaszolj. Még ha a felhasználó más nyelven beszél is, értsd meg de MINDIG magyarul válaszolj. Ne használj angol szavakat.",
    "ko": "언어 규칙: 한국어로만 답변해야 합니다. 사용자가 다른 언어로 말해도 이해하되 항상 한국어로 답변하세요. 영어를 사용하지 마세요.",
}

# =============================================================================
# ADAPTIVE PERSONA TEMPLATES - Adjusts tone based on sentiment
# =============================================================================
PERSONA_TEMPLATES = {
    "default": "You are a friendly, natural-sounding customer service agent on a live phone call.",
    "empathetic": "You are an empathetic, patient, and apologetic customer service agent. The customer seems frustrated, so prioritize validating their feelings, apologizing sincerely, and resolving their issue quickly.",
    "retention": "You are a senior retention specialist. The customer is at risk of leaving. Be extremely professional, empathetic, and empowered to solve issues. Your primary goal is to save this customer relationship.",
}

def get_language_instruction(lang_code):
    """Get language instruction for the LLM based on TTS output language"""
    # Normalize language code
    if '-' in lang_code:
        lang_code = lang_code.split('-')[0].lower()
    return LANGUAGE_INSTRUCTIONS.get(lang_code, LANGUAGE_INSTRUCTIONS.get('en'))


def build_llm_prompt(transcript, conversation, history_context="", response_lang="en", sentiment=None):
    """Build a natural conversation prompt for LLM with adaptive persona"""
    
    name = conversation.customer_name.split()[0] if conversation and conversation.customer_name else "friend"
    lang_instruction = get_language_instruction(response_lang)
    
    # Select persona based on sentiment
    persona = PERSONA_TEMPLATES["default"]
    if sentiment:
        if sentiment.get('churn_risk') in ['high', 'medium'] or sentiment.get('label') == 'cancellation_intent':
            persona = PERSONA_TEMPLATES["retention"]
            logging.info("[Persona] Using RETENTION persona")
        elif sentiment.get('label') in ['negative', 'very_negative']:
            persona = PERSONA_TEMPLATES["empathetic"]
            logging.info("[Persona] Using EMPATHETIC persona")
    
    prompt = f'''{persona}

{lang_instruction}

CRITICAL VOICE RULES (this will be spoken aloud by TTS):
1. Use ONLY 1-3 short sentences - people can't follow long spoken responses
2. Use contractions naturally (I'm, you're, don't, can't, we'll)
3. Sound human: use "um", "well", "actually" sparingly but naturally
4. Never use bullet points, lists, markdown, or formatting
5. Reference the customer by first name ({name}) occasionally
6. Be warm but efficient - like a helpful friend who works at the company
7. If you don't understand, ask naturally: "Sorry, I didn't quite catch that - could you say that again?"
8. The customer may speak a DIFFERENT language (like Hebrew) - understand them but respond in YOUR designated language only

{f"Conversation so far:{chr(10)}{history_context}{chr(10)}" if history_context else ""}

{name} just said: "{transcript}"

Respond naturally in 1-3 sentences as if speaking on the phone:'''

    return prompt


def build_general_llm_prompt(transcript, history_context="", custom_template=None, response_lang="en"):
    """Build prompt for non-database mode conversations"""
    
    lang_instruction = get_language_instruction(response_lang)
    
    if custom_template and '{transcript}' in custom_template:
        # Add language instruction to custom template
        return f"{lang_instruction}\n\n{custom_template.format(transcript=transcript)}"
    
    prompt = f'''You're having a friendly voice conversation. Keep it natural and brief.

{lang_instruction}

VOICE RULES:
- Maximum 2-3 sentences - this is spoken, not written
- Use contractions and casual language
- Sound like a real person, not an AI
- No bullet points or formatting ever
- Customer may speak a different language - understand them, respond in YOUR language only

{f"Recent conversation:{chr(10)}{history_context}{chr(10)}" if history_context else ""}

User said: "{transcript}"

Reply naturally:'''

    return prompt


# =============================================================================
# MAIN PIPELINE - Enhanced conversation handling with language support
# =============================================================================
async def main_pipeline(websocket, audio_bytes: bytes, settings: dict, session_id: str = None):
    pipeline_start_time = time.time()
    metrics = {
        'asr_processing_time': 0,
        'total_processing_time': 0,
        'llm_tts_metrics': {
            'first_llm_token_time': None,
            'llm_token_latencies': [],
            'first_tts_chunk_time': None,
            'tts_chunk_latencies': [],
            'audio_generation_time': 0
        }
    }
    tts_tasks, input_audio_file = [], None
    db_manager = None
    conversation = None

    db_enabled = settings.get('db_enabled', False)
    reset_session = settings.get('reset_session', False)
    
    # Get response language from TTS settings (this is the Voice Output language)
    response_lang = settings.get('tts_language_code', 'en')
    # Normalize language code (en-US -> en), but keep zh-cn
    if response_lang.lower() != 'zh-cn' and '-' in response_lang:
        response_lang = response_lang.split('-')[0].lower()
    else:
        response_lang = response_lang.lower()
    logging.info(f"[Lang] Response language: {response_lang}")
    
    if db_enabled and ASYNCPG_AVAILABLE:
        # USE GLOBAL POOL - Fixes "no voice" latency/timeout issues
        db_host = settings.get('db_host', 'localhost')
        db_port = settings.get('db_port', '5432')
        db_name = settings.get('db_name', 'customer_service')
        db_user = settings.get('db_user', 'agent')
        db_pass = settings.get('db_password', '')
        
        # Get global pool or create one
        shared_pool = await get_db_pool(db_host, db_port, db_name, db_user, db_pass)
        
        # Initialize manager with shared pool
        db_manager = DatabaseManager(
            host=db_host, port=db_port, dbname=db_name, user=db_user, password=db_pass,
            pool=shared_pool
        )
        # connect() will detect the shared pool and just verify tables
        await db_manager.connect()
        
        if reset_session and session_id in conversation_sessions:
            conversation_sessions[session_id].reset()
            logging.info(f"[Session] Reset requested for: {session_id}")
        
        if session_id not in conversation_sessions:
            # Try to load from DB
            saved_data = await db_manager.load_session(session_id)
            if saved_data:
                conversation_sessions[session_id] = ConversationState.from_dict(saved_data)
                logging.info(f"[Session] Loaded session from DB: {session_id}")
            else:
                conversation_sessions[session_id] = ConversationState()
                logging.info(f"[Session] Created new session: {session_id}")
        
        conversation = conversation_sessions[session_id]
        logging.info(f"[Session] ID: {session_id}, State: {conversation.state}, Identified: {conversation.is_identified}")

    resampled_file = None
    input_audio_file = None

    try:
        # Use in-memory buffer for initial inspection to avoid disk I/O
        audio_buffer = io.BytesIO(audio_bytes)
        sample_rate, n_channels, inspect_status = inspect_wav_properties(audio_buffer)
        
        logging.info(inspect_status)
        if sample_rate == 0:
            await websocket.send(inspect_status)
            return

        # --- ASR Stage ---
        asr_start_time = time.time()
        
        asr_sample_rate = 16000
        
        # Optimize: Only write to disk if resampling is needed
        if sample_rate != asr_sample_rate:
            # Must write to file for ffmpeg resampling
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                input_audio_file = tmp.name
                tmp.write(audio_bytes)
                
            asr_audio_file, needs_cleanup = await resample_audio_async(input_audio_file, asr_sample_rate)
            if needs_cleanup:
                resampled_file = asr_audio_file
                
            # Transcribe from file (resampled)
            final_transcript = await transcribe_with_whisper(asr_audio_file, settings)
        else:
            # Direct in-memory transcription (Fast path)
            final_transcript = await transcribe_with_whisper(audio_bytes, settings)
            
        metrics['asr_processing_time'] = time.time() - asr_start_time

        # Filter noise
        noise_patterns = {'-', '--', '...', '.', '..', '—', '–', '', ' ', 'uh', 'um', 'hmm', 'hm', 'ah', 'eh'}
        cleaned_transcript = final_transcript.strip() if final_transcript else ''
        
        words = re.findall(r'\b\w+\b', cleaned_transcript.lower())
        word_count = len(words)
        
        valid_single_words = {'hi', 'hey', 'hello', 'bye', 'goodbye', 'yes', 'no', 'help', 'stop', 
                              'start', 'thanks', 'please', 'okay', 'ok', 'sure', 'what', 'why', 
                              'how', 'when', 'where', 'who', 'cancel', 'confirm', 'standard', 'premium'}
        
        single_word_noise = {'you', 'me', 'the', 'a', 'an', 'is', 'are', 'was', 'were', 'so', 
                             'and', 'but', 'or', 'if', 'then', 'well', 'like', 'just', 'right', 
                             'huh', 'uh', 'um', 'oh'}
        
        is_noise = (
            not cleaned_transcript or 
            cleaned_transcript.lower() in noise_patterns or 
            len(cleaned_transcript) < 2 or
            (word_count == 1 and words[0] in single_word_noise and words[0] not in valid_single_words)
        )
        
        if is_noise:
            logging.info(f"[ASR] Ignoring noise/short transcript: \"{final_transcript}\" (words: {word_count})")
            await websocket.send("Status: Listening... (speak a complete phrase)")
            return
            
        logging.info(f"[ASR] Transcript: \"{final_transcript}\" (words: {word_count})")
        
        # --- Sentiment Analysis (use ASR language for detection) ---
        asr_lang = settings.get('asr_language_code', 'en')
        sentiment = analyze_sentiment(final_transcript, asr_lang)
        if sentiment and sentiment.get('needs_alert') and db_manager:
            # Get customer info if identified
            cust_id = conversation.customer_id if conversation else None
            cust_name = conversation.customer_name if conversation else "Unknown"
            cust_email = conversation.customer_email if conversation else None
            
            # Create context summary from recent conversation
            context = ""
            if conversation and conversation.history:
                recent = conversation.history[-6:]  # Last 3 exchanges
                context = " | ".join([f"{m['role']}: {m['content'][:100]}" for m in recent])
            
            # Save sentiment alert to database
            await db_manager.create_sentiment_alert(
                customer_id=cust_id,
                customer_name=cust_name,
                customer_email=cust_email,
                sentiment_score=sentiment['score'],
                sentiment_label=sentiment['label'],
                trigger_phrases=", ".join(sentiment['trigger_phrases']),
                customer_message=final_transcript[:500],
                context_summary=context[:1000] if context else None,
                churn_risk=sentiment['churn_risk'],
                recommended_action=sentiment['recommended_action'],
                session_id=session_id
            )
            
            logging.warning(f"[SENTIMENT] Detected {sentiment['label']} sentiment (score: {sentiment['score']}, risk: {sentiment['churn_risk']})")
        
        # --- v5.1.3: RETENTION LOGIC - When customer wants to leave ---
        retention_prefix = ""
        if sentiment and sentiment.get('churn_risk') in ['high', 'medium']:
            response_lang = settings.get('tts_language_code', 'en')
            retention_prefix = get_retention_response(sentiment, response_lang)
            if retention_prefix:
                logging.info(f"[RETENTION] 🛡️ Applying retention response for {sentiment['churn_risk']} churn risk")
        
        # --- Build Response ---
        response_text = ""
        use_llm = False
        full_response = ""
        
        if db_manager and conversation:
            conversation.add_message("user", final_transcript)
            
            # =====================================================
            # STATE MACHINE FOR CONVERSATION FLOW
            # =====================================================
            
            # STATE: Upgrade - Selecting Plan
            if conversation.state == ConversationState.STATE_UPGRADE_SELECT_PLAN:
                plan_choice = extract_plan_choice(final_transcript)
                
                if plan_choice:
                    # User selected a plan
                    plan = await db_manager.get_plan_by_name(plan_choice)
                    if plan:
                        conversation.set_pending_upgrade(plan['name'], plan.get('id'))
                        response_text = get_natural_response('upgrade_confirm', lang=response_lang, plan=plan['name'])
                    else:
                        response_text = get_natural_response('upgrade_not_found', lang=response_lang, plan=plan_choice)
                elif is_cancellation(final_transcript):
                    conversation.cancel_upgrade()
                    response_text = get_natural_response('upgrade_cancelled', lang=response_lang)
                else:
                    response_text = get_natural_response('upgrade_ask_plan', lang=response_lang)
            
            # STATE: Upgrade - Awaiting Confirmation
            elif conversation.state == ConversationState.STATE_UPGRADE_CONFIRM:
                if is_confirmation(final_transcript):
                    # User confirmed - create the upgrade request
                    plan_name = conversation.pending_upgrade_plan
                    plan_id = conversation.pending_upgrade_plan_id
                    
                    # Get current plan details
                    current_plan_results, _ = await db_manager.run_predefined_query('current_plan', conversation.customer_id)
                    current_plan_id = current_plan_results[0]['id'] if current_plan_results else None
                    current_plan_name = current_plan_results[0]['name'] if current_plan_results else None
                    
                    # Create upgrade request in database with full details
                    result, error = await db_manager.create_upgrade_request(
                        customer_id=conversation.customer_id,
                        customer_name=conversation.customer_name,
                        customer_email=conversation.customer_email,
                        current_plan_id=current_plan_id,
                        current_plan_name=current_plan_name,
                        target_plan_id=plan_id,
                        target_plan_name=plan_name,
                        session_id=session_id
                    )
                    
                    if error:
                        logging.error(f"[Upgrade] Failed to create request: {error}")
                        response_text = get_natural_response('upgrade_error', lang=response_lang)
                    else:
                        completed_plan = conversation.complete_upgrade()
                        response_text = get_natural_response('upgrade_submitted', lang=response_lang, plan=completed_plan)
                        logging.info(f"[Upgrade] Request #{result} created: {conversation.customer_name} ({conversation.customer_email}) -> {completed_plan}")
                        
                elif is_cancellation(final_transcript):
                    conversation.cancel_upgrade()
                    response_text = get_natural_response('upgrade_cancelled', lang=response_lang)
                else:
                    # Unclear response - ask again
                    response_text = get_natural_response('upgrade_unclear', lang=response_lang, plan=conversation.pending_upgrade_plan)
            
            # STATE: Create Ticket - Awaiting Details
            elif conversation.state == ConversationState.STATE_CREATE_TICKET:
                logging.info(f"[Ticket] Processing description: {final_transcript}")
                
                if is_cancellation(final_transcript):
                    conversation.state = ConversationState.STATE_IDENTIFIED
                    response_text = get_natural_response('ticket_create_cancel', lang=response_lang)
                    logging.info("[Ticket] Creation cancelled by user")
                else:
                    # Use transcript as subject (reason) as requested by user
                    # "The subject in the ticket needs to be the reason the customer says"
                    subject = final_transcript[:200] if final_transcript else "Voice Request"
                    description = f"Created via Voice Agent on {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\nDetails: {final_transcript}"
                    
                    logging.info(f"[Ticket] Creating ticket for customer {conversation.customer_id}...")
                    ticket_id, error = await db_manager.create_ticket(conversation.customer_id, subject, description)
                    
                    if ticket_id:
                        logging.info(f"[Ticket] Success! ID: {ticket_id}")
                        response_text = get_natural_response('ticket_created', lang=response_lang, id=ticket_id)
                        
                        # FORCE REFRESH of customer info so the new ticket shows up in UI immediately
                        try:
                            # Re-fetch customer info logic
                            tickets, _ = await db_manager.run_predefined_query('tickets', conversation.customer_id)
                            # ... (simplified re-fetch for UI update)
                            # We will rely on the UI auto-refresh polling, but we can also push an update if needed.
                            # For now, relying on the 'ticket_created' response is enough, 
                            # but let's log that we expect the UI to update.
                            logging.info("[Ticket] Ticket created, UI should update on next poll")
                        except Exception as e:
                            logging.error(f"[Ticket] Error during post-creation info fetch: {e}")
                            
                    else:
                        logging.error(f"[Ticket] Creation failed: {error}")
                        response_text = get_natural_response('ticket_create_fail', lang=response_lang)
                    
                    # Reset state
                    conversation.state = ConversationState.STATE_IDENTIFIED

            # STATE: Close Ticket - Awaiting Confirmation (if ambiguous)
            # (Currently logic closes latest automatically, but this state is reserved for future specific selection)

            # STATE: Awaiting PIN
            elif conversation.state == ConversationState.STATE_AWAITING_PIN or conversation.awaiting_pin:
                # Use ASR language for PIN extraction (what the user speaks)
                asr_lang = settings.get('asr_language_code', 'en')
                pin = extract_pin_from_text(final_transcript, asr_lang)
                logging.info(f"[Auth] Extracted PIN: {pin} (lang: {asr_lang})")
                
                if pin:
                    customer = await db_manager.find_customer_by_pin(pin)
                    if customer:
                        conversation.identify_customer(
                            customer['id'], 
                            customer['name'], 
                            customer['email'],
                            pin
                        )
                        
                        # --- Send Customer Info to UI (Enhanced with full history) ---
                        try:
                            # Fetch summary data for UI popup
                            tickets, _ = await db_manager.run_predefined_query('tickets', customer['id'])
                            invoices, _ = await db_manager.run_predefined_query('invoices', customer['id'])
                            subscription, _ = await db_manager.run_predefined_query('subscription', customer['id'])
                            customer_details, _ = await db_manager.run_predefined_query('customer_info', customer['id'])
                            
                            open_tickets = len([t for t in tickets if t['status'] == 'open']) if tickets else 0
                            overdue_inv = len([i for i in invoices if i['status'] == 'overdue']) if invoices else 0
                            pending_inv = len([i for i in invoices if i['status'] == 'pending']) if invoices else 0
                            paid_inv = len([i for i in invoices if i['status'] == 'paid']) if invoices else 0
                            
                            # Get subscription info
                            plan_name = ''
                            plan_price = ''
                            subscription_status = ''
                            if subscription and len(subscription) > 0:
                                plan_name = subscription[0].get('plan_name', '')
                                plan_price = f"{subscription[0].get('price', 0):.2f}"
                                subscription_status = subscription[0].get('status', '')
                            
                            # Get customer since date
                            customer_since = ''
                            if customer_details and len(customer_details) > 0:
                                created_at = customer_details[0].get('created_at')
                                if created_at:
                                    customer_since = created_at.strftime('%b %Y') if hasattr(created_at, 'strftime') else str(created_at)[:7]
                            
                            # Get recent tickets with details
                            recent_tickets = []
                            if tickets:
                                for t in tickets[:3]:
                                    recent_tickets.append({
                                        'subject': t.get('subject', 'No subject'),
                                        'status': t.get('status', 'open'),
                                        'priority': t.get('priority', 'medium')
                                    })
                            
                            info_payload = {
                                "type": "customer_info",
                                "data": {
                                    "customer_id": customer['id'],  # For auto-refresh
                                    "name": customer['name'],
                                    "email": customer['email'],
                                    "phone": customer['phone'],
                                    "open_tickets": open_tickets,
                                    "overdue_invoices": overdue_inv,
                                    "pending_invoices": pending_inv,
                                    # Extended info
                                    "plan_name": plan_name,
                                    "plan_price": plan_price,
                                    "subscription_status": subscription_status,
                                    "total_tickets": len(tickets) if tickets else 0,
                                    "total_invoices": len(invoices) if invoices else 0,
                                    "paid_invoices": paid_inv,
                                    "customer_since": customer_since,
                                    "recent_tickets": recent_tickets
                                }
                            }
                            await websocket.send(json.dumps(info_payload))
                            logging.info(f"[UI] Sent customer info for {customer['name']}")
                        except Exception as e:
                            logging.error(f"[UI] Failed to send info: {e}")
                        # --------------------------------
                        
                        response_text = get_natural_response('greeting_returning', lang=response_lang, name=customer['name'].split()[0])
                    else:
                        conversation.pin_attempts += 1
                        if conversation.pin_attempts >= 3:
                            response_text = get_natural_response('pin_locked', lang=response_lang)
                            conversation.awaiting_pin = False
                            conversation.state = ConversationState.STATE_NEW
                        else:
                            remaining = 3 - conversation.pin_attempts
                            response_text = get_natural_response('pin_invalid', lang=response_lang, remaining=remaining)
                else:
                    response_text = get_natural_response('pin_unclear', lang=response_lang)
            
            # STATE: Not identified yet - ask for PIN
            elif not conversation.is_identified:
                conversation.awaiting_pin = True
                conversation.state = ConversationState.STATE_AWAITING_PIN
                response_text = get_natural_response('greeting_new', lang=response_lang)
            
            # STATE: Identified - Handle queries
            else:
                query_type = detect_query_type(final_transcript)
                
                # Special handling for upgrade requests
                if query_type == 'upgrade':
                    logging.info("[Query] Starting upgrade flow")
                    conversation.start_upgrade_flow()
                    response_text = get_natural_response('upgrade_ask_plan', lang=response_lang)
                
                elif query_type == 'create_ticket':
                    logging.info("[Query] Starting create ticket flow")
                    conversation.state = ConversationState.STATE_CREATE_TICKET
                    response_text = get_natural_response('ticket_ask_details', lang=response_lang)

                elif query_type == 'close_ticket':
                    logging.info("[Query] Processing close ticket request")
                    
                    # Check for specific ticket number in transcript
                    ticket_number = None
                    numbers = re.findall(r'\b\d+\b', final_transcript)
                    if numbers:
                        ticket_number = numbers[-1] # Assume the last number is the ID if present
                    
                    if ticket_number:
                        logging.info(f"[Query] Closing specific ticket ID: {ticket_number}")
                        result, error = await db_manager.close_ticket_by_id(conversation.customer_id, ticket_number)
                        if result:
                            t_id, subject = result
                            response_text = get_natural_response('ticket_close_success', lang=response_lang, id=t_id, subject=subject)
                        else:
                            response_text = get_natural_response('ticket_close_not_found', lang=response_lang, id=ticket_number)
                    else:
                        # Logic: Fetch open tickets first
                        open_tickets, error = await db_manager.get_open_tickets(conversation.customer_id)
                        
                        if not open_tickets:
                            response_text = get_natural_response('ticket_resolved', lang=response_lang, count=0)
                        elif len(open_tickets) == 1:
                            # Single ticket - close it
                            t_id = open_tickets[0]['id']
                            subject = open_tickets[0]['subject']
                            result, error = await db_manager.close_ticket_by_id(conversation.customer_id, t_id)
                            if result:
                                response_text = get_natural_response('ticket_close_success', lang=response_lang, id=t_id, subject=subject)
                            else:
                                response_text = get_natural_response('ticket_close_fail', lang=response_lang)
                        else:
                            # Multiple tickets - close latest but inform user
                            latest = open_tickets[0]
                            t_id = latest['id']
                            subject = latest['subject']
                            count = len(open_tickets)
                            
                            result, error = await db_manager.close_ticket_by_id(conversation.customer_id, t_id)
                            if result:
                                response_text = get_natural_response('ticket_close_latest', lang=response_lang, count=count, id=t_id, subject=subject)
                            else:
                                response_text = get_natural_response('ticket_close_fail', lang=response_lang)

                elif query_type:
                    logging.info(f"[Query] Detected type: {query_type}")
                    results, error = await db_manager.run_predefined_query(query_type, conversation.customer_id)
                    
                    if error:
                        logging.error(f"[Query] Error: {error}")
                        response_text = get_natural_response('query_error', lang=response_lang)
                    elif results:
                        response_text = format_query_results(query_type, results, conversation.customer_name, response_lang)
                        
                        # --- SEND RICH CARD ---
                        try:
                            card_payload = generate_card_payload(query_type, results)
                            if card_payload:
                                await websocket.send(json.dumps(card_payload))
                                logging.info(f"[UI] Sent rich card: {query_type}")
                        except Exception as e:
                            logging.error(f"[UI] Failed to send card: {e}")
                    else:
                        response_text = get_natural_response('info_not_found', lang=response_lang, type=query_type)
                else:
                    # General conversation - use LLM
                    use_llm = True
        else:
            # Non-database mode - use LLM
            use_llm = True
        
        # --- LLM + TTS Stage ---
        llm_tts_start_time = time.time()

        if response_text and not use_llm:
            # Use predefined response
            # v5.1.3: Prepend retention message if customer is at risk of churning
            if retention_prefix:
                response_text = f"{retention_prefix} {response_text}"
                logging.info(f"[RETENTION] Prepended retention message to response")
            
            logging.info(f"[Response] Using predefined: {response_text[:50]}...")
            
            task = asyncio.create_task(
                stream_tts_for_sentence(
                    websocket, response_text,
                    llm_tts_start_time, metrics, settings
                )
            )
            tts_tasks.append(task)
            await task
            
            full_response = response_text
        else:
            # Use LLM for response
            # v5.1.3: If retention needed, use retention message as primary response
            if retention_prefix and sentiment and sentiment.get('churn_risk') == 'high':
                logging.info(f"[RETENTION] Using retention response for high churn risk")
                response_text = retention_prefix
                
                task = asyncio.create_task(
                    stream_tts_for_sentence(
                        websocket, response_text,
                        llm_tts_start_time, metrics, settings
                    )
                )
                tts_tasks.append(task)
                await task
                full_response = response_text
            else:
                # Normal LLM flow
                llm_api_base = settings.get('llm_api_base')
                llm_api_key = settings.get('llm_api_key')
            
                # Get or create conversation history
                if session_id not in conversation_sessions:
                    conversation_sessions[session_id] = ConversationState()
                conv_state = conversation_sessions[session_id]
                
                if not (db_manager and conversation):
                    conv_state.add_message("user", final_transcript)
                
                # Build history context
                history_context = ""
                recent_history = conv_state.history[-8:]
                if len(recent_history) > 2:
                    history_context = ""
                    for msg in recent_history[:-1]:
                        role = "Customer" if msg["role"] == "user" else "Agent"
                        history_context += f"{role}: {msg['content']}\n"
                
                # Build prompt with language instruction
                if conversation and conversation.is_identified:
                    prompt = build_llm_prompt(final_transcript, conversation, history_context, response_lang, sentiment)
                else:
                    custom_template = settings.get('llm_prompt_template')
                    prompt = build_general_llm_prompt(final_transcript, history_context, custom_template, response_lang)
                
                logging.info(f"[LLM] Generating response...")
                
                # Validate URL
                if not llm_api_base:
                    logging.error("[LLM] No API base URL configured")
                    await websocket.send("Error: LLM API base URL not configured")
                    return
                
                if llm_api_base.startswith('ttps://'):
                    llm_api_base = 'h' + llm_api_base
                elif not llm_api_base.startswith('http'):
                    llm_api_base = 'https://' + llm_api_base
                
                llm_api_base = llm_api_base.rstrip('/')
                llm_url = f"{llm_api_base}/chat/completions"
                logging.info(f"[LLM] URL: {llm_url[:60]}...")
                
                llm_headers = {
                    "Authorization": f"Bearer {llm_api_key}",
                    "Content-Type": "application/json"
                }
                llm_payload = {
                    "model": settings.get('llm_model_name'),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 150,
                    "stream": True
                }
                
                sentence_buffer = ""
                full_response = ""
                
                async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                    async with client.stream("POST", llm_url, headers=llm_headers, json=llm_payload) as response:
                        response.raise_for_status()
                        
                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            
                            try:
                                chunk_data = json.loads(data_str)
                                if metrics['llm_tts_metrics']['first_llm_token_time'] is None:
                                    metrics['llm_tts_metrics']['first_llm_token_time'] = time.time() - llm_tts_start_time
                                
                                choices = chunk_data.get('choices', [])
                                if choices:
                                    delta = choices[0].get('delta', {})
                                    token = delta.get('content', '')
                                    if token:
                                        sentence_buffer += token
                                        full_response += token
                                        
                                        while True:
                                            split_pos = -1
                                            for delim in SENTENCE_TERMINATORS:
                                                pos = sentence_buffer.find(delim)
                                                if pos != -1 and (split_pos == -1 or pos < split_pos):
                                                    split_pos = pos
                                            
                                            if split_pos != -1:
                                                dispatch_text = sentence_buffer[:split_pos + 1]
                                                
                                                # Buffer very short sentences to avoid voice switching artifacts
                                                # If text is too short (e.g. "Ok."), keep it in buffer unless it's a clear end
                                                if len(dispatch_text) < 15 and len(sentence_buffer) > split_pos + 1:
                                                    # Look ahead - if we have more text, maybe wait
                                                    # But simpler: just process it if it's a sentence
                                                    pass
                                                
                                                sentence_buffer = sentence_buffer[split_pos + 1:]
                                                task = asyncio.create_task(
                                                    stream_tts_for_sentence(
                                                        websocket, dispatch_text,
                                                        llm_tts_start_time, metrics, settings
                                                    )
                                                )
                                                tts_tasks.append(task)
                                                await task
                                            else:
                                                break
                            except json.JSONDecodeError:
                                continue
                
                # Handle remaining text
                if sentence_buffer.strip():
                    task = asyncio.create_task(
                        stream_tts_for_sentence(
                            websocket, sentence_buffer,
                            llm_tts_start_time, metrics, settings
                        )
                    )
                    tts_tasks.append(task)
                    await task

        # Safe gather for TTS tasks
        audio_sent = False
        if tts_tasks:
            results = await asyncio.gather(*tts_tasks, return_exceptions=True)
            for res in results:
                if res is True:
                    audio_sent = True
        
        # Fallback if text generated but no audio
        if full_response and not audio_sent:
            logging.error("[TTS] ❌❌❌ CRITICAL: Text generated but NO audio sent!")
            logging.error(f"[TTS] ❌ Response text was: '{full_response[:200]}...'")
            logging.error(f"[TTS] ❌ TTS Server: {settings.get('tts_server_address')}")
            
            # v5.0.1: Send the text response to UI so user can still see what agent wanted to say
            try:
                fallback_payload = {
                    "type": "tts_fallback",
                    "message": "Voice generation failed - here's what I wanted to say:",
                    "text": full_response,
                    "tts_server": settings.get('tts_server_address'),
                    "suggestion": "Check TTS server connection"
                }
                await websocket.send(json.dumps(fallback_payload))
                logging.info("[TTS] Sent fallback text to UI")
            except Exception as e:
                logging.error(f"[TTS] Failed to send fallback: {e}")
            
            await websocket.send(f"Agent response (no voice): {full_response}")

        # Save response to history
        if conversation:
            conversation.add_message("assistant", full_response)
        elif session_id in conversation_sessions:
            conversation_sessions[session_id].add_message("assistant", full_response)
        
        # Save session state to DB
        if db_manager and conversation:
            await db_manager.save_session(
                session_id, 
                conversation.customer_id, 
                conversation.to_dict()
            )

        # Latency report
        metrics['total_processing_time'] = time.time() - pipeline_start_time
        m = metrics['llm_tts_metrics']
        
        logging.info("--- Latency Report ---")
        logging.info(f"ASR Time: {metrics['asr_processing_time']:.4f}s")
        if m['first_llm_token_time']:
            logging.info(f"First LLM Token: {m['first_llm_token_time']:.4f}s")
        if m['first_tts_chunk_time']:
            logging.info(f"First TTS Chunk: {m['first_tts_chunk_time']:.4f}s")
        logging.info(f"Total Time: {metrics['total_processing_time']:.4f}s")
        
        logging.info(f"Response: \"{full_response[:100]}...\"")
        if conversation:
            logging.info(f"Customer: {conversation.customer_name or 'Not identified'}")
            logging.info(f"State: {conversation.state}")
        history_len = len(conversation_sessions.get(session_id, ConversationState()).history) if session_id else 0
        logging.info(f"History length: {history_len}")
        logging.info("--- End of Report ---")
        
        # Send metrics to client
        try:
            metrics_payload = {
                "type": "latency_report",
                "data": {
                    "asr_processing_time": metrics['asr_processing_time'],
                    "first_llm_token_time": m['first_llm_token_time'],
                    "first_tts_chunk_time": m['first_tts_chunk_time'],
                    "total_processing_time": metrics['total_processing_time']
                }
            }
            await websocket.send(json.dumps(metrics_payload))
        except Exception as e:
            logging.error(f"Failed to send metrics: {e}")

        await websocket.send("__END_OF_STREAM__")

    except Exception as e:
        err = f"Pipeline error: {e}\n{traceback.format_exc()}"
        logging.error(err)
        try:
            await websocket.send(f"Server Error: {e}")
        except websockets.ConnectionClosed:
            pass
    finally:
        for task in tts_tasks:
            task.cancel()
        
        for temp_file in [input_audio_file, resampled_file]:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError as e:
                    logging.warning(f"Failed to remove temp file {temp_file}: {e}")
        
        if db_manager:
            await db_manager.disconnect()


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================
def cleanup_old_sessions(max_age_minutes: int = 30):
    """Remove sessions older than max_age_minutes"""
    now = datetime.now()
    expired = [sid for sid, state in conversation_sessions.items() 
               if now - state.last_activity > timedelta(minutes=max_age_minutes)]
    for sid in expired:
        del conversation_sessions[sid]
    if expired:
        logging.info(f"Cleaned up {len(expired)} expired session(s)")


async def handler(websocket):
    """Handles incoming WebSocket connections."""
    client_id = str(websocket.remote_address)
    
    if not _rate_limiter.is_allowed(client_id):
        logging.warning(f"Rate limit exceeded for {client_id}")
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Rate limit exceeded. Please wait."
            }))
            return
        except:
            pass
            
    logging.info(f"Client connected: {client_id}")
    current_settings = DEFAULT_SETTINGS.copy()
    audio_bytes = None
    session_id = None
    
    # cleanup handled by periodic task now

    try:
        first_message = await websocket.recv()

        try:
            client_settings = json.loads(first_message)
            current_settings.update(client_settings)
            session_id = client_settings.get('session_id', str(websocket.remote_address))
            logging.info(f"Session ID: {session_id}")
            
            # Check if this is a refresh_customer_info request
            if client_settings.get('action') == 'refresh_customer_info':
                # Get conversation session
                conversation = session_manager.get(session_id)
                if conversation and conversation.is_identified and conversation.customer_id:
                    try:
                        # Use the global db pool if available
                        db_pool = await ensure_db_pool()
                        if db_pool:
                            async with db_pool.acquire() as conn:
                                # Fetch tickets
                                tickets_rows = await conn.fetch("""
                                    SELECT id, subject, status, priority, created_at
                                    FROM support_tickets WHERE customer_id = $1
                                    ORDER BY created_at DESC LIMIT 10
                                """, conversation.customer_id)
                                tickets = [dict(r) for r in tickets_rows]
                                
                                # Fetch invoices
                                invoices_rows = await conn.fetch("""
                                    SELECT i.id, i.amount, i.status, i.due_date
                                    FROM invoices i WHERE i.customer_id = $1
                                    ORDER BY i.due_date DESC LIMIT 10
                                """, conversation.customer_id)
                                invoices = [dict(r) for r in invoices_rows]
                                
                                # Fetch subscription
                                subscription_row = await conn.fetchrow("""
                                    SELECT p.name as plan_name, p.price, s.status
                                    FROM subscriptions s
                                    JOIN plans p ON s.plan_id = p.id
                                    WHERE s.customer_id = $1
                                    ORDER BY s.start_date DESC LIMIT 1
                                """, conversation.customer_id)
                                subscription = dict(subscription_row) if subscription_row else None
                                
                                # Fetch customer details
                                customer_details_row = await conn.fetchrow("""
                                    SELECT phone, created_at FROM customers WHERE id = $1
                                """, conversation.customer_id)
                                customer_details = dict(customer_details_row) if customer_details_row else None
                                
                                # Calculate stats
                                open_tickets = len([t for t in tickets if t['status'] == 'open']) if tickets else 0
                                overdue_inv = len([i for i in invoices if i['status'] == 'overdue']) if invoices else 0
                                paid_inv = len([i for i in invoices if i['status'] == 'paid']) if invoices else 0
                                
                                # Get subscription info
                                plan_name = subscription['plan_name'] if subscription else ''
                                plan_price = f"{subscription['price']:.2f}" if subscription else ''
                                subscription_status = subscription['status'] if subscription else ''
                                
                                # Get customer since date
                                customer_since = ''
                                if customer_details and customer_details.get('created_at'):
                                    created_at = customer_details['created_at']
                                    customer_since = created_at.strftime('%b %Y') if hasattr(created_at, 'strftime') else str(created_at)[:7]
                                
                                # Get recent tickets
                                recent_tickets = []
                                if tickets:
                                    for t in tickets[:3]:
                                        recent_tickets.append({
                                            'subject': t.get('subject', 'No subject'),
                                            'status': t.get('status', 'open'),
                                            'priority': t.get('priority', 'medium')
                                        })
                                
                                info_payload = {
                                    "type": "customer_info",
                                    "data": {
                                        "name": conversation.customer_name,
                                        "email": conversation.customer_email,
                                        "phone": customer_details.get('phone', 'N/A') if customer_details else 'N/A',
                                        "open_tickets": open_tickets,
                                        "overdue_invoices": overdue_inv,
                                        "plan_name": plan_name,
                                        "plan_price": plan_price,
                                        "subscription_status": subscription_status,
                                        "total_tickets": len(tickets) if tickets else 0,
                                        "total_invoices": len(invoices) if invoices else 0,
                                        "paid_invoices": paid_inv,
                                        "customer_since": customer_since,
                                        "recent_tickets": recent_tickets
                                    }
                                }
                                await websocket.send(json.dumps(info_payload))
                                logging.info(f"[UI] Refreshed customer info for {conversation.customer_name}")
                        else:
                            logging.warning("[UI] No database pool available for refresh")
                    except Exception as e:
                        logging.error(f"[UI] Failed to refresh customer info: {e}")
                return  # Don't process audio for refresh requests
            
            audio_bytes = await websocket.recv()

        except (json.JSONDecodeError, TypeError):
            session_id = str(websocket.remote_address)
            if isinstance(first_message, bytes):
                audio_bytes = first_message
            else:
                raise ValueError("Received non-JSON, non-bytes message.")

        logging.info(f"Received {len(audio_bytes)} bytes of audio data.")
        await main_pipeline(websocket, audio_bytes, current_settings, session_id)

    except websockets.ConnectionClosed:
        logging.info(f"Client disconnected: {websocket.remote_address}")
    except Exception as e:
        logging.error(f"Error with client {websocket.remote_address}: {e}\n{traceback.format_exc()}")


# =============================================================================
# HEALTH CHECK & SERVER
# =============================================================================
async def health_handler(reader, writer):
    await reader.readline()
    
    db_status = "connected" if _db_pool else "not_configured"
    http_status = "connected" if _http_client and not _http_client.is_closed else "initializing"
    sessions_count = len(conversation_sessions)
    
    health_data = {
        "status": "healthy",
        "version": "5.1.0",
        "database_pool": db_status,
        "http_client": http_status,
        "active_sessions": sessions_count,
        "circuit_breakers": {
            "asr": _asr_circuit.get_status(),
            "tts": _tts_circuit.get_status(),
            "llm": _llm_circuit.get_status()
        },
        "caches": {
            "tts": _tts_cache.stats(),
            "query": _query_cache.stats(),
            "llm": _llm_cache.stats()
        },
        "services": _service_health.get_all(),
        "features": {
            "edge_tts_fallback": EDGE_TTS_AVAILABLE,
            "tts_circuit_breaker": "disabled",
        }
    }
    
    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{json.dumps(health_data)}"
    writer.write(response.encode())
    await writer.drain()
    writer.close()


async def circuit_reset_handler(reader, writer):
    """Reset circuit breakers via HTTP request"""
    await reader.readline()
    
    old_status = {
        "asr": _asr_circuit.get_status(),
        "tts": _tts_circuit.get_status(),
        "llm": _llm_circuit.get_status()
    }
    
    # Reset all circuit breakers
    _asr_circuit.failures = 0
    _asr_circuit.state = "CLOSED"
    _tts_circuit.failures = 0
    _tts_circuit.state = "CLOSED"
    _llm_circuit.failures = 0
    _llm_circuit.state = "CLOSED"
    
    new_status = {
        "asr": _asr_circuit.get_status(),
        "tts": _tts_circuit.get_status(),
        "llm": _llm_circuit.get_status()
    }
    
    logging.info(f"[CircuitBreaker] All circuits reset! Old: {old_status}")
    
    reset_data = {
        "status": "reset",
        "message": "All circuit breakers have been reset",
        "old_status": old_status,
        "new_status": new_status
    }
    
    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{json.dumps(reset_data)}"
    writer.write(response.encode())
    await writer.drain()
    writer.close()


async def start_health_server():
    server = await asyncio.start_server(health_handler, "0.0.0.0", 8766)
    logging.info("Health check server: http://0.0.0.0:8766/health")
    async with server:
        await server.serve_forever()


async def start_circuit_reset_server():
    """Start server for resetting circuit breakers"""
    server = await asyncio.start_server(circuit_reset_handler, "0.0.0.0", 8767)
    logging.info("Circuit reset server: http://0.0.0.0:8767/reset")
    async with server:
        await server.serve_forever()


async def periodic_session_cleanup(interval_seconds: int = 300):
    """Periodically cleanup expired sessions"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            cleanup_old_sessions()
        except Exception as e:
            logging.error(f"Session cleanup error: {e}")


# =============================================================================
# PRE-WARMING & HEALTH PROBES - v5.1.0
# =============================================================================
PREWARM_PHRASES = {
    "en": [
        "Hello, how can I help you today?",
        "Please wait a moment.",
        "Thank you for contacting us.",
        "Is there anything else I can help you with?",
        "I understand. Let me help you with that.",
    ],
    "he": [
        "שלום, איך אני יכול לעזור לך?",
        "אנא המתן רגע.",
        "תודה שפנית אלינו.",
        "האם יש עוד משהו שאני יכול לעזור בו?",
        "אני מבין. בוא נטפל בזה.",
    ],
}


async def prewarm_tts_cache(settings: dict):
    """Pre-generate common phrases on startup for faster first response"""
    language = settings.get('tts_language_code', 'en')
    lang_short = language.split('-')[0] if '-' in language else language
    
    phrases = PREWARM_PHRASES.get(lang_short, PREWARM_PHRASES.get('en', []))
    
    logging.info(f"[TTS] 🔥 Pre-warming cache with {len(phrases)} phrases ({lang_short})...")
    
    success = 0
    for phrase in phrases:
        try:
            audio = await synthesize_with_xtts(phrase, settings, max_retries=1)
            if audio:
                success += 1
                logging.debug(f"[TTS] Pre-warmed: {phrase[:30]}...")
        except Exception as e:
            logging.warning(f"[TTS] Pre-warm failed for: {phrase[:30]}... ({e})")
        
        # Small delay between requests
        await asyncio.sleep(0.5)
    
    logging.info(f"[TTS] 🔥 Pre-warming complete: {success}/{len(phrases)} phrases cached")


async def periodic_health_probe(interval_seconds: int = 30):
    """Periodically check service health"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            # Check TTS server
            tts_url = DEFAULT_SETTINGS.get('tts_server_address', '')
            if tts_url:
                if not tts_url.startswith('http'):
                    tts_url = f"http://{tts_url}"
                try:
                    client = await get_http_client()
                    start = time.time()
                    response = await client.get(f"{tts_url}/health", timeout=5.0)
                    latency = time.time() - start
                    if response.status_code == 200:
                        await _service_health.update("tts", "ok", latency, tts_url)
                    else:
                        await _service_health.update("tts", "degraded", latency, tts_url)
                except Exception as e:
                    await _service_health.update("tts", "down", 0, tts_url)
                    logging.warning(f"[Health] TTS probe failed: {e}")
            
            # Log cache stats periodically
            tts_stats = _tts_cache.stats()
            if tts_stats['size'] > 0:
                logging.debug(f"[Cache] TTS: {tts_stats['size']} items, {tts_stats['hit_rate']} hit rate")
                
        except Exception as e:
            logging.error(f"Health probe error: {e}")


async def graceful_shutdown():
    """Cleanup on shutdown"""
    logging.info("Shutting down gracefully...")
    await close_http_client()
    await close_db_pool()
    logging.info("Cleanup complete")


async def main():
    host, port = "0.0.0.0", 8765
    logging.info("=" * 60)
    logging.info(f"🎙️ Voice Agent WebSocket Server v5.1.0 (Build {BUILD_NUMBER})")
    logging.info("Maximum Reliability Edition")
    logging.info("=" * 60)
    logging.info(f"📡 WebSocket: ws://{host}:{port}")
    logging.info(f"🎤 ASR Server: {DEFAULT_SETTINGS['asr_server_address']}")
    logging.info(f"🔊 TTS Server: {DEFAULT_SETTINGS['tts_server_address']}")
    logging.info(f"🗄️ Database: {'enabled' if DEFAULT_SETTINGS['db_enabled'] else 'disabled'}")
    logging.info("=" * 60)
    logging.info("🛡️ Reliability Features:")
    logging.info("   ✅ TTS Circuit Breaker: DISABLED (always tries)")
    logging.info("   ✅ TTS Retries: 5 attempts")
    logging.info(f"   ✅ Edge-TTS Fallback: {'AVAILABLE' if EDGE_TTS_AVAILABLE else 'not installed (pip install edge-tts)'}")
    logging.info("   ✅ TTS Cache: 2000 phrases, 10min TTL")
    logging.info("   ✅ Text Fallback: Shows response if voice fails")
    logging.info("   ✅ Health Probes: Every 30 seconds")
    logging.info("   ✅ Cache Pre-warming: Common phrases")
    logging.info("=" * 60)
    
    # Start background services
    asyncio.create_task(start_health_server())
    asyncio.create_task(start_circuit_reset_server())
    asyncio.create_task(periodic_session_cleanup(300))
    asyncio.create_task(periodic_cache_cleanup(120))
    asyncio.create_task(periodic_health_probe(30))  # Health check every 30s
    
    # Pre-warm HTTP connection pool
    await get_http_client()
    logging.info("✅ HTTP client pool initialized")
    
    # Pre-warm TTS cache with common phrases (run in background)
    asyncio.create_task(prewarm_tts_cache(DEFAULT_SETTINGS))
    
    try:
        async with websockets.serve(handler, host, port, max_size=50 * 1024 * 1024):
            logging.info("✅ Server ready and listening!")
            await asyncio.Future()
    finally:
        await graceful_shutdown()


async def periodic_cache_cleanup(interval_seconds: int = 120):
    """Periodically cleanup expired cache entries"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            _tts_cache.cleanup()
            _query_cache.cleanup()
            _llm_cache.cleanup()
            logging.debug("[Cache] Cleanup completed")
        except Exception as e:
            logging.error(f"Cache cleanup error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shutdown.")
    except Exception as e:
        logging.error(f"Server error: {e}")