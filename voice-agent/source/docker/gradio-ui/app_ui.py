import gradio as gr
import websockets
import asyncio
import os
import traceback
import struct
import json
import tempfile
import requests
import urllib3
import warnings
from typing import AsyncGenerator
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

# Suppress SSL warnings for internal services
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@contextmanager
def db_connection(host, port, dbname, user, password):
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname, user=user, password=password, connect_timeout=3
        )
        yield conn
    except Exception as e:
        print(f"[DB] Connection error: {e}")
        raise e
    finally:
        if conn:
            conn.close()

# Suppress pandas SQLAlchemy warning (we use psycopg2 directly, which works fine)
warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy.*')

# Build Metadata
BUILD_NUMBER = "5.1.4"

# === CRITICAL FIX: Patch Audio component BEFORE any usage ===
def apply_audio_patch():
    """Patch the Audio component's preprocess method to ensure all required attributes exist"""
    from gradio.components.audio import Audio
    
    # Create shared temp directory
    temp_dir = Path(tempfile.gettempdir()) / "gradio"
    temp_dir.mkdir(exist_ok=True)
    
    # Store original preprocess
    if not hasattr(Audio, '_original_preprocess'):
        Audio._original_preprocess = Audio.preprocess
    
    def patched_preprocess(self, payload):
        # Ensure ALL required attributes exist before processing
        if not hasattr(self, 'temp_files'):
            self.temp_files = set()
        if not hasattr(self, 'DEFAULT_TEMP_DIR'):
            self.DEFAULT_TEMP_DIR = str(temp_dir)
        if not hasattr(self, 'type'):
            self.type = "filepath"
        if not hasattr(self, 'format'):
            self.format = "wav"
        if not hasattr(self, 'streaming'):
            self.streaming = False
        
        # Call original preprocess
        return Audio._original_preprocess(self, payload)
    
    Audio.preprocess = patched_preprocess
    print(f"✅ Audio.preprocess patched with temp_dir: {temp_dir}")

# Apply patch immediately
apply_audio_patch()

# =====================================================
# Global Customer Info & Metrics Storage (by session_id)
# This persists info across process_audio calls
# =====================================================
_customer_info_cache = {}  # {session_id: {"info": {...}, "timestamp": ...}}
_metrics_cache = {}        # {session_id: {"markdown": str, "timestamp": ...}}
_CACHE_TTL_SECONDS = 3600  # 1 hour TTL for customer info

# Metrics persistence file (permanent storage)
METRICS_CACHE_FILE = Path("gradio_metrics_cache.json")

# Default metrics placeholder
METRICS_PLACEHOLDER = """### ⏱️ Latency Report
* **Processing:** -
* **LLM First Token:** -
* **TTS First Chunk:** -
* **Total Time:** -
"""

def load_metrics_cache():
    """Load metrics cache from disk"""
    global _metrics_cache
    if METRICS_CACHE_FILE.exists():
        try:
            with open(METRICS_CACHE_FILE, 'r') as f:
                _metrics_cache = json.load(f)
            print(f"✅ Loaded {len(_metrics_cache)} metrics entries from disk")
        except Exception as e:
            print(f"⚠️ Failed to load metrics cache: {e}")
            _metrics_cache = {}

def save_metrics_cache():
    """Save metrics cache to disk"""
    try:
        with open(METRICS_CACHE_FILE, 'w') as f:
            json.dump(_metrics_cache, f)
    except Exception as e:
        print(f"⚠️ Failed to save metrics cache: {e}")

# Load on startup
load_metrics_cache()

def get_metrics_cache(session_id: str) -> str:
    """Get last metrics markdown from cache (no TTL, persistent)"""
    if session_id in _metrics_cache:
        entry = _metrics_cache[session_id]
        if isinstance(entry, dict) and "markdown" in entry:
            return entry["markdown"]
        # Handle legacy or simple format
        return str(entry)
    return None

def set_metrics_cache(session_id: str, markdown: str):
    """Store metrics markdown in cache and persist to disk (non-blocking attempt)"""
    import time
    _metrics_cache[session_id] = {
        "markdown": markdown,
        "timestamp": time.time()
    }
    # Don't block the main thread for disk I/O
    # In a real async app we'd use aiofiles or run_in_executor, 
    # but here we just want to avoid freezing the UI loop if possible.
    # Since this function is called from async loop, we should technically await it,
    # but it's a sync function.
    try:
        # Simple optimization: only save every 5th update or if critical
        # For now, just save.
        save_metrics_cache()
    except Exception:
        pass

def get_customer_info(session_id: str) -> dict:
    """Get customer info from cache"""
    import time
    if session_id in _customer_info_cache:
        entry = _customer_info_cache[session_id]
        # Check TTL
        if time.time() - entry.get("timestamp", 0) < _CACHE_TTL_SECONDS:
            return entry.get("info")
        else:
            # Expired, remove it
            del _customer_info_cache[session_id]
    return None

def set_customer_info(session_id: str, info: dict):
    """Store customer info in cache"""
    import time
    _customer_info_cache[session_id] = {
        "info": info,
        "timestamp": time.time()
    }
    # Cleanup old entries (simple LRU-like)
    if len(_customer_info_cache) > 1000:
        oldest = min(_customer_info_cache.keys(), key=lambda k: _customer_info_cache[k]["timestamp"])
        del _customer_info_cache[oldest]

def clear_customer_info(session_id: str):
    """Clear customer info from cache"""
    if session_id in _customer_info_cache:
        del _customer_info_cache[session_id]

def format_customer_info_html(info: dict) -> str:
    """Format customer info as HTML for display - Enhanced version with full history"""
    if not info or not info.get('name') or info.get('name') == '-':
        return '''<div id="customer-info-content" data-customer-id="" style="padding: 24px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-radius: 16px; border: 2px dashed #adb5bd; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
            <div style="color: #495057; font-size: 16px; text-align: center;">
                <div style="font-size: 48px; margin-bottom: 12px; opacity: 0.7;">👤</div>
                <strong style="color: #343a40;">Waiting for customer identification...</strong>
            </div>
        </div>'''
    
    # Extract data with defaults
    customer_id = info.get('customer_id', '')
    name = info.get('name', 'Unknown')
    email = info.get('email', 'N/A')
    phone = info.get('phone', 'N/A')
    open_tickets = info.get('open_tickets', 0)
    overdue_invoices = info.get('overdue_invoices', 0)
    
    # Enhanced data (if available)
    plan_name = info.get('plan_name', '')
    plan_price = info.get('plan_price', '')
    subscription_status = info.get('subscription_status', '')
    total_tickets = info.get('total_tickets', 0)
    total_invoices = info.get('total_invoices', 0)
    paid_invoices = info.get('paid_invoices', 0)
    customer_since = info.get('customer_since', '')
    recent_tickets = info.get('recent_tickets', [])
    
    # Status indicators - vibrant colors
    ticket_color = '#dc3545' if open_tickets > 0 else '#198754'
    invoice_color = '#dc3545' if overdue_invoices > 0 else '#198754'
    
    # Build subscription info section
    subscription_html = ''
    if plan_name:
        status_badge = ''
        if subscription_status == 'active':
            status_badge = '<span style="background: #198754; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; margin-left: 10px; text-transform: uppercase; letter-spacing: 0.5px;">Active</span>'
        elif subscription_status:
            status_badge = f'<span style="background: #fd7e14; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; margin-left: 10px; text-transform: uppercase;">{subscription_status.title()}</span>'
        
        subscription_html = f'''
        <div style="background: linear-gradient(135deg, #e7f5ff 0%, #d0ebff 100%); padding: 14px 16px; border-radius: 12px; margin-top: 14px; border-left: 4px solid #1971c2;">
            <div style="font-weight: 700; color: #1864ab; font-size: 14px; margin-bottom: 6px;">📦 Subscription</div>
            <div style="font-size: 16px; color: #212529;">
                <strong style="color: #212529;">{plan_name}</strong>
                <span style="color: #198754; font-weight: 700; margin-left: 4px;">${plan_price}/mo</span>
                {status_badge}
            </div>
        </div>'''
    
    # Build recent tickets section
    tickets_html = ''
    if recent_tickets:
        ticket_items = ''
        for ticket in recent_tickets[:3]:
            priority_color = {'high': '#dc3545', 'medium': '#fd7e14', 'low': '#198754'}.get(ticket.get('priority', 'medium'), '#6c757d')
            status_text = ticket.get('status', 'open')
            status_style = 'color: #dc3545; font-weight: 600;' if status_text == 'open' else 'color: #6c757d;'
            ticket_items += f'''<div style="padding: 10px 0; border-bottom: 1px solid #dee2e6; font-size: 14px;">
                <span style="color: {priority_color}; font-size: 10px; vertical-align: middle;">⬤</span>
                <span style="color: #212529; font-weight: 500; margin-left: 6px;">{ticket.get('subject', 'No subject')[:35]}</span>
                <span style="{status_style} font-size: 12px; margin-left: 6px;">({status_text})</span>
            </div>'''
        tickets_html = f'''
        <div style="background: linear-gradient(135deg, #fff9db 0%, #fff3bf 100%); padding: 14px 16px; border-radius: 12px; margin-top: 14px; border-left: 4px solid #f59f00;">
            <div style="font-weight: 700; color: #e67700; font-size: 14px; margin-bottom: 6px;">🎫 Recent Tickets</div>
            {ticket_items}
        </div>'''
    
    # Summary section
    summary_html = ''
    if total_tickets > 0 or total_invoices > 0:
        summary_html = f'''
        <div style="display: flex; gap: 12px; margin-top: 14px;">
            <div style="flex: 1; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); padding: 14px; border-radius: 12px; text-align: center; border: 1px solid #dee2e6;">
                <div style="font-size: 28px; font-weight: 800; color: #212529;">{total_tickets}</div>
                <div style="font-size: 11px; color: #495057; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Total Tickets</div>
            </div>
            <div style="flex: 1; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); padding: 14px; border-radius: 12px; text-align: center; border: 1px solid #dee2e6;">
                <div style="font-size: 28px; font-weight: 800; color: #212529;">{paid_invoices}/{total_invoices}</div>
                <div style="font-size: 11px; color: #495057; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Invoices Paid</div>
            </div>
        </div>'''
    
    # Quick Actions section (visible only when customer is identified)
    actions_html = f'''
        <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e9ecef;">
            <div style="font-weight: 700; color: #495057; font-size: 13px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px;">⚡ Quick Actions</div>
            <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <button onclick="window.customerActions.closeTicket({customer_id})" 
                    style="padding: 8px 14px; font-size: 12px; font-weight: 600; background: linear-gradient(135deg, #198754, #20c997); color: white; border: none; border-radius: 8px; cursor: pointer; transition: transform 0.1s, box-shadow 0.1s;"
                    onmouseover="this.style.transform='scale(1.02)'; this.style.boxShadow='0 2px 8px rgba(25,135,84,0.4)'"
                    onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none'">
                    ✅ Close Ticket
                </button>
                <button onclick="window.customerActions.updateTicket({customer_id})" 
                    style="padding: 8px 14px; font-size: 12px; font-weight: 600; background: linear-gradient(135deg, #0d6efd, #6610f2); color: white; border: none; border-radius: 8px; cursor: pointer; transition: transform 0.1s, box-shadow 0.1s;"
                    onmouseover="this.style.transform='scale(1.02)'; this.style.boxShadow='0 2px 8px rgba(13,110,253,0.4)'"
                    onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none'">
                    ✏️ Update Ticket
                </button>
                <button onclick="window.customerActions.createTicket({customer_id})" 
                    style="padding: 8px 14px; font-size: 12px; font-weight: 600; background: linear-gradient(135deg, #fd7e14, #ffc107); color: white; border: none; border-radius: 8px; cursor: pointer; transition: transform 0.1s, box-shadow 0.1s;"
                    onmouseover="this.style.transform='scale(1.02)'; this.style.boxShadow='0 2px 8px rgba(253,126,20,0.4)'"
                    onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none'">
                    ➕ New Ticket
                </button>
                <button onclick="window.customerActions.viewHistory({customer_id})" 
                    style="padding: 8px 14px; font-size: 12px; font-weight: 600; background: linear-gradient(135deg, #6c757d, #495057); color: white; border: none; border-radius: 8px; cursor: pointer; transition: transform 0.1s, box-shadow 0.1s;"
                    onmouseover="this.style.transform='scale(1.02)'; this.style.boxShadow='0 2px 8px rgba(108,117,125,0.4)'"
                    onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none'">
                    📋 Full History
                </button>
            </div>
        </div>'''
    
    # Customer since
    since_html = ''
    if customer_since:
        since_html = f'<div style="font-size: 13px; color: rgba(255,255,255,0.85); margin-top: 6px;">Customer since: {customer_since}</div>'
    
    # Main HTML - Professional design
    html = f'''<div id="customer-info-content" data-customer-id="{customer_id}" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.12);">
        <!-- Header -->
        <div style="background: linear-gradient(135deg, #087f5b 0%, #0ca678 100%); color: white; padding: 20px;">
            <div style="font-size: 24px; font-weight: 700; display: flex; align-items: center; gap: 12px;">
                <span style="font-size: 28px;">✅</span>
                <span style="text-shadow: 0 1px 2px rgba(0,0,0,0.1);">{name}</span>
            </div>
            {since_html}
        </div>
        
        <!-- Contact -->
        <div style="background: #ffffff; padding: 16px 20px; border-bottom: 1px solid #e9ecef;">
            <div style="display: flex; gap: 24px; flex-wrap: wrap;">
                <div style="font-size: 15px; color: #212529;">
                    <span style="color: #1971c2; margin-right: 6px;">📧</span>{email}
                </div>
                <div style="font-size: 15px; color: #212529;">
                    <span style="color: #198754; margin-right: 6px;">📱</span>{phone}
                </div>
            </div>
        </div>
        
        <!-- Stats -->
        <div style="display: flex; background: #ffffff; border-bottom: 1px solid #e9ecef;">
            <div style="flex: 1; padding: 20px; text-align: center; border-right: 1px solid #e9ecef;">
                <div style="font-size: 36px; font-weight: 800; color: {ticket_color};">{open_tickets}</div>
                <div style="font-size: 12px; color: #495057; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Open Tickets</div>
            </div>
            <div style="flex: 1; padding: 20px; text-align: center;">
                <div style="font-size: 36px; font-weight: 800; color: {invoice_color};">{overdue_invoices}</div>
                <div style="font-size: 12px; color: #495057; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Overdue</div>
            </div>
        </div>
        
        <!-- Details -->
        <div style="background: #f8f9fa; padding: 16px 20px;">
            {subscription_html}
            {tickets_html}
            {summary_html}
            {actions_html}
        </div>
    </div>'''
    return html


def format_visual_card_html(data: dict) -> str:
    """Format rich UI card for visual feedback"""
    if not data:
        return ""
        
    title = data.get('title', 'Information')
    items = data.get('items', [])
    card_type = data.get('type', 'info')
    
    # Colors based on type
    if card_type == 'alert':
        header_bg = 'linear-gradient(135deg, #e03131 0%, #c92a2a 100%)'
        icon = '⚠️'
    elif card_type == 'success':
        header_bg = 'linear-gradient(135deg, #2f9e44 0%, #2b8a3e 100%)'
        icon = '✅'
    else:
        header_bg = 'linear-gradient(135deg, #1971c2 0%, #1864ab 100%)'
        icon = 'ℹ️'
    
    items_html = ""
    for item in items:
        label = item.get('label', '')
        value = item.get('value', '')
        highlight = item.get('highlight', False)
        
        style = "font-weight: 700; color: #e03131;" if highlight else "color: #212529;"
        
        items_html += f"""
        <div style="display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #e9ecef;">
            <span style="color: #868e96; font-size: 14px;">{label}</span>
            <span style="{style} font-size: 15px; font-weight: 600;">{value}</span>
        </div>
        """
        
    html = f"""
    <div style="animation: slideIn 0.3s ease; margin-bottom: 16px; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1); font-family: sans-serif;">
        <div style="background: {header_bg}; padding: 12px 16px; color: white; font-weight: 600; font-size: 15px; display: flex; align-items: center; gap: 8px;">
            <span>{icon}</span> {title}
        </div>
        <div style="background: white; padding: 4px 16px;">
            {items_html}
        </div>
    </div>
    """
    return html

def format_metrics_html(metrics_data: dict = None) -> str:
    """Format performance metrics as styled HTML table with explanations"""
    
    # Default placeholder values
    asr_time = "-"
    llm_time = "-"
    tts_time = "-"
    total_time = "-"
    
    # Color coding based on performance thresholds
    def get_color_and_rating(value, good_threshold, medium_threshold):
        """Returns (color, rating_emoji) based on performance"""
        if value is None:
            return "#868e96", "⚪"  # Gray for N/A
        if value <= good_threshold:
            return "#37b24d", "🟢"  # Green - Excellent
        elif value <= medium_threshold:
            return "#f59f00", "🟡"  # Yellow - OK
        else:
            return "#e03131", "🔴"  # Red - Slow
    
    # Default colors
    asr_color, asr_rating = "#868e96", "⚪"
    llm_color, llm_rating = "#868e96", "⚪"
    tts_color, tts_rating = "#868e96", "⚪"
    total_color, total_rating = "#1971c2", "⚪"
    
    # Explanations
    asr_tooltip = "Time to convert your speech to text"
    llm_tooltip = "Time for AI to start generating response"
    tts_tooltip = "Time to start playing audio response"
    total_tooltip = "Total pipeline processing time"
    
    if metrics_data:
        # Handle both possible key names for ASR (flexibility)
        asr = metrics_data.get('asr_processing_time') or metrics_data.get('asr_time')
        llm = metrics_data.get('first_llm_token_time') or metrics_data.get('llm_time')
        tts = metrics_data.get('first_tts_chunk_time') or metrics_data.get('tts_time')
        total = metrics_data.get('total_processing_time') or metrics_data.get('total_time')
        
        # ASR Processing (good: <0.3s, medium: <0.8s)
        if asr is not None and isinstance(asr, (int, float)): 
            asr_time = f"{asr:.2f}s"
            asr_color, asr_rating = get_color_and_rating(asr, 0.3, 0.8)
        
        # LLM First Token (good: <0.5s, medium: <1.5s)
        # Handle case where LLM is not used (predefined responses)
        if llm is not None and isinstance(llm, (int, float)): 
            llm_time = f"{llm:.2f}s"
            llm_color, llm_rating = get_color_and_rating(llm, 0.5, 1.5)
        else:
            # LLM not used - this is OK, not an error
            llm_time = "N/A"
            llm_tooltip = "LLM not used (predefined response)"
            llm_color = "#868e96"
            llm_rating = "➖"
        
        # TTS First Chunk (good: <1.0s, medium: <2.5s)
        if tts is not None and isinstance(tts, (int, float)): 
            tts_time = f"{tts:.2f}s"
            tts_color, tts_rating = get_color_and_rating(tts, 1.0, 2.5)
            
        # Total Time (good: <2.0s, medium: <4.0s)
        if total is not None and isinstance(total, (int, float)): 
            total_time = f"{total:.2f}s"
            total_color, total_rating = get_color_and_rating(total, 2.0, 4.0)
    
    html = f"""
    <div id="metrics-content" style="background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); font-family: sans-serif; border: 1px solid #e9ecef;">
        <div style="background: linear-gradient(135deg, #4dabf7 0%, #339af0 100%); padding: 10px 16px; color: white; font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px;">
            <span>⏱️</span> Latency Report
            <span style="font-size: 11px; font-weight: normal; margin-left: auto;">🟢 &lt;Good 🟡 OK 🔴 Slow</span>
        </div>
        <div style="padding: 0 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #f1f3f5;" title="{asr_tooltip}">
                <div style="display: flex; flex-direction: column;">
                    <span style="color: #495057; font-size: 13px;">🎤 ASR Processing</span>
                    <span style="color: #adb5bd; font-size: 10px;">Speech → Text</span>
                </div>
                <span style="color: {asr_color}; font-weight: 600; font-size: 14px;">{asr_rating} {asr_time}</span>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #f1f3f5;" title="{llm_tooltip}">
                <div style="display: flex; flex-direction: column;">
                    <span style="color: #495057; font-size: 13px;">🧠 LLM First Token</span>
                    <span style="color: #adb5bd; font-size: 10px;">AI Response Start</span>
                </div>
                <span style="color: {llm_color}; font-weight: 600; font-size: 14px;">{llm_rating} {llm_time}</span>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #f1f3f5;" title="{tts_tooltip}">
                <div style="display: flex; flex-direction: column;">
                    <span style="color: #495057; font-size: 13px;">🔊 TTS First Chunk</span>
                    <span style="color: #adb5bd; font-size: 10px;">Audio Playback Start</span>
                </div>
                <span style="color: {tts_color}; font-weight: 600; font-size: 14px;">{tts_rating} {tts_time}</span>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 12px 0; background: #f8f9fa; margin: 0 -16px; padding-left: 16px; padding-right: 16px;" title="{total_tooltip}">
                <div style="display: flex; flex-direction: column;">
                    <span style="color: #212529; font-size: 13px; font-weight: 600;">⏳ Total Pipeline</span>
                    <span style="color: #adb5bd; font-size: 10px;">End-to-End Latency</span>
                </div>
                <span style="color: {total_color}; font-weight: 700; font-size: 16px;">{total_rating} {total_time}</span>
            </div>
        </div>
    </div>
    """
    return html


def get_extended_customer_info(db_conn, customer_id: int) -> dict:
    """Fetch extended customer info from database including history"""
    if not db_conn:
        return {}
    
    try:
        cursor = db_conn.cursor()
        
        # Get subscription and plan info
        cursor.execute("""
            SELECT p.name, p.price, s.status, s.start_date
            FROM subscriptions s
            JOIN plans p ON s.plan_id = p.id
            WHERE s.customer_id = %s
            ORDER BY s.start_date DESC
            LIMIT 1
        """, (customer_id,))
        sub_row = cursor.fetchone()
        
        # Get ticket statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'open') as open_count
            FROM support_tickets
            WHERE customer_id = %s
        """, (customer_id,))
        ticket_stats = cursor.fetchone()
        
        # Get recent tickets
        cursor.execute("""
            SELECT subject, status, priority, created_at
            FROM support_tickets
            WHERE customer_id = %s
            ORDER BY created_at DESC
            LIMIT 3
        """, (customer_id,))
        recent_tickets = cursor.fetchall()
        
        # Get invoice statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'paid') as paid_count,
                COUNT(*) FILTER (WHERE status = 'overdue') as overdue_count
            FROM invoices
            WHERE customer_id = %s
        """, (customer_id,))
        invoice_stats = cursor.fetchone()
        
        # Get customer since date
        cursor.execute("""
            SELECT created_at FROM customers WHERE id = %s
        """, (customer_id,))
        customer_row = cursor.fetchone()
        
        cursor.close()
        
        # Build extended info dict
        extended = {}
        
        if sub_row:
            extended['plan_name'] = sub_row[0]
            extended['plan_price'] = f"{sub_row[1]:.2f}"
            extended['subscription_status'] = sub_row[2]
        
        if ticket_stats:
            extended['total_tickets'] = ticket_stats[0] or 0
        
        if recent_tickets:
            extended['recent_tickets'] = [
                {'subject': t[0], 'status': t[1], 'priority': t[2]}
                for t in recent_tickets
            ]
        
        if invoice_stats:
            extended['total_invoices'] = invoice_stats[0] or 0
            extended['paid_invoices'] = invoice_stats[1] or 0
        
        if customer_row and customer_row[0]:
            extended['customer_since'] = customer_row[0].strftime('%b %Y')
        
        return extended
        
    except Exception as e:
        print(f"[DB] Error fetching extended info: {e}")
        return {}

# =====================================================
# Database Initialization SQL
# =====================================================
INIT_TABLES_SQL = """
-- Drop tables if exist (only these specific tables)
DROP TABLE IF EXISTS conversation_sessions CASCADE;
DROP TABLE IF EXISTS sentiment_alerts CASCADE;
DROP TABLE IF EXISTS upgrade_requests CASCADE;
DROP TABLE IF EXISTS support_tickets CASCADE;
DROP TABLE IF EXISTS invoices CASCADE;
DROP TABLE IF EXISTS subscriptions CASCADE;
DROP TABLE IF EXISTS plans CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- Customers table with PIN for authentication
CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    phone VARCHAR(20),
    pin VARCHAR(4) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Subscription plans table
CREATE TABLE plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    billing_cycle VARCHAR(20) NOT NULL DEFAULT 'monthly',
    features TEXT[],
    data_limit_gb INT,
    support_level VARCHAR(20) DEFAULT 'email'
);

-- Subscriptions table
CREATE TABLE subscriptions (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
    plan_id INT REFERENCES plans(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    start_date DATE NOT NULL,
    end_date DATE,
    auto_renew BOOLEAN DEFAULT true
);

-- Invoices table
CREATE TABLE invoices (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
    subscription_id INT REFERENCES subscriptions(id) ON DELETE CASCADE,
    amount DECIMAL(10,2) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    due_date DATE NOT NULL,
    paid_at TIMESTAMP
);

-- Support tickets table
CREATE TABLE support_tickets (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
    subject VARCHAR(200) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    priority VARCHAR(20) DEFAULT 'medium',
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);

-- Create indexes
CREATE INDEX idx_customers_pin ON customers(pin);
CREATE INDEX idx_subscriptions_customer ON subscriptions(customer_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_status ON invoices(status);
CREATE INDEX idx_tickets_customer ON support_tickets(customer_id);
CREATE INDEX idx_tickets_status ON support_tickets(status);

-- Upgrade requests table
CREATE TABLE upgrade_requests (
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
);

-- Sentiment alerts table
CREATE TABLE sentiment_alerts (
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
);

-- Conversation sessions table (for persistence)
CREATE TABLE conversation_sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    customer_id INTEGER,
    data JSONB,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_DEMO_DATA_SQL = """
-- Insert subscription plans
INSERT INTO plans (name, price, billing_cycle, features, data_limit_gb, support_level) VALUES
('Basic', 9.99, 'monthly', ARRAY['5GB Storage', 'Email Support', 'Basic Analytics'], 5, 'email'),
('Standard', 19.99, 'monthly', ARRAY['25GB Storage', 'Email & Chat Support', 'Advanced Analytics', 'API Access'], 25, 'email'),
('Premium', 49.99, 'monthly', ARRAY['100GB Storage', 'Priority Support', 'Full Analytics', 'API Access', 'Custom Integrations'], 100, 'phone'),
('Enterprise', 99.99, 'monthly', ARRAY['Unlimited Storage', '24/7 Priority Support', 'Full Analytics', 'API Access', 'Custom Integrations', 'Dedicated Account Manager'], 1000, 'priority'),
('Basic Annual', 99.99, 'yearly', ARRAY['5GB Storage', 'Email Support', 'Basic Analytics'], 5, 'email'),
('Premium Annual', 499.99, 'yearly', ARRAY['100GB Storage', 'Priority Support', 'Full Analytics', 'API Access', 'Custom Integrations'], 100, 'phone');

-- Insert customers with PIN codes (easy to say: 1234, 5678, 1111, etc.)
INSERT INTO customers (name, email, phone, pin) VALUES
('John Smith', 'john.smith@email.com', '+1-555-0101', '1234'),
('Sarah Johnson', 'sarah.j@email.com', '+1-555-0102', '5678'),
('Michael Chen', 'mchen@email.com', '+1-555-0103', '1111'),
('Emily Davis', 'emily.davis@email.com', '+1-555-0104', '2222'),
('David Wilson', 'dwilson@email.com', '+1-555-0105', '3333'),
('Lisa Anderson', 'lisa.a@email.com', '+1-555-0106', '4444'),
('Robert Taylor', 'rtaylor@email.com', '+1-555-0107', '5555'),
('Jennifer Brown', 'jbrown@email.com', '+1-555-0108', '6666'),
('James Martinez', 'jmartinez@email.com', '+1-555-0109', '7777'),
('Amanda White', 'awhite@email.com', '+1-555-0110', '8888');

-- Insert subscriptions
INSERT INTO subscriptions (customer_id, plan_id, status, start_date, end_date, auto_renew) VALUES
(1, 3, 'active', '2024-01-15', '2025-01-15', true),
(2, 2, 'active', '2024-02-01', '2025-02-01', true),
(3, 1, 'active', '2024-03-10', '2025-03-10', true),
(4, 4, 'active', '2024-01-01', '2025-01-01', true),
(5, 2, 'active', '2024-04-15', '2025-04-15', false),
(6, 3, 'suspended', '2024-02-20', '2025-02-20', true),
(7, 1, 'active', '2024-05-01', '2025-05-01', true),
(8, 2, 'cancelled', '2024-03-01', '2024-09-01', false),
(9, 4, 'active', '2024-06-15', '2025-06-15', true),
(10, 3, 'active', '2024-07-01', '2025-07-01', true);

-- Insert invoices
INSERT INTO invoices (customer_id, subscription_id, amount, status, due_date, paid_at) VALUES
(1, 1, 49.99, 'paid', '2024-02-15', '2024-02-14'),
(1, 1, 49.99, 'paid', '2024-03-15', '2024-03-15'),
(1, 1, 49.99, 'pending', '2024-04-15', NULL),
(2, 2, 19.99, 'paid', '2024-03-01', '2024-02-28'),
(2, 2, 19.99, 'overdue', '2024-04-01', NULL),
(3, 3, 9.99, 'paid', '2024-04-10', '2024-04-10'),
(4, 4, 99.99, 'paid', '2024-02-01', '2024-01-30'),
(4, 4, 99.99, 'paid', '2024-03-01', '2024-02-28'),
(5, 5, 19.99, 'pending', '2024-05-15', NULL),
(6, 6, 49.99, 'overdue', '2024-03-20', NULL);

-- Insert support tickets
INSERT INTO support_tickets (customer_id, subject, description, status, priority, created_at) VALUES
(1, 'Cannot access premium features', 'Getting error when trying to use API', 'open', 'high', '2024-03-20 10:30:00'),
(2, 'Billing question', 'Need invoice copy for tax purposes', 'resolved', 'low', '2024-03-15 14:20:00'),
(3, 'Upgrade inquiry', 'Interested in Standard plan features', 'open', 'medium', '2024-03-22 09:15:00'),
(4, 'Performance issues', 'Dashboard loading slowly', 'in_progress', 'high', '2024-03-21 16:45:00'),
(5, 'Feature request', 'Would like custom reports', 'open', 'low', '2024-03-19 11:00:00'),
(6, 'Account suspended', 'Payment failed, need to update card', 'open', 'high', '2024-03-23 08:30:00');
"""


def create_database_if_not_exists(db_host, db_port, db_name, db_user, db_password, admin_user, admin_password):
    """Create a new database if it doesn't exist"""
    try:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        
        # Connect to 'postgres' database with admin credentials
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname='postgres',
            user=admin_user,
            password=admin_password
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        # Check if database exists
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cursor.fetchone()
            
            if exists:
                conn.close()
                return f"ℹ️ Database '{db_name}' already exists. You can proceed to Initialize Tables."
            
            # Create database
            cursor.execute(f'CREATE DATABASE "{db_name}"')
            
            # Create user if not exists (may fail if user exists, which is okay)
            try:
                cursor.execute(f"CREATE USER \"{db_user}\" WITH PASSWORD '{db_password}'")
            except psycopg2.errors.DuplicateObject:
                pass
            
            # Grant privileges
            cursor.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO "{db_user}"')
        
        conn.close()
        
        # Connect to the new database to grant schema privileges
        conn2 = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=admin_user,
            password=admin_password
        )
        conn2.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        with conn2.cursor() as cursor:
            cursor.execute(f'GRANT ALL ON SCHEMA public TO "{db_user}"')
            cursor.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{db_user}"')
            cursor.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{db_user}"')
        
        conn2.close()
        
        return f"""✅ Database '{db_name}' created successfully!

📋 Details:
• Database: {db_name}
• User: {db_user}
• Host: {db_host}:{db_port}

✨ You can now click 'Initialize Tables' to create the schema and demo data."""

    except ImportError:
        return "❌ Error: psycopg2 not installed. Please install it with: pip install psycopg2-binary"
    except Exception as e:
        return f"❌ Error creating database: {str(e)}"


def drop_database(db_host, db_port, db_name, admin_user, admin_password):
    """Drop (delete) database completely"""
    try:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        
        if db_name.lower() in ['postgres', 'template0', 'template1']:
            return "❌ Cannot drop system databases!"
        
        # Connect to 'postgres' database with admin credentials
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname='postgres',
            user=admin_user,
            password=admin_password
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        with conn.cursor() as cursor:
            # Check if database exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cursor.fetchone()
            
            if not exists:
                conn.close()
                return f"ℹ️ Database '{db_name}' does not exist."
            
            # Terminate all connections to the database
            cursor.execute(f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '{db_name}'
                AND pid <> pg_backend_pid()
            """)
            
            # Drop database
            cursor.execute(f'DROP DATABASE "{db_name}"')
        
        conn.close()
        
        return f"""✅ Database '{db_name}' deleted successfully!

⚠️ All data has been permanently removed."""

    except ImportError:
        return "❌ Error: psycopg2 not installed"
    except Exception as e:
        return f"❌ Error dropping database: {str(e)}"


def init_tables_and_data(db_host, db_port, db_name, db_user, db_password):
    """Initialize tables and demo data in the database"""
    try:
        import psycopg2
        
        # Connect to the specific database
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        # Create tables
        with conn.cursor() as cursor:
            cursor.execute(INIT_TABLES_SQL)
        
        # Insert demo data
        with conn.cursor() as cursor:
            cursor.execute(INSERT_DEMO_DATA_SQL)
        
        # Get counts for verification
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM customers")
            customers = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM plans")
            plans = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM subscriptions")
            subscriptions = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM invoices")
            invoices = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM support_tickets")
            tickets = cursor.fetchone()[0]
        
        conn.close()
        
        return f"""✅ Tables initialized successfully!

📊 Created tables and inserted demo data:
• Customers: {customers}
• Plans: {plans}
• Subscriptions: {subscriptions}
• Invoices: {invoices}
• Support Tickets: {tickets}

🎉 Ready to use! Try asking:
• "My email is john.smith@email.com"
• "What's my subscription?"
• "Do I have unpaid invoices?"
"""
    except ImportError:
        return "❌ Error: psycopg2 not installed. Please install it with: pip install psycopg2-binary"
    except Exception as e:
        return f"❌ Error initializing tables: {str(e)}"


def test_db_connection(db_host, db_port, db_name, db_user, db_password):
    """Test database connection"""
    try:
        import psycopg2
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password,
            connect_timeout=5
        )
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT version()")
            version = cursor.fetchone()[0]
        
        conn.close()
        
        return f"✅ Connection successful!\n\n📌 Server: {version[:60]}..."
    except ImportError:
        return "❌ Error: psycopg2 not installed"
    except Exception as e:
        return f"❌ Connection failed: {str(e)}"


def get_db_stats(db_host, db_port, db_name, db_user, db_password):
    """Get database statistics"""
    try:
        import psycopg2
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        stats = []
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            if not tables:
                conn.close()
                return "⚠️ No tables found. Click 'Initialize Tables' to create tables and demo data."
            
            for table in ['customers', 'plans', 'subscriptions', 'invoices', 'support_tickets']:
                if table in tables:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    stats.append(f"• {table}: {count} rows")
                else:
                    stats.append(f"• {table}: ❌ not found")
        
        conn.close()
        
        return "📊 Database Statistics:\n\n" + "\n".join(stats)
    except ImportError:
        return "❌ Error: psycopg2 not installed"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def list_databases(db_host, db_port, admin_user, admin_password):
    """List all databases on the server"""
    try:
        import psycopg2
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname='postgres',
            user=admin_user,
            password=admin_password,
            connect_timeout=5
        )
        
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT datname, pg_size_pretty(pg_database_size(datname)) as size
                FROM pg_database 
                WHERE datistemplate = false
                ORDER BY datname
            """)
            databases = cursor.fetchall()
        
        conn.close()
        
        result = "📚 Databases on server:\n\n"
        for db_name, size in databases:
            icon = "🔒" if db_name == "postgres" else "📁"
            result += f"{icon} {db_name} ({size})\n"
        
        return result
    except ImportError:
        return "❌ Error: psycopg2 not installed"
    except Exception as e:
        return f"❌ Error: {str(e)}"


# =====================================================
# Data Viewer/Editor Functions
# =====================================================

def load_table_data(db_host, db_port, db_name, db_user, db_password, table_name):
    """Load data from a specific table"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        # Get data
        query = f"SELECT * FROM {table_name} ORDER BY id LIMIT 100"
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        return df
    except ImportError:
        return None
    except Exception as e:
        print(f"Error loading table: {e}")
        return None


def get_customers_data(db_host, db_port, db_name, db_user, db_password):
    """Get customers table data"""
    df = load_table_data(db_host, db_port, db_name, db_user, db_password, "customers")
    if df is not None:
        return df
    return None


def get_plans_data(db_host, db_port, db_name, db_user, db_password):
    """Get plans table data"""
    df = load_table_data(db_host, db_port, db_name, db_user, db_password, "plans")
    if df is not None:
        return df
    return None


def get_subscriptions_data(db_host, db_port, db_name, db_user, db_password):
    """Get subscriptions with customer and plan names"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        query = """
            SELECT s.id, c.name as customer_name, p.name as plan_name, 
                   s.status, s.start_date, s.end_date, s.auto_renew
            FROM subscriptions s
            JOIN customers c ON s.customer_id = c.id
            JOIN plans p ON s.plan_id = p.id
            ORDER BY s.id
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"Error: {e}")
        return None


def get_invoices_data(db_host, db_port, db_name, db_user, db_password):
    """Get invoices with customer names"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        query = """
            SELECT i.id, c.name as customer_name, i.amount, i.status, 
                   i.due_date, i.paid_at
            FROM invoices i
            JOIN customers c ON i.customer_id = c.id
            ORDER BY i.id
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"Error: {e}")
        return None


def get_tickets_data(db_host, db_port, db_name, db_user, db_password):
    """Get support tickets with customer names"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        query = """
            SELECT t.id, c.name as customer_name, t.subject, t.status, 
                   t.priority, t.created_at, t.resolved_at
            FROM support_tickets t
            JOIN customers c ON t.customer_id = c.id
            ORDER BY t.created_at DESC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"Error: {e}")
        return None


def get_upgrade_requests_data(db_host, db_port, db_name, db_user, db_password):
    """Get upgrade requests"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        # First check if table exists
        cursor = conn.cursor()
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'upgrade_requests'
            )
        """)
        table_exists = cursor.fetchone()[0]
        
        if not table_exists:
            conn.close()
            return pd.DataFrame(columns=['id', 'customer_name', 'email', 'current_plan', 'target_plan', 'status', 'created_at'])
        
        query = """
            SELECT id, customer_name, customer_email as email, 
                   current_plan_name as current_plan, target_plan_name as target_plan,
                   status, priority, created_at, processed_at, notes
            FROM upgrade_requests
            ORDER BY created_at DESC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if df.empty:
            return pd.DataFrame(columns=['id', 'customer_name', 'email', 'current_plan', 'target_plan', 'status', 'created_at'])
        
        return df
    except Exception as e:
        print(f"Error getting upgrade requests: {e}")
        return None


def get_sentiment_alerts_data(db_host, db_port, db_name, db_user, db_password):
    """Get sentiment alerts for unhappy customers"""
    try:
        import psycopg2
        import pandas as pd
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        # First check if sentiment_alerts table exists
        cursor = conn.cursor()
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'sentiment_alerts'
            )
        """)
        table_exists = cursor.fetchone()[0]
        
        if not table_exists:
            conn.close()
            return pd.DataFrame(columns=['id', 'customer_name', 'email', 'sentiment', 'churn_risk', 'trigger_phrases', 'message', 'action', 'created_at'])
        
        # Query the sentiment_alerts table
        query = """
            SELECT 
                id,
                customer_name,
                customer_email as email,
                sentiment_label as sentiment,
                sentiment_score as score,
                churn_risk,
                trigger_phrases,
                customer_message as message,
                recommended_action as action,
                created_at,
                resolved_at
            FROM sentiment_alerts
            WHERE resolved_at IS NULL
            ORDER BY 
                CASE churn_risk 
                    WHEN 'high' THEN 1 
                    WHEN 'medium' THEN 2 
                    ELSE 3 
                END,
                created_at DESC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if df.empty:
            return pd.DataFrame(columns=['id', 'customer_name', 'email', 'sentiment', 'churn_risk', 'trigger_phrases', 'message', 'action', 'created_at'])
        
        return df
    except Exception as e:
        print(f"Error getting sentiment alerts: {e}")
        return None


def add_customer(db_host, db_port, db_name, db_user, db_password, name, email, phone, pin_code=None):
    """Add a new customer with optional PIN code"""
    try:
        import psycopg2
        
        if not name or not email:
            return "❌ Name and email are required"
        
        # Validate PIN if provided
        if pin_code:
            pin_code = ''.join(c for c in str(pin_code) if c.isdigit())[:4]
            if len(pin_code) < 4:
                return "❌ PIN must be 4 digits"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            if pin_code:
                cursor.execute(
                    "INSERT INTO customers (name, email, phone, pin) VALUES (%s, %s, %s, %s) RETURNING id",
                    (name, email, phone, pin_code)
                )
            else:
                cursor.execute(
                    "INSERT INTO customers (name, email, phone, pin) VALUES (%s, %s, %s, %s) RETURNING id",
                    (name, email, phone, '0000')  # Default PIN
                )
            new_id = cursor.fetchone()[0]
        
        conn.close()
        return f"✅ Customer added successfully! ID: {new_id}"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def update_customer(db_host, db_port, db_name, db_user, db_password, customer_id, name, email, phone):
    """Update an existing customer"""
    try:
        import psycopg2
        
        if not customer_id:
            return "❌ Customer ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE customers SET name = %s, email = %s, phone = %s WHERE id = %s",
                (name, email, phone, int(customer_id))
            )
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Customer ID {customer_id} not found"
        
        conn.close()
        return f"✅ Customer {customer_id} updated successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def delete_customer(db_host, db_port, db_name, db_user, db_password, customer_id):
    """Delete a customer"""
    try:
        import psycopg2
        
        if not customer_id:
            return "❌ Customer ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM customers WHERE id = %s", (int(customer_id),))
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Customer ID {customer_id} not found"
        
        conn.close()
        return f"✅ Customer {customer_id} deleted successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def update_invoice_status(db_host, db_port, db_name, db_user, db_password, invoice_id, new_status):
    """Update invoice status"""
    try:
        import psycopg2
        
        if not invoice_id or not new_status:
            return "❌ Invoice ID and status are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            if new_status == 'paid':
                cursor.execute(
                    "UPDATE invoices SET status = %s, paid_at = NOW() WHERE id = %s",
                    (new_status, int(invoice_id))
                )
            else:
                cursor.execute(
                    "UPDATE invoices SET status = %s, paid_at = NULL WHERE id = %s",
                    (new_status, int(invoice_id))
                )
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Invoice ID {invoice_id} not found"
        
        conn.close()
        return f"✅ Invoice {invoice_id} status updated to '{new_status}'!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def update_ticket_status(db_host, db_port, db_name, db_user, db_password, ticket_id, new_status):
    """Update support ticket status"""
    try:
        import psycopg2
        
        if not ticket_id or not new_status:
            return "❌ Ticket ID and status are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            if new_status == 'resolved':
                cursor.execute(
                    "UPDATE support_tickets SET status = %s, resolved_at = NOW() WHERE id = %s",
                    (new_status, int(ticket_id))
                )
            else:
                cursor.execute(
                    "UPDATE support_tickets SET status = %s, resolved_at = NULL WHERE id = %s",
                    (new_status, int(ticket_id))
                )
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Ticket ID {ticket_id} not found"
        
        conn.close()
        return f"✅ Ticket {ticket_id} status updated to '{new_status}'!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def delete_invoice(db_host, db_port, db_name, db_user, db_password, invoice_id):
    """Delete an invoice"""
    try:
        import psycopg2
        
        if not invoice_id:
            return "❌ Invoice ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM invoices WHERE id = %s", (int(invoice_id),))
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Invoice ID {invoice_id} not found"
        
        conn.close()
        return f"✅ Invoice {invoice_id} deleted successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def delete_ticket(db_host, db_port, db_name, db_user, db_password, ticket_id):
    """Delete a support ticket"""
    try:
        import psycopg2
        
        if not ticket_id:
            return "❌ Ticket ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM support_tickets WHERE id = %s", (int(ticket_id),))
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Ticket ID {ticket_id} not found"
        
        conn.close()
        return f"✅ Ticket {ticket_id} deleted successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


# =============================================================================
# PLAN CRUD OPERATIONS
# =============================================================================
def add_plan(db_host, db_port, db_name, db_user, db_password, name, price, billing_cycle, data_limit_gb, support_level):
    """Add a new plan"""
    try:
        import psycopg2
        
        if not name or not price:
            return "❌ Plan name and price are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO plans (name, price, billing_cycle, data_limit_gb, support_level)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (name, float(price), billing_cycle or 'monthly', int(data_limit_gb or 0), support_level or 'standard')
            )
            new_id = cursor.fetchone()[0]
        
        conn.close()
        return f"✅ Plan '{name}' created with ID {new_id}!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def update_plan(db_host, db_port, db_name, db_user, db_password, plan_id, name, price, billing_cycle, data_limit_gb, support_level):
    """Update an existing plan"""
    try:
        import psycopg2
        
        if not plan_id:
            return "❌ Plan ID is required for update"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            # Build dynamic update query
            updates = []
            values = []
            if name:
                updates.append("name = %s")
                values.append(name)
            if price:
                updates.append("price = %s")
                values.append(float(price))
            if billing_cycle:
                updates.append("billing_cycle = %s")
                values.append(billing_cycle)
            if data_limit_gb:
                updates.append("data_limit_gb = %s")
                values.append(int(data_limit_gb))
            if support_level:
                updates.append("support_level = %s")
                values.append(support_level)
            
            if not updates:
                return "❌ No fields to update"
            
            values.append(int(plan_id))
            cursor.execute(f"UPDATE plans SET {', '.join(updates)} WHERE id = %s", values)
            
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Plan ID {plan_id} not found"
        
        conn.close()
        return f"✅ Plan {plan_id} updated successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def delete_plan(db_host, db_port, db_name, db_user, db_password, plan_id):
    """Delete a plan"""
    try:
        import psycopg2
        
        if not plan_id:
            return "❌ Plan ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            # Check if plan has active subscriptions
            cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE plan_id = %s", (int(plan_id),))
            sub_count = cursor.fetchone()[0]
            if sub_count > 0:
                conn.close()
                return f"❌ Cannot delete: Plan has {sub_count} active subscription(s)"
            
            cursor.execute("DELETE FROM plans WHERE id = %s", (int(plan_id),))
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Plan ID {plan_id} not found"
        
        conn.close()
        return f"✅ Plan {plan_id} deleted successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


# =============================================================================
# SUBSCRIPTION CRUD OPERATIONS
# =============================================================================
def add_subscription(db_host, db_port, db_name, db_user, db_password, customer_id, plan_id, status, auto_renew):
    """Add a new subscription"""
    try:
        import psycopg2
        from datetime import datetime, timedelta
        
        if not customer_id or not plan_id:
            return "❌ Customer ID and Plan ID are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        start_date = datetime.now()
        end_date = start_date + timedelta(days=30)  # Default 30 days
        
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO subscriptions (customer_id, plan_id, status, start_date, end_date, auto_renew)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (int(customer_id), int(plan_id), status or 'active', start_date, end_date, auto_renew == 'Yes')
            )
            new_id = cursor.fetchone()[0]
        
        conn.close()
        return f"✅ Subscription created with ID {new_id}!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def update_subscription(db_host, db_port, db_name, db_user, db_password, sub_id, status, auto_renew):
    """Update an existing subscription"""
    try:
        import psycopg2
        
        if not sub_id:
            return "❌ Subscription ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            updates = []
            values = []
            
            if status:
                updates.append("status = %s")
                values.append(status)
            if auto_renew:
                updates.append("auto_renew = %s")
                values.append(auto_renew == 'Yes')
            
            if not updates:
                return "❌ No fields to update"
            
            values.append(int(sub_id))
            cursor.execute(f"UPDATE subscriptions SET {', '.join(updates)} WHERE id = %s", values)
            
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Subscription ID {sub_id} not found"
        
        conn.close()
        return f"✅ Subscription {sub_id} updated successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def cancel_subscription(db_host, db_port, db_name, db_user, db_password, sub_id):
    """Cancel a subscription"""
    try:
        import psycopg2
        from datetime import datetime
        
        if not sub_id:
            return "❌ Subscription ID is required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE subscriptions SET status = 'cancelled', end_date = %s WHERE id = %s",
                (datetime.now(), int(sub_id))
            )
            if cursor.rowcount == 0:
                conn.close()
                return f"❌ Subscription ID {sub_id} not found"
        
        conn.close()
        return f"✅ Subscription {sub_id} cancelled!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


# =============================================================================
# TICKET CRUD OPERATIONS
# =============================================================================
def add_ticket(db_host, db_port, db_name, db_user, db_password, customer_id, subject, description, priority):
    """Create a new support ticket"""
    try:
        import psycopg2
        
        if not customer_id or not subject:
            return "❌ Customer ID and subject are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO support_tickets (customer_id, subject, description, status, priority, created_at)
                   VALUES (%s, %s, %s, 'open', %s, NOW()) RETURNING id""",
                (int(customer_id), subject, description or '', priority or 'medium')
            )
            new_id = cursor.fetchone()[0]
        
        conn.close()
        return f"✅ Ticket #{new_id} created!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def add_invoice(db_host, db_port, db_name, db_user, db_password, customer_id, amount, due_date, status):
    """Create a new invoice"""
    try:
        import psycopg2
        from datetime import datetime, timedelta
        
        if not customer_id or not amount:
            return "❌ Customer ID and amount are required"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        conn.autocommit = True
        
        # Default due date is 30 days from now
        if not due_date:
            due_date = datetime.now() + timedelta(days=30)
        
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO invoices (customer_id, amount, status, due_date, created_at)
                   VALUES (%s, %s, %s, %s, NOW()) RETURNING id""",
                (int(customer_id), float(amount), status or 'pending', due_date)
            )
            new_id = cursor.fetchone()[0]
        
        conn.close()
        return f"✅ Invoice #{new_id} created!"
    except Exception as e:
        return f"❌ Error: {str(e)}"


def run_custom_query(db_host, db_port, db_name, db_user, db_password, query):
    """Run a custom SQL query (SELECT only for safety)"""
    try:
        import psycopg2
        import pandas as pd
        
        if not query:
            return None, "❌ Query is empty"
        
        # Safety check - only allow SELECT queries
        query_upper = query.strip().upper()
        if not query_upper.startswith("SELECT"):
            return None, "❌ Only SELECT queries are allowed for safety"
        
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        return df, f"✅ Query returned {len(df)} rows"
    except Exception as e:
        return None, f"❌ Error: {str(e)}"


AGENT_PERSONAS = {
    "🤖 Smart Assistant": {
        "description": "Natural, intelligent conversational AI - like talking to a knowledgeable friend",
        "template": '''You're having a natural voice conversation. Be warm, smart, and genuinely helpful.

User said: "{transcript}"

VOICE CONVERSATION RULES:
• Keep it SHORT - 1-3 sentences max. This is spoken, not written
• Use contractions and natural speech patterns
• Sound like a real person, not a script
• If unclear, ask naturally: "Sorry, what do you mean?"
• IMPORTANT: User may speak a different language - understand them but respond in YOUR language

Reply naturally:'''
    },
    "📞 Customer Service Pro": {
        "description": "Warm, efficient customer service agent - professional yet personable",
        "template": '''You're a friendly customer service agent on a live phone call. You genuinely want to help.

Customer said: "{transcript}"

SERVICE STYLE:
• Warm and professional - helpful colleague, not corporate robot
• Get to the point quickly - customers appreciate efficiency
• Use customer's name occasionally if known
• Sound real and human, not scripted
• 1-3 sentences max
• Customer may speak their native language - understand them, respond in your language

What would a great service rep say?'''
    },
    "💼 Business Advisor": {
        "description": "Strategic, insightful business consultant - confident and clear",
        "template": '''You're a sharp business advisor having a voice conversation. Confident, direct, insightful.

They asked: "{transcript}"

ADVISOR STYLE:
• Be direct - every word counts
• Give actionable insights, not vague advice
• Sound confident but not arrogant
• 2-3 sentences max

What's the smart take?'''
    },
    "🎓 Expert Teacher": {
        "description": "Patient, engaging educator - makes complex things simple",
        "template": '''You're a great teacher having a voice conversation with a curious learner.

They asked: "{transcript}"

TEACHING STYLE:
• Make it simple - explain like they're 12
• Use analogies and examples
• Sound enthusiastic, not boring
• 2-3 sentences, no lectures
• If it needs more, say "want me to explain more?"

Explain it:'''
    },
    "🎯 Custom": {
        "description": "Define your own persona with a custom prompt template",
        "template": '{transcript}'
    }
}

# TTS Languages with display names, response instructions, and technical term translations
# All 17 XTTS v2 supported languages with comprehensive instructions
TTS_LANGUAGES = {
    "en": {
        "display_name": "🇺🇸 English",
        "response_instruction": """

LANGUAGE RULE: You MUST respond ONLY in English. Even if the user speaks Hebrew, Arabic, or another language - understand them but ALWAYS reply in natural, conversational English.

IMPORTANT: All responses must be in English including:
- Greetings and acknowledgments
- Technical terms (subscription, plan, ticket, invoice, balance, etc.)
- Numbers and currency (use $ for dollars)
- Status updates and confirmations""",
    },
    "he": {
        "display_name": "🇮🇱 Hebrew (עברית) ⚠️ Experimental",
        "response_instruction": """

⚠️ EXPERIMENTAL: Hebrew is NOT officially supported by XTTS v2.
Audio quality may vary. Consider using English for production.

כללי שפה: אתה חייב לענות רק בעברית. גם אם המשתמש מדבר אנגלית או שפה אחרת - הבן אותם אבל תמיד ענה בעברית טבעית ושוטפת.

חשוב - תרגם את כל המונחים הטכניים לעברית:
- subscription = מנוי
- plan = תוכנית / חבילה
- ticket = כרטיס / קריאה
- invoice = חשבונית
- balance = יתרה
- payment = תשלום
- pending = ממתין
- active = פעיל
- cancelled = מבוטל
- upgrade = שדרוג
- premium = פרימיום
- standard = סטנדרט
- support = תמיכה
השתמש בשקלים (₪) לסכומים כספיים.""",
    },
    "es": {
        "display_name": "🇪🇸 Spanish",
        "response_instruction": """

REGLA DE IDIOMA: DEBES responder SOLAMENTE en español. Aunque el usuario hable otro idioma, entiéndelo pero SIEMPRE responde en español natural y conversacional.

IMPORTANTE - Traduce todos los términos técnicos al español:
- subscription = suscripción
- plan = plan
- ticket = ticket / incidencia
- invoice = factura
- balance = saldo
- payment = pago
- pending = pendiente
- active = activo
- cancelled = cancelado
- upgrade = mejora / actualización
- premium = premium
- standard = estándar
- support = soporte
Usa euros (€) o la moneda apropiada para cantidades.""",
    },
    "fr": {
        "display_name": "🇫🇷 French",
        "response_instruction": """

RÈGLE DE LANGUE: Tu DOIS répondre UNIQUEMENT en français. Même si l'utilisateur parle une autre langue, comprends-le mais réponds TOUJOURS en français naturel et conversationnel.

IMPORTANT - Traduis tous les termes techniques en français:
- subscription = abonnement
- plan = forfait
- ticket = ticket / demande
- invoice = facture
- balance = solde
- payment = paiement
- pending = en attente
- active = actif
- cancelled = annulé
- upgrade = mise à niveau
- premium = premium
- standard = standard
- support = support / assistance
Utilise les euros (€) pour les montants.""",
    },
    "de": {
        "display_name": "🇩🇪 German",
        "response_instruction": """

SPRACHREGEL: Du MUSST ausschließlich auf Deutsch antworten. Auch wenn der Benutzer eine andere Sprache spricht, verstehe ihn aber antworte IMMER in natürlichem, umgangssprachlichem Deutsch.

WICHTIG - Übersetze alle technischen Begriffe ins Deutsche:
- subscription = Abonnement
- plan = Tarif / Plan
- ticket = Ticket / Anfrage
- invoice = Rechnung
- balance = Kontostand / Saldo
- payment = Zahlung
- pending = ausstehend
- active = aktiv
- cancelled = gekündigt
- upgrade = Upgrade
- premium = Premium
- standard = Standard
- support = Support / Kundenservice
Verwende Euro (€) für Beträge.""",
    },
    "it": {
        "display_name": "🇮🇹 Italian",
        "response_instruction": """

REGOLA LINGUISTICA: DEVI rispondere SOLO in italiano. Anche se l'utente parla un'altra lingua, capiscilo ma rispondi SEMPRE in italiano naturale e colloquiale.

IMPORTANTE - Traduci tutti i termini tecnici in italiano:
- subscription = abbonamento
- plan = piano
- ticket = ticket / richiesta
- invoice = fattura
- balance = saldo
- payment = pagamento
- pending = in attesa
- active = attivo
- cancelled = annullato
- upgrade = upgrade / aggiornamento
- premium = premium
- standard = standard
- support = supporto / assistenza
Usa euro (€) per gli importi.""",
    },
    "pt": {
        "display_name": "🇵🇹 Portuguese",
        "response_instruction": """

REGRA DE IDIOMA: Você DEVE responder APENAS em português. Mesmo que o usuário fale outro idioma, entenda-o mas responda SEMPRE em português natural e conversacional.

IMPORTANTE - Traduza todos os termos técnicos para português:
- subscription = assinatura
- plan = plano
- ticket = chamado / ticket
- invoice = fatura
- balance = saldo
- payment = pagamento
- pending = pendente
- active = ativo
- cancelled = cancelado
- upgrade = upgrade / melhoria
- premium = premium
- standard = padrão
- support = suporte
Use reais (R$) ou euros (€) conforme apropriado.""",
    },
    "pl": {
        "display_name": "🇵🇱 Polish",
        "response_instruction": """

ZASADA JĘZYKOWA: MUSISZ odpowiadać TYLKO po polsku. Nawet jeśli użytkownik mówi w innym języku, zrozum go ale ZAWSZE odpowiadaj po polsku w naturalny, konwersacyjny sposób.

WAŻNE - Przetłumacz wszystkie terminy techniczne na polski:
- subscription = subskrypcja / abonament
- plan = plan
- ticket = zgłoszenie
- invoice = faktura
- balance = saldo
- payment = płatność
- pending = oczekujący
- active = aktywny
- cancelled = anulowany
- upgrade = ulepszenie
- premium = premium
- standard = standardowy
- support = wsparcie
Używaj złotych (zł) dla kwot.""",
    },
    "tr": {
        "display_name": "🇹🇷 Turkish",
        "response_instruction": """

DİL KURALI: SADECE Türkçe yanıt vermelisin. Kullanıcı başka bir dilde konuşsa bile, onu anla ama HER ZAMAN doğal, konuşma dilinde Türkçe yanıt ver.

ÖNEMLİ - Tüm teknik terimleri Türkçe'ye çevir:
- subscription = abonelik
- plan = plan
- ticket = destek talebi
- invoice = fatura
- balance = bakiye
- payment = ödeme
- pending = beklemede
- active = aktif
- cancelled = iptal edildi
- upgrade = yükseltme
- premium = premium
- standard = standart
- support = destek
Tutarlar için Türk Lirası (₺) kullan.""",
    },
    "ru": {
        "display_name": "🇷🇺 Russian",
        "response_instruction": """

ЯЗЫКОВОЕ ПРАВИЛО: Ты ДОЛЖЕН отвечать ТОЛЬКО на русском. Даже если пользователь говорит на другом языке, пойми его но ВСЕГДА отвечай на естественном, разговорном русском.

ВАЖНО - Переводи все технические термины на русский:
- subscription = подписка
- plan = тариф / план
- ticket = заявка / обращение
- invoice = счёт
- balance = баланс
- payment = оплата / платёж
- pending = ожидает
- active = активный
- cancelled = отменён
- upgrade = повышение тарифа
- premium = премиум
- standard = стандарт
- support = поддержка
Используй рубли (₽) для сумм.""",
    },
    "nl": {
        "display_name": "🇳🇱 Dutch",
        "response_instruction": """

TAALREGEL: Je MOET ALLEEN in het Nederlands antwoorden. Ook als de gebruiker een andere taal spreekt, begrijp hem maar antwoord ALTIJD in natuurlijk, conversationeel Nederlands.

BELANGRIJK - Vertaal alle technische termen naar het Nederlands:
- subscription = abonnement
- plan = pakket
- ticket = ticket / melding
- invoice = factuur
- balance = saldo
- payment = betaling
- pending = in behandeling
- active = actief
- cancelled = geannuleerd
- upgrade = upgrade
- premium = premium
- standard = standaard
- support = klantenservice
Gebruik euro's (€) voor bedragen.""",
    },
    "cs": {
        "display_name": "🇨🇿 Czech",
        "response_instruction": """

JAZYKOVÉ PRAVIDLO: MUSÍŠ odpovídat POUZE česky. I když uživatel mluví jiným jazykem, porozuměj mu ale VŽDY odpovídej v přirozeném, hovorovém českém jazyce.

DŮLEŽITÉ - Přelož všechny technické termíny do češtiny:
- subscription = předplatné
- plan = tarif
- ticket = požadavek / tiket
- invoice = faktura
- balance = zůstatek
- payment = platba
- pending = čekající
- active = aktivní
- cancelled = zrušeno
- upgrade = upgrade
- premium = premium
- standard = standard
- support = podpora
Používej koruny (Kč) pro částky.""",
    },
    "ar": {
        "display_name": "🇸🇦 Arabic",
        "response_instruction": """

قاعدة اللغة: يجب أن تجيب باللغة العربية فقط. حتى لو تحدث المستخدم بلغة أخرى، افهمه لكن أجب دائماً بالعربية الطبيعية والمحادثة.

مهم - ترجم جميع المصطلحات التقنية إلى العربية:
- subscription = اشتراك
- plan = خطة / باقة
- ticket = تذكرة / طلب دعم
- invoice = فاتورة
- balance = رصيد
- payment = دفعة
- pending = قيد الانتظار
- active = نشط
- cancelled = ملغى
- upgrade = ترقية
- premium = بريميوم
- standard = أساسي
- support = دعم
استخدم العملة المناسبة للمبالغ.""",
    },
    "zh-cn": {
        "display_name": "🇨🇳 Chinese",
        "response_instruction": """

语言规则：你必须只用中文回答。即使用户说其他语言，也要理解他们但始终用自然的对话式中文回答。

重要 - 将所有技术术语翻译成中文：
- subscription = 订阅
- plan = 套餐
- ticket = 工单
- invoice = 发票
- balance = 余额
- payment = 付款
- pending = 待处理
- active = 活跃
- cancelled = 已取消
- upgrade = 升级
- premium = 高级版
- standard = 标准版
- support = 客服
金额使用人民币（¥）。""",
    },
    "ja": {
        "display_name": "🇯🇵 Japanese",
        "response_instruction": """

言語ルール：日本語のみで回答してください。ユーザーが他の言語を話しても、理解した上で必ず自然な会話調の日本語で回答してください。

重要 - すべての技術用語を日本語に翻訳してください：
- subscription = サブスクリプション / 定期購読
- plan = プラン
- ticket = チケット / お問い合わせ
- invoice = 請求書
- balance = 残高
- payment = お支払い
- pending = 保留中
- active = アクティブ
- cancelled = キャンセル済み
- upgrade = アップグレード
- premium = プレミアム
- standard = スタンダード
- support = サポート
金額には円（¥）を使用してください。""",
    },
    "hu": {
        "display_name": "🇭🇺 Hungarian",
        "response_instruction": """

NYELVI SZABÁLY: CSAK magyarul válaszolj. Még ha a felhasználó más nyelven beszél is, értsd meg de MINDIG természetes, társalgási magyar nyelven válaszolj.

FONTOS - Fordítsd le az összes technikai kifejezést magyarra:
- subscription = előfizetés
- plan = csomag
- ticket = jegy / hibajegy
- invoice = számla
- balance = egyenleg
- payment = fizetés
- pending = függőben
- active = aktív
- cancelled = lemondva
- upgrade = frissítés
- premium = prémium
- standard = alap
- support = ügyfélszolgálat
Használj forintot (Ft) az összegekhez.""",
    },
    "ko": {
        "display_name": "🇰🇷 Korean",
        "response_instruction": """

언어 규칙: 한국어로만 답변해야 합니다. 사용자가 다른 언어로 말해도 이해하되 항상 자연스러운 대화체 한국어로 답변하세요.

중요 - 모든 기술 용어를 한국어로 번역하세요:
- subscription = 구독
- plan = 요금제
- ticket = 문의 / 티켓
- invoice = 청구서
- balance = 잔액
- payment = 결제
- pending = 대기 중
- active = 활성
- cancelled = 취소됨
- upgrade = 업그레이드
- premium = 프리미엄
- standard = 스탠다드
- support = 고객 지원
금액에는 원(₩)을 사용하세요.""",
    },
    "hi": {
        "display_name": "🇮🇳 Hindi",
        "response_instruction": """

भाषा नियम: आपको केवल हिंदी में जवाब देना होगा। भले ही उपयोगकर्ता अन्य भाषा में बोले, उन्हें समझें लेकिन हमेशा स्वाभाविक, बातचीत वाली हिंदी में जवाब दें।

महत्वपूर्ण - सभी तकनीकी शब्दों का हिंदी में अनुवाद करें:
- subscription = सदस्यता
- plan = प्लान / योजना
- ticket = टिकट / शिकायत
- invoice = बिल / चालान
- balance = शेष राशि
- payment = भुगतान
- pending = लंबित
- active = सक्रिय
- cancelled = रद्द
- upgrade = अपग्रेड
- premium = प्रीमियम
- standard = स्टैंडर्ड
- support = सहायता
राशियों के लिए रुपये (₹) का उपयोग करें।""",
    },
}

# ASR Languages supported by Whisper (for input speech recognition)
ASR_LANGUAGES = {
    "auto": "🌐 Auto-detect",
    "en": "🇺🇸 English",
    "he": "🇮🇱 Hebrew (עברית)",
    "es": "🇪🇸 Spanish",
    "fr": "🇫🇷 French",
    "de": "🇩🇪 German",
    "it": "🇮🇹 Italian",
    "pt": "🇵🇹 Portuguese",
    "pl": "🇵🇱 Polish",
    "tr": "🇹🇷 Turkish",
    "ru": "🇷🇺 Russian",
    "nl": "🇳🇱 Dutch",
    "cs": "🇨🇿 Czech",
    "ar": "🇸🇦 Arabic",
    "zh": "🇨🇳 Chinese",
    "ja": "🇯🇵 Japanese",
    "hu": "🇭🇺 Hungarian",
    "ko": "🇰🇷 Korean",
    "hi": "🇮🇳 Hindi",
    "vi": "🇻🇳 Vietnamese",
    "th": "🇹🇭 Thai",
    "uk": "🇺🇦 Ukrainian",
    "el": "🇬🇷 Greek",
    "ro": "🇷🇴 Romanian",
    "sv": "🇸🇪 Swedish",
    "da": "🇩🇰 Danish",
    "fi": "🇫🇮 Finnish",
    "no": "🇳🇴 Norwegian",
    "bg": "🇧🇬 Bulgarian",
    "ca": "🏴 Catalonian",
    "hr": "🇭🇷 Croatian",
    "et": "🇪🇪 Estonian",
    "id": "🇮🇩 Indonesian",
    "lv": "🇱🇻 Latvian",
    "lt": "🇱🇹 Lithuanian",
    "ms": "🇲🇾 Malay",
    "sk": "🇸🇰 Slovak",
    "sl": "🇸🇮 Slovenian",
    "tl": "🇵🇭 Tagalog",
    "ta": "🇮🇳 Tamil",
    "te": "🇮🇳 Telugu",
    "ur": "🇵🇰 Urdu",
    "cy": "🏴󠁧󠁢󠁷󠁬󠁳󠁿 Welsh",
    "sw": "🇰🇪 Swahili",
    "af": "🇿🇦 Afrikaans",
    "is": "🇮🇸 Icelandic",
    "km": "🇰🇭 Khmer",
    "lo": "🇱🇦 Lao",
    "mk": "🇲🇰 Macedonian",
    "ml": "🇮🇳 Malayalam",
    "mr": "🇮🇳 Marathi",
    "my": "🇲🇲 Burmese",
    "ne": "🇳🇵 Nepali",
    "sr": "🇷🇸 Serbian",
    "si": "🇱🇰 Sinhala",
    "hy": "🇦🇲 Armenian",
    "az": "🇦🇿 Azerbaijani",
    "be": "🇧🇾 Belarusian",
    "bs": "🇧🇦 Bosnian",
    "gl": "🇪🇸 Galician",
    "ka": "🇬🇪 Georgian",
    "gu": "🇮🇳 Gujarati",
    "kn": "🇮🇳 Kannada",
    "kk": "🇰🇿 Kazakh",
    "ky": "🇰🇬 Kyrgyz",
    "mn": "🇲🇳 Mongolian",
    "fa": "🇮🇷 Persian",
    "sd": "🇵🇰 Sindhi",
    "tt": "🇷🇺 Tatar",
    "uz": "🇺🇿 Uzbek",
    "am": "🇪🇹 Amharic",
    "jw": "🇮🇩 Javanese",
    "su": "🇮🇩 Sundanese",
    "ha": "🇳🇬 Hausa",
    "yo": "🇳🇬 Yoruba",
    "ln": "🇨🇩 Lingala",
    "so": "🇸🇴 Somali",
    "ps": "🇦🇫 Pashto",
    "yi": "🇮🇱 Yiddish",
}

DEFAULT_CONFIG = {
    "websocket_uri": os.getenv("WEBSOCKET_URI", "ws://localhost:8765"),
    "llm_api_base": os.getenv("LLM_API_BASE", "https://your-llm-endpoint/v1"),
    "llm_api_key": os.getenv("LLM_API_KEY", ""),
    "llm_model_name": os.getenv("LLM_MODEL_NAME", "meta-llama/Llama-3.2-1B-Instruct"),
    "llm_prompt_template": os.getenv("LLM_PROMPT_TEMPLATE", 'Answer the question: "{transcript}"\n\nAnswer concisely.'),
    "asr_server_address": os.getenv("ASR_SERVER_ADDRESS", "whisper-large-v3-predictor-00002-deployment.liav-hpe-com-ba9ce2f9.svc.cluster.local:9000"),
    "asr_language_code": "en-US",  # Input is always English (Whisper)
    "tts_server_address": os.getenv("TTS_SERVER_ADDRESS", "localhost:8000"),
    "tts_language_code": os.getenv("TTS_LANGUAGE_CODE", "en"),  # XTTS language code
    "tts_voice": os.getenv("TTS_VOICE", "default"),  # XTTS uses default speaker
    # Database configuration
    "db_enabled": os.getenv("DB_ENABLED", "false").lower() == "true",
    "db_host": os.getenv("DB_HOST", "postgres.default.svc.cluster.local"),
    "db_port": os.getenv("DB_PORT", "5432"),
    "db_name": os.getenv("DB_NAME", "customer_service"),
    "db_user": os.getenv("DB_USER", "agent"),
    "db_password": os.getenv("DB_PASSWORD", ""),
    "db_admin_user": os.getenv("DB_ADMIN_USER", "postgres"),
    "db_admin_password": os.getenv("DB_ADMIN_PASSWORD", ""),
}

TTS_SAMPLE_RATE = 24000
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH_BYTES = 2

# =============================================================================
# SERVICE STATUS MONITORING
# =============================================================================
def check_service_status(url: str, service_type: str = "generic", timeout: float = 10.0) -> dict:
    """Check if a service is available - improved for LLM/ingress endpoints"""
    try:
        if not url:
            return {"status": "⚠️ Not configured", "latency": "-"}
        
        # Normalize URL
        original_url = url.strip()
        if not original_url.startswith('http'):
            original_url = f"https://{original_url}"
        
        # Remove trailing slash
        original_url = original_url.rstrip('/')
        
        import time
        start = time.time()
        
        # Check if this is an internal cluster service (not reachable from UI)
        if '.svc.cluster.local' in original_url:
            return {"status": "🔒 Internal (OK)", "latency": "N/A"}
        
        # Helper function to try a request and handle SSL/connection variations
        def try_request(test_url, method="GET", json_data=None, headers=None):
            try:
                req_headers = headers or {}
                if method == "GET":
                    response = requests.get(test_url, timeout=timeout, verify=False, headers=req_headers)
                else:
                    response = requests.post(test_url, json=json_data, timeout=timeout, verify=False, headers=req_headers)
                latency = (time.time() - start) * 1000
                # Any HTTP response (including 4xx errors) means the server is alive
                # 401/403 = auth required but server is up, 404 = endpoint not found but server is up
                if response.status_code < 500:
                    return {"status": "✅ Online", "latency": f"{latency:.0f}ms"}
                # Even 5xx might mean server is overloaded but responding
                if response.status_code < 600:
                    return {"status": "⚠️ Responding (5xx)", "latency": f"{latency:.0f}ms"}
                return None
            except requests.exceptions.SSLError:
                # SSL error often means server is reachable but has cert issues
                latency = (time.time() - start) * 1000
                return {"status": "✅ Online (SSL)", "latency": f"{latency:.0f}ms"}
            except requests.exceptions.ConnectionError as e:
                err_str = str(e).lower()
                # Various SSL-related connection errors still indicate server is reachable
                if any(x in err_str for x in ['ssl', 'certificate', 'handshake', 'tls']):
                    latency = (time.time() - start) * 1000
                    return {"status": "✅ Online", "latency": f"{latency:.0f}ms"}
                # Connection refused = server not listening
                # Connection reset = server crashed
                return None
            except requests.exceptions.ReadTimeout:
                # Timeout means server might be slow but could be reachable
                latency = (time.time() - start) * 1000
                return {"status": "⚠️ Slow", "latency": f"{latency:.0f}ms"}
            except:
                return None
        
        # Different endpoints for different services
        if service_type == "whisper":
            endpoints = ['/v1/models', '/health', '/docs', '/']
            for endpoint in endpoints:
                result = try_request(f"{original_url}{endpoint}")
                if result:
                    return result
                    
        elif service_type == "llm":
            # Extract base URL by removing common API paths
            base_url = original_url
            for suffix in ['/v1/chat/completions', '/v1/completions', '/v1/models', '/v1']:
                if base_url.endswith(suffix):
                    base_url = base_url[:-len(suffix)]
                    break
            
            # Try multiple endpoints - prioritize common LLM API patterns
            # Many ingress-based LLMs respond to /v1/models or just the base URL
            test_urls = [
                original_url,  # Try original URL first (user provided this URL for a reason)
                f"{base_url}/v1/models",
                base_url,
                f"{base_url}/health", 
                f"{base_url}/api/health",
                f"{base_url}/v1",
            ]
            
            # Remove duplicates while preserving order
            seen = set()
            unique_urls = []
            for u in test_urls:
                if u and u not in seen:
                    seen.add(u)
                    unique_urls.append(u)
            
            for test_url in unique_urls:
                result = try_request(test_url)
                if result:
                    return result
            
            # Try POST to chat completions with proper headers (most LLMs respond to this)
            chat_url = f"{base_url}/v1/chat/completions"
            result = try_request(
                chat_url, 
                method="POST", 
                json_data={"model": "test", "messages": [{"role": "user", "content": "test"}]},
                headers={"Content-Type": "application/json"}
            )
            if result:
                return result
            
            # Final attempt: just try the base with a trailing slash
            result = try_request(f"{base_url}/")
            if result:
                return result
            
            return {"status": "❌ Offline", "latency": "-"}
            
        elif service_type == "xtts":
            endpoints = ['/health', '/info', '/']
            for endpoint in endpoints:
                result = try_request(f"{original_url}{endpoint}")
                if result:
                    return result
        else:
            endpoints = ['/health', '/v1/models', '/']
            for endpoint in endpoints:
                result = try_request(f"{original_url}{endpoint}")
                if result:
                    return result
        
        return {"status": "❌ Offline", "latency": "-"}
        
    except requests.exceptions.Timeout:
        return {"status": "⏱️ Timeout", "latency": "-"}
    except requests.exceptions.ConnectionError:
        return {"status": "❌ Unreachable", "latency": "-"}
    except Exception as e:
        return {"status": f"❌ Error", "latency": "-"}


def check_database_status(host, port, dbname, user, password):
    """Check database connectivity"""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password,
            connect_timeout=5
        )
        conn.close()
        return f"✅ Connected | `{dbname}@{host}`"
    except Exception as e:
        return f"❌ {str(e)[:30]}"


def check_all_services(asr_server, tts_server, llm_api_base, 
                       db_host, db_port, db_name, db_user, db_password, db_enabled):
    """Check status of all services"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    results = []
    
    # ASR (Whisper) - usually internal cluster service
    asr = check_service_status(asr_server, service_type="whisper")
    asr_display = (asr_server[:50] + "...") if asr_server and len(asr_server) > 50 else (asr_server or 'N/A')
    results.append(f"🎤 **ASR (Whisper):** {asr['status']} | {asr['latency']} | `{asr_display}`")
    
    # TTS (XTTS) - localhost in same pod
    tts = check_service_status(tts_server, service_type="xtts")
    results.append(f"🔊 **TTS (XTTS):** {tts['status']} | {tts['latency']} | `{tts_server or 'N/A'}`")
    
    # LLM - external ingress
    llm = check_service_status(llm_api_base, service_type="llm")
    llm_display = (llm_api_base[:45] + "...") if llm_api_base and len(llm_api_base) > 45 else (llm_api_base or 'N/A')
    results.append(f"🤖 **LLM:** {llm['status']} | {llm['latency']} | `{llm_display}`")
    
    # Database
    if db_enabled:
        db = check_database_status(db_host, db_port, db_name, db_user, db_password)
        results.append(f"🗄️ **Database:** {db}")
    else:
        results.append("🗄️ **Database:** ⚠️ Disabled")
    
    # WebSocket Server - health endpoint
    ws_uri = DEFAULT_CONFIG['websocket_uri']
    ws_http = ws_uri.replace('ws://', 'http://').replace('wss://', 'https://').replace(':8765', ':8766')
    ws = check_service_status(ws_http, service_type="generic")
    results.append(f"🔌 **WebSocket:** {ws['status']} | {ws['latency']}")
    
    # Add note about internal services
    note = "\n\n*🔒 Internal = Kubernetes internal service (verified working via WebSocket logs)*"
    
    return f"### 📊 Service Status ({timestamp})\n\n" + "\n\n".join(results) + note

def get_persona_names():
    return list(AGENT_PERSONAS.keys())

def get_persona_template(persona_name):
    return AGENT_PERSONAS.get(persona_name, AGENT_PERSONAS["🤖 Smart Assistant"])["template"]

def get_persona_description(persona_name):
    return AGENT_PERSONAS.get(persona_name, {}).get("description", "")

def get_tts_language_codes():
    return list(TTS_LANGUAGES.keys())

def get_language_display_name(lang_code):
    return TTS_LANGUAGES.get(lang_code, {}).get("display_name", lang_code)

def get_language_instruction(lang_code):
    return TTS_LANGUAGES.get(lang_code, {}).get("response_instruction", "")

def build_final_prompt(persona_name, lang_code, custom_template=None):
    if persona_name == "🎯 Custom" and custom_template:
        base_template = custom_template
    else:
        base_template = get_persona_template(persona_name)
    return base_template + get_language_instruction(lang_code)

def add_wav_header(audio_data, sample_rate, channels, sample_width):
    data_size = len(audio_data)
    chunk_size = 36 + data_size
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', chunk_size, b'WAVE',
        b'fmt ', 16, 1, channels,
        sample_rate, sample_rate * channels * sample_width,
        channels * sample_width, sample_width * 8,
        b'data', data_size
    )
    return header + audio_data


def strip_wav_header(data: bytes) -> bytes:
    """Strip WAV header from audio data, returning only raw PCM.
    
    WAV format:
    - Bytes 0-3: "RIFF"
    - Bytes 4-7: File size
    - Bytes 8-11: "WAVE"
    - Then chunks until "data" chunk
    - After "data" + 4 bytes size comes the actual audio
    """
    if len(data) < 44:
        # print(f"[strip_wav] Data too short ({len(data)} bytes), returning as-is")
        return data
    
    # Check if this is a WAV file
    header_start = data[:4]
    if header_start != b'RIFF' or data[8:12] != b'WAVE':
        # Not a WAV file, return as-is (this is expected for raw PCM)
        # print(f"[strip_wav] Not a WAV file (starts with {header_start!r}), returning {len(data)} bytes as-is")
        return data
    
    # print(f"[strip_wav] Found WAV header, stripping...")
    
    # Find the "data" chunk
    pos = 12
    while pos < len(data) - 8:
        chunk_id = data[pos:pos+4]
        try:
            chunk_size = struct.unpack('<I', data[pos+4:pos+8])[0]
        except struct.error:
            break
        
        if chunk_id == b'data':
            # Found data chunk - return everything after the header
            pcm_data = data[pos+8:]
            # print(f"[strip_wav] Stripped {pos+8} bytes header, returning {len(pcm_data)} bytes PCM")
            return pcm_data
        
        pos += 8 + chunk_size
    
    # Couldn't find data chunk, return original (minus standard 44-byte header)
    # print(f"[strip_wav] No data chunk found, stripping 44 bytes header")
    return data[44:] if len(data) > 44 else data

async def websocket_producer(audio_filepath, event_queue, settings):
    websocket_uri = settings.get('websocket_uri', DEFAULT_CONFIG['websocket_uri'])
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries + 1):
        try:
            async with websockets.connect(
                websocket_uri, 
                max_size=50 * 1024 * 1024,
                ping_interval=10,  # More frequent ping
                ping_timeout=20,
                close_timeout=10
            ) as websocket:
                await event_queue.put(("status", "✅ Connected to server"))
                
                config = {
                    "session_id": settings.get('session_id', 'default-session'),
                    "llm_api_base": settings.get('llm_api_base'),
                    "llm_api_key": settings.get('llm_api_key'),
                    "llm_model_name": settings.get('llm_model_name'),
                    "llm_prompt_template": settings.get('llm_prompt_template'),
                    "asr_server_address": settings.get('asr_server_address'),
                    "asr_language_code": settings.get('asr_language_code'),
                    "tts_server_address": settings.get('tts_server_address'),
                    "tts_language_code": settings.get('tts_language_code'),
                    "tts_voice": settings.get('tts_voice'),
                    "tts_sample_rate_hz": TTS_SAMPLE_RATE,
                    "db_enabled": settings.get('db_enabled', False),
                    "db_host": settings.get('db_host'),
                    "db_port": settings.get('db_port'),
                    "db_name": settings.get('db_name'),
                    "db_user": settings.get('db_user'),
                    "db_password": settings.get('db_password'),
                }
                await websocket.send(json.dumps(config))
                await event_queue.put(("status", f"📤 Configuration sent (Session: {config['session_id'][:20]}...)"))
                
                if settings.get('db_enabled'):
                    await event_queue.put(("status", "🗄️ Database mode enabled"))
                
                with open(audio_filepath, "rb") as f:
                    audio_data = f.read()
                
                await event_queue.put(("status", f"🎤 Sending {len(audio_data):,} bytes of audio"))
                await websocket.send(audio_data)
                await event_queue.put(("status", "⏳ Processing..."))
                
                while True:
                    try:
                        # Add timeout to prevent hanging
                        response = await asyncio.wait_for(websocket.recv(), timeout=60)
                        if isinstance(response, bytes):
                            await event_queue.put(("audio", response))
                        else:
                            # Try to parse as JSON first
                            try:
                                msg_json = json.loads(response)
                                if isinstance(msg_json, dict):
                                    if msg_json.get("type") == "customer_info":
                                        await event_queue.put(("customer_info", msg_json["data"]))
                                        continue
                                    elif msg_json.get("type") == "latency_report":
                                        await event_queue.put(("latency_report", msg_json["data"]))
                                        continue
                                    elif msg_json.get("type") == "visual_card":
                                        await event_queue.put(("visual_card", msg_json["data"]))
                                        continue
                                    elif msg_json.get("type") == "tts_fallback":
                                        # v5.0.1: TTS failed, show text response instead
                                        await event_queue.put(("tts_fallback", msg_json))
                                        continue
                                    elif msg_json.get("type") == "tts_error":
                                        # v5.0.1: TTS error info
                                        await event_queue.put(("tts_error", msg_json))
                                        continue
                            except:
                                pass

                            # Check for end of stream markers
                            if "__END_OF_STREAM__" in response:
                                await event_queue.put(("status", "✅ Response complete"))
                                break
                            elif "Agent response (no voice):" in response:
                                # v5.0.1: Fallback text response
                                await event_queue.put(("status", f"🔇 {response}"))
                            elif "Error" in response:
                                await event_queue.put(("status", f"⚠️ {response}"))
                                break
                            else:
                                await event_queue.put(("status", f"📨 {response}"))
                    except websockets.exceptions.ConnectionClosed:
                        await event_queue.put(("status", "🔌 Connection closed"))
                        break
                    except asyncio.TimeoutError:
                        await event_queue.put(("status", "⏱️ Response timeout"))
                        break
                
                # If we reach here successfully, break the retry loop
                break
                        
        except (websockets.exceptions.WebSocketException, OSError) as e:
            if attempt < max_retries:
                await event_queue.put(("status", f"⚠️ Connection failed, retrying ({attempt+1}/{max_retries})..."))
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                await event_queue.put(("status", f"❌ Connection error after retries: {str(e)}"))
        except Exception as e:
            await event_queue.put(("status", f"❌ Unexpected error: {str(e)}"))
            break
            
    await event_queue.put(("done", None))

async def process_audio(
    audio_filepath,
    llm_api_base, llm_api_key, llm_model_name, llm_prompt_template,
    asr_server_address, asr_language_code,
    tts_server_address, tts_language_code, tts_voice,
    db_enabled, db_host, db_port, db_name, db_user, db_password,
    session_suffix="initial",
    client_session_id=None
) -> AsyncGenerator:
    
    if isinstance(audio_filepath, dict):
        audio_filepath = audio_filepath.get('path', None)
    
    # Silent return for empty/invalid audio (don't show error in log)
    if audio_filepath is None:
        return
    
    if not os.path.exists(audio_filepath):
        return
    
    if os.path.getsize(audio_filepath) < 1000:  # Less than 1KB = not valid audio
        return
    
    # Use ASR language from settings, default to en-US
    if not asr_language_code or asr_language_code == "auto":
        asr_language_code = "auto"  # Let Whisper auto-detect
    
    # Session ID includes suffix so reset button creates new session
    import hashlib
    # Fix: Use client_session_id if available to ensure unique session per browser
    if client_session_id and str(client_session_id).strip():
        session_base = f"{client_session_id}:{session_suffix}"
    else:
        # Fallback to DB-based hash (legacy behavior, causes collisions)
        session_base = f"{db_host}:{db_port}:{db_name}:{db_user}:{session_suffix}"

    session_id = hashlib.md5(session_base.encode()).hexdigest()[:16]
    
    # Validate and fix LLM URL
    if llm_api_base:
        llm_api_base = llm_api_base.strip()
        if llm_api_base.startswith('ttps://'):
            llm_api_base = 'h' + llm_api_base
        elif not llm_api_base.startswith('http'):
            llm_api_base = 'https://' + llm_api_base
        llm_api_base = llm_api_base.rstrip('/')
        
    # Ensure all settings are strings (except booleans) to prevent JSON errors
    settings = {
        'websocket_uri': str(DEFAULT_CONFIG['websocket_uri']),
        'session_id': str(session_id),
        'llm_api_base': str(llm_api_base) if llm_api_base else "",
        'llm_api_key': str(llm_api_key) if llm_api_key else "",
        'llm_model_name': str(llm_model_name) if llm_model_name else "",
        'llm_prompt_template': str(llm_prompt_template) if llm_prompt_template else "",
        'asr_server_address': str(asr_server_address) if asr_server_address else "",
        'asr_language_code': str(asr_language_code) if asr_language_code else "en",
        'tts_server_address': str(tts_server_address) if tts_server_address else "",
        'tts_language_code': str(tts_language_code) if tts_language_code else "en",
        'tts_voice': str(tts_voice) if tts_voice else "default",
        'db_enabled': bool(db_enabled),
        'db_host': str(db_host) if db_host else "",
        'db_port': str(db_port) if db_port else "",
        'db_name': str(db_name) if db_name else "",
        'db_user': str(db_user) if db_user else "",
        'db_password': str(db_password) if db_password else "",
    }
    
    voice_short = tts_voice.split('.')[-1] if '.' in tts_voice else tts_voice
    lang_display = get_language_display_name(tts_language_code)
    
    status_log = "🚀 PIPELINE STARTED\n"
    status_log += "=" * 50 + "\n"
    status_log += f"🗣️ Voice: {voice_short}\n"
    status_log += f"🌍 Response Language: {lang_display}\n"
    status_log += f"🎤 Input Language: English (ASR)\n"
    status_log += f"🤖 Model: {llm_model_name}\n"
    if db_enabled:
        status_log += f"🗄️ Database: {db_name}@{db_host}\n"
    status_log += "=" * 50 + "\n\n"
    
    # Helper to prevent massive logs causing websocket crashes
    def truncate_log(log_text, max_chars=8000):
        if len(log_text) > max_chars:
            return "..." + log_text[-(max_chars):]
        return log_text

    # FIX v3.1.22: Use global cache for persistent customer info (survives across function calls)
    current_customer_info = get_customer_info(session_id)
    
    # FIX: Use global cache for metrics to prevent disappearance
    # Initialize with HTML
    cached_perf_html = get_metrics_cache(session_id)
    
    if cached_perf_html and "Latency Report" in cached_perf_html:
        last_perf_html = cached_perf_html
    else:
        # Default placeholder
        last_perf_html = format_metrics_html(None)
    
    print(f"[UI_DEBUG] Session {session_id[:8]} - Init metrics HTML len: {len(last_perf_html)}")

    # First yield - preserve existing customer info and metrics if we have them
    yield gr.update(), status_log, gr.update(value=None), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
    
    audio_buffer = bytearray()
    event_queue = asyncio.Queue()
    producer_task = asyncio.create_task(websocket_producer(audio_filepath, event_queue, settings))
    
    total_audio_chunks = 0
    
    try:
        while True:
            event_type, data = await event_queue.get()
            
            if event_type == "done":
                status_log += f"📊 Total audio chunks received: {total_audio_chunks}\n"
                break
            elif event_type == "status":
                status_log += f"{data}\n"
                status_log = truncate_log(status_log)
                yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
            
            elif event_type == "customer_info":
                if data and isinstance(data, dict):
                    # FIX v3.1.22: Store customer info in global cache AND local variable
                    current_customer_info = data
                    set_customer_info(session_id, data)  # Persist to global cache
                    status_log += f"✅ Customer identified: {data.get('name', 'Unknown')}\n"
                    status_log += f"   📧 Email: {data.get('email', 'N/A')}\n"
                    status_log += f"   📱 Phone: {data.get('phone', 'N/A')}\n"
                    status_log += f"   🎫 Open Tickets: {data.get('open_tickets', 0)}\n"
                    status_log += f"   💰 Overdue Invoices: {data.get('overdue_invoices', 0)}\n"
                    status_log = truncate_log(status_log)
                    # FIX v3.1.22: ONLY update customer_info_display HERE when customer is first identified!
                    yield gr.update(), status_log, gr.update(), gr.update(value=format_customer_info_html(current_customer_info)), gr.update(value=last_perf_html, visible=True), gr.update()
                else:
                    # Don't update the JSON component if data is empty/invalid
                    yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
            
            elif event_type == "visual_card":
                if data:
                    card_html = format_visual_card_html(data)
                    yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update(value=card_html, visible=True)

            elif event_type == "tts_fallback":
                # v5.0.1: TTS failed, show the text response
                if data:
                    agent_text = data.get("text", "")
                    status_log += f"\n🔇 VOICE FAILED - Agent response (text):\n"
                    status_log += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    status_log += f"💬 {agent_text}\n"
                    status_log += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    status_log += f"⚠️ TTS Server: {data.get('tts_server', 'Unknown')}\n"
                    status_log = truncate_log(status_log)
                    yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()

            elif event_type == "tts_error":
                # v5.0.1: TTS error details
                if data:
                    status_log += f"\n⚠️ TTS Error: {data.get('message', 'Unknown error')}\n"
                    status_log = truncate_log(status_log)
                    yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()

            elif event_type == "latency_report":
                if data:
                    # Generate fresh HTML from data
                    perf_html = format_metrics_html(data)
                    
                    last_perf_html = perf_html # Store to variable
                    set_metrics_cache(session_id, perf_html) # Persist to global cache
                    print(f"[UI_DEBUG] Session {session_id[:8]} - Received latency_report, updated HTML")
                    # Update UI
                    yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=perf_html, visible=True), gr.update()
                
            elif event_type == "audio":
                total_audio_chunks += 1
                # Strip WAV header if present (server might send WAV per sentence)
                raw_pcm = strip_wav_header(data)
                audio_buffer.extend(raw_pcm)
                
                # OPTIMIZATION: Removed intermediate yields to prevent flooding the ASGI connection
                # This prevents "h11._util.LocalProtocolError: Too little data" errors
        
        # Audio generation completed
        if len(audio_buffer) == 0:
            # Check if this was an error case (usually signaled by status update earlier)
            if "Error" not in status_log:
                status_log += "\n⚠️ Warning: Response generated but no audio content received.\n"

            print(f"[UI_DEBUG] Session {session_id[:8]} - Empty buffer")
            yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
            return

        print(f"[UI] Total accumulated PCM bytes: {len(audio_buffer)}")
        if len(audio_buffer) > 100:
             print(f"[UI] First 20 bytes: {list(audio_buffer[:20])}")

        status_log += f"\n✅ Creating audio from {len(audio_buffer):,} bytes...\n"
        print(f"[UI_DEBUG] Session {session_id[:8]} - Creating audio")
        yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
        
        # Validate audio buffer
        if len(audio_buffer) < 1000:  # Less than ~0.03 seconds of audio
            status_log += "⚠️ Audio too short, skipping output\n"
            yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
            return
        
        wav_data = add_wav_header(bytes(audio_buffer), TTS_SAMPLE_RATE, TTS_CHANNELS, TTS_SAMPLE_WIDTH_BYTES)
        
        # Validate WAV file
        if len(wav_data) < 100 or wav_data[:4] != b'RIFF':
            status_log += "⚠️ Invalid audio data, skipping output\n"
            yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
            return
        
        # === v5.1.0 FIX: Return base64 data URL for RELIABLE playback ===
        # This bypasses file serving issues and works in all browsers
        import base64
        
        try:
            # Convert to base64 data URL
            audio_b64 = base64.b64encode(wav_data).decode('utf-8')
            data_url = f"data:audio/wav;base64,{audio_b64}"
            
            status_log += f"🎧 READY - {len(wav_data):,} bytes\n"
            status_log += f"🔊 Audio prepared for playback\n"
            status_log += "=" * 50 + "\n"
            
            print(f"[UI] Audio converted to base64: {len(audio_b64)} chars")
            print(f"[UI_DEBUG] Session {session_id[:8]} - FINAL YIELD - base64 audio")
            
            # Flush event loop before final payload
            await asyncio.sleep(0.05)

            # Return base64 data URL - this will be picked up by JavaScript
            final_metrics_update = gr.update(value=last_perf_html, visible=True)
            yield data_url, status_log, gr.update(value=None), gr.update(), final_metrics_update, gr.update()
            
        except Exception as audio_err:
            print(f"[UI] Error creating audio: {audio_err}")
            import traceback
            traceback.print_exc()
            status_log += f"❌ Error: {audio_err}\n"
            yield gr.update(), status_log, gr.update(), gr.update(), gr.update(value=last_perf_html, visible=True), gr.update()
        
    except Exception as e:
        print(f"[UI] ERROR in process_audio: {str(e)}")
        status_log += f"\n❌ ERROR: {str(e)}\n"
        yield gr.update(), status_log, gr.update(), gr.update(), gr.update(), gr.update()
    finally:
        producer_task.cancel()


def create_ui():
    
    # =========================================================================
    # MODERN UI & CSS - Professional Design System (v5.1.4)
    # =========================================================================
    custom_css = """
    /* --- FONTS & BASICS --- */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    body, .gradio-container {
        font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
    }

    /* Hidden audio input for VAD */
    .hidden-audio-input {
        position: absolute !important;
        width: 1px !important;
        height: 1px !important;
        padding: 0 !important;
        margin: -1px !important;
        overflow: hidden !important;
        clip: rect(0, 0, 0, 0) !important;
        white-space: nowrap !important;
        border: 0 !important;
    }
    
    /* --- LAYOUT & CONTAINERS --- */
    .gradio-container {
        background-color: #f3f4f6 !important; /* Slate 50 */
        max-width: 1400px !important;
    }
    
    /* Panels / Cards */
    .gr-panel {
        background: white !important;
        border: 1px solid #e5e7eb !important; /* Gray 200 */
        border-radius: 12px !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03) !important;
        padding: 24px !important;
    }
    
    /* --- BUTTONS --- */
    .gr-button-primary {
        background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%) !important; /* Indigo 600-700 */
        border: none !important;
        color: white !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        padding: 10px 20px !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 4px 6px rgba(79, 70, 229, 0.2) !important;
    }
    .gr-button-primary:hover {
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 8px rgba(79, 70, 229, 0.3) !important;
        background: linear-gradient(135deg, #4338ca 0%, #3730a3 100%) !important;
    }
    
    .gr-button-secondary {
        background: white !important;
        border: 1px solid #d1d5db !important;
        color: #374151 !important; /* Gray 700 */
        font-weight: 500 !important;
        border-radius: 8px !important;
    }
    .gr-button-secondary:hover {
        background: #f9fafb !important;
        border-color: #9ca3af !important;
    }
    
    /* Stop/Delete Buttons */
    .variant-stop, .gr-button-stop {
        background: #fee2e2 !important; /* Red 100 */
        color: #991b1b !important; /* Red 800 */
        border: 1px solid #fecaca !important;
    }
    .variant-stop:hover {
        background: #fecaca !important;
    }

    /* --- TABS --- */
    .tabs {
        background: transparent !important;
        border: none !important;
    }
    .tab-nav {
        background: white !important;
        border-radius: 12px !important;
        padding: 4px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
        border: 1px solid #e5e7eb !important;
        display: flex !important;
        gap: 4px !important;
    }
    .tab-nav button {
        border-radius: 8px !important;
        border: none !important;
        font-weight: 500 !important;
        color: #6b7280 !important; /* Gray 500 */
        transition: all 0.2s !important;
    }
    .tab-nav button.selected {
        background: #f3f4f6 !important; /* Gray 100 */
        color: #111827 !important; /* Gray 900 */
        font-weight: 600 !important;
        box-shadow: none !important;
    }

    /* --- INPUTS --- */
    .gr-input, .gr-box, textarea, select {
        border: 1px solid #d1d5db !important;
        border-radius: 8px !important;
        background: #fff !important;
        transition: border-color 0.2s !important;
    }
    .gr-input:focus, textarea:focus {
        border-color: #4f46e5 !important;
        ring: 2px solid rgba(79, 70, 229, 0.2) !important;
    }

    /* --- DATA TABLES --- */
    table {
        border-collapse: separate !important; 
        border-spacing: 0 !important;
        border-radius: 8px !important;
        overflow: visible !important;
        border: 1px solid #e5e7eb !important;
    }
    th {
        background: #f9fafb !important;
        text-transform: uppercase !important;
        font-size: 0.75rem !important;
        letter-spacing: 0.05em !important;
        color: #6b7280 !important;
        padding: 12px 16px !important;
    }
    td {
        padding: 12px 16px !important;
        border-top: 1px solid #e5e7eb !important;
        color: #374151 !important;
        white-space: normal !important;
        word-wrap: break-word !important;
    }
    tr:hover td {
        background: #f9fafb !important;
    }

    /* --- CUSTOM COMPONENTS --- */
    /* Status Log */
    .status-log textarea {
        font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
        font-size: 13px !important;
        background: #1f2937 !important; /* Gray 800 */
        color: #e5e7eb !important; /* Gray 200 */
        line-height: 1.5 !important;
        border-radius: 8px !important;
    }

    /* Audio Player Custom */
    #audio-player-container {
        background: white !important;
        border: 1px solid #e5e7eb !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05) !important;
    }

    /* VAD Control Panel */
    #vad-panel {
        background: linear-gradient(145deg, #1e293b, #0f172a) !important;
        color: white !important;
        border-radius: 16px !important;
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1) !important;
    }
    
    /* Animations */
    @keyframes pulse-ring {
        0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7); }
        70% { transform: scale(1); box-shadow: 0 0 0 10px rgba(34, 197, 94, 0); }
        100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); }
    }
    .recording-active {
        animation: pulse-ring 2s infinite;
    }
    """
    
    # Create modern theme - Professional & Clean
    modern_theme = gr.themes.Default(
        primary_hue=gr.themes.colors.indigo,
        secondary_hue=gr.themes.colors.slate,
        neutral_hue=gr.themes.colors.gray,
        font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
        radius_size=gr.themes.sizes.radius_lg,
    ).set(
        button_primary_background_fill="#4f46e5",
        button_primary_background_fill_hover="#4338ca",
        button_primary_text_color="white",
        block_shadow="0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03)",
        block_border_width="1px",
        block_background_fill="white"
    )
    
    with gr.Blocks(title="HPE AI Voice Agent v5.1.4", theme=modern_theme, css=custom_css) as demo:
        
        gr.Markdown("""
        # 🎙️ AI Voice Agent
        ### Powered by HPE Private Cloud AI | v5.1.4
        
        **Interaction Modes:**
        - **Push-to-Talk**: Record → Click Send → Listen to response
        - **Conversation Mode**: Hands-free continuous conversation (speak to interrupt!)
        
        🌍 **17 Output Languages:** English, Spanish, French, German, Italian, Portuguese, Polish, Turkish, Russian, Dutch, Czech, Arabic, Chinese, Japanese, Hungarian, Korean, Hindi | Hebrew (Experimental)
        
        🛡️ **Smart Retention:** When customers want to leave, the agent will try to retain them!
        """)
        
        with gr.Tabs():
            
            with gr.TabItem("🎤 Voice Chat"):
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 🎙️ Your Input")
                        
                        chat_mode = gr.Radio(
                            choices=["📍 Push-to-Talk", "🔄 Conversation Mode"],
                            value="📍 Push-to-Talk",
                            label="Interaction Mode"
                        )
                        
                        with gr.Group(visible=True) as ptt_group:
                            input_audio = gr.Audio(
                                sources=["microphone"],
                                type="filepath",
                                label="🎙️ Record Audio"
                            )
                            upload_audio = gr.Audio(
                                sources=["upload"],
                                type="filepath",
                                label="📁 Or Upload Audio File"
                            )
                            with gr.Row():
                                submit_btn = gr.Button("🚀 Send Recording", variant="primary", size="lg")
                                submit_upload_btn = gr.Button("📤 Send Upload", variant="primary", size="lg")
                                clear_btn = gr.Button("🗑️ Clear", variant="secondary")
                        
                        with gr.Group(visible=False) as conv_group:
                            # Audio input must be visible (not visible=False) for file input to exist in DOM
                            # We hide it with elem_classes and CSS instead
                            conv_audio = gr.Audio(
                                sources=["upload"],
                                type="filepath",
                                visible=True,
                                elem_id="conv-audio-input",
                                elem_classes=["hidden-audio-input"]
                            )
                            # Hidden session suffix - changes when reset is clicked
                            session_suffix = gr.Textbox(value="initial", visible=False, elem_id="session-suffix")
                            # Client-side session ID (unique per browser)
                            client_session_id = gr.Textbox(value="", visible=False, elem_id="client-session-id")
                            
                            conv_html = gr.HTML(value="""
<div style="background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); border-radius: 16px; padding: 24px; color: white; position: relative; overflow: hidden;">
    <div style="text-align: center; margin-bottom: 20px;">
        <div id="conv-status" style="font-size: 22px; font-weight: 600;">🎤 Click Start or Press Space</div>
        <div id="conv-debug" style="font-size: 14px; color: #94a3b8; margin-top: 8px;">Level: -- | Threshold: -- | Session: --</div>
    </div>
    
    <!-- Canvas Visualizer -->
    <div style="background: rgba(0,0,0,0.3); border-radius: 12px; height: 140px; margin-bottom: 20px; position: relative; overflow: hidden; border: 1px solid rgba(255,255,255,0.1);">
        <canvas id="audio-visualizer" width="600" height="140" style="width: 100%; height: 100%;"></canvas>
        
        <!-- Processing Indicator -->
        <div id="processing-indicator" style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); display: none;">
            <div class="processing-pulse" style="width: 100px; height: 100px; display: flex; align-items: center; justify-content: center;">
                <span style="font-size: 32px;">🤖</span>
            </div>
        </div>
    </div>

    <div style="margin-bottom: 20px; padding: 0 10px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <label style="font-size: 14px; color: #94a3b8; font-weight: 600;">🎚️ Trigger Sensitivity</label>
            <span style="background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 4px; color: #fbbf24; font-weight: bold; font-size: 12px;" id="sens-value">20</span>
        </div>
        
        <!-- Visual slider track -->
        <div style="position: relative; height: 24px; display: flex; align-items: center;">
            <input type="range" id="sensitivity-slider" min="5" max="100" value="20" 
                style="width: 100%; cursor: pointer; height: 6px; accent-color: #fbbf24; z-index: 2;">
        </div>
        
        <div style="display: flex; justify-content: space-between; font-size: 11px; color: #64748b; margin-top: 4px;">
            <span>More Sensitive<br>(Detect Whispers)</span>
            <span style="text-align: right;">Less Sensitive<br>(Ignore Noise)</span>
        </div>
    </div>
    
    <!-- Hidden legacy bar for compatibility -->
    <div style="height: 0px; width: 0%; overflow: hidden;" id="level-bar"></div>
    
    <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
        <button id="start-btn" onclick="startVAD()" style="padding: 14px 40px; font-size: 16px; font-weight: 600; background: #22c55e; color: white; border: none; border-radius: 12px; cursor: pointer; box-shadow: 0 4px 12px rgba(34, 197, 94, 0.3);">▶️ Start</button>
        <button id="stop-btn" onclick="stopVAD()" style="padding: 14px 40px; font-size: 16px; font-weight: 600; background: #ef4444; color: white; border: none; border-radius: 12px; cursor: pointer; box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);">⏹️ Stop</button>
        <button id="calibrate-btn" onclick="calibrateNoise()" style="padding: 14px 20px; font-size: 14px; font-weight: 600; background: #6366f1; color: white; border: none; border-radius: 12px; cursor: pointer;" title="Auto-set threshold">⚡ Auto</button>
    </div>
    <div style="margin-top: 16px; text-align: center; font-size: 13px; color: #94a3b8;">
        💡 Tip: Press <b>Spacebar</b> to toggle recording
    </div>
</div>
                            """)
                            # Reset session button (Gradio button for proper state management)
                            reset_btn = gr.Button("🔄 New Session", variant="secondary", size="sm", elem_id="reset-session-btn")
                            
                            # Performance metrics display
                            # Using HTML for robust state management and better styling control
                            performance_display = gr.HTML(
                                value=format_metrics_html(None), 
                                visible=True, 
                                elem_id="performance-display"
                            )
                            # Rich Card display
                            rich_card_display = gr.HTML(visible=False)
                    
                    with gr.Column(scale=1):
                        gr.Markdown("### 🔊 Agent Response")
                        # Custom HTML audio player - more reliable than Gradio's Audio component
                        # v5.1.0: Enhanced with multiple play attempts and user interaction detection
                        gr.HTML("""
                        <div id="audio-player-container" style="padding: 10px; background: #f0f4f8; border-radius: 8px; margin-bottom: 10px; position: relative;">
                            <!-- Output Visualizer Canvas -->
                            <div style="height: 120px; width: 100%; margin-bottom: 10px; background: rgba(0,0,0,0.05); border-radius: 8px; overflow: hidden; position: relative; display: flex; align-items: center; justify-content: center;">
                                <canvas id="agent-visualizer" width="400" height="120" style="width: 100%; height: 100%;"></canvas>
                            </div>
                            
                            <audio id="custom-audio-player" controls autoplay playsinline preload="auto" style="width: 100%;">
                                Your browser does not support the audio element.
                            </audio>
                            <div id="audio-status" style="text-align: center; margin-top: 5px; font-size: 12px; color: #666;">
                                🔊 Ready - Click anywhere on page if audio doesn't play automatically
                            </div>
                            
                            <!-- Hidden play button for user interaction -->
                            <button id="force-play-btn" onclick="document.getElementById('custom-audio-player').play().catch(e=>console.log(e))" 
                                    style="display: none; margin-top: 10px; padding: 10px 20px; background: #4CAF50; color: white; border: none; border-radius: 5px; cursor: pointer; width: 100%;">
                                ▶️ Click to Play Audio
                            </button>
                        </div>
                        
                        <script>
                        // v5.1.0: Enable audio on first user interaction
                        (function() {
                            let audioEnabled = false;
                            const enableAudio = () => {
                                if (audioEnabled) return;
                                const player = document.getElementById('custom-audio-player');
                                if (player && player.src && player.paused) {
                                    player.play().then(() => {
                                        console.log('[Audio] Enabled by user interaction');
                                        audioEnabled = true;
                                    }).catch(e => {});
                                }
                            };
                            document.addEventListener('click', enableAudio);
                            document.addEventListener('keydown', enableAudio);
                            document.addEventListener('touchstart', enableAudio);
                        })();
                        </script>
                        """)
                        # Hidden textbox to receive audio filepath (served by Gradio)
                        audio_data_holder = gr.Textbox(
                            label="Audio Path", 
                            elem_id="audio-data-holder",
                            visible=False
                        )
                        
                        # Customer Info Window (Updates when identified)
                        # FIX v3.1.22: Improved styling with !important
                        gr.Markdown("#### 👤 Customer Context")
                        customer_info_display = gr.HTML(
                            value='''<div id="customer-info-content" data-customer-id="" style="padding: 24px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-radius: 16px; border: 2px dashed #adb5bd; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
                                <div style="color: #495057; font-size: 16px; text-align: center;">
                                    <div style="font-size: 48px; margin-bottom: 12px; opacity: 0.7;">👤</div>
                                    <strong style="color: #343a40;">Waiting for customer identification...</strong>
                                </div>
                            </div>''',
                            elem_id="customer-info-display"
                        )
                        
                        # Hidden components for auto-refresh
                        customer_id_holder = gr.Textbox(value="", elem_id="customer-id-holder", visible=False)
                        refresh_trigger = gr.Button("Refresh", elem_id="refresh-trigger-btn", visible=False)
                        
                        # Note: Customer info is persisted via JavaScript (see VAD_SCRIPT)
                        
                        status_log = gr.Textbox(label="📋 Processing Log", lines=12, interactive=False)
                
                # Save Settings Section at bottom of Voice Chat
                with gr.Row():
                    gr.Markdown("---")
                with gr.Row():
                    with gr.Column(scale=3):
                        main_save_status = gr.Markdown("💾 *Settings auto-save on change*")
                    with gr.Column(scale=1):
                        main_save_btn = gr.Button("💾 Save Settings", variant="secondary", size="sm")
            
            with gr.TabItem("🤖 Agent Settings"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("#### 🔌 LLM Connection")
                        llm_api_base = gr.Textbox(label="API Base URL", value=DEFAULT_CONFIG['llm_api_base'], elem_id="llm_api_base")
                        llm_api_key = gr.Textbox(label="API Key", value=DEFAULT_CONFIG['llm_api_key'], type="password", elem_id="llm_api_key")
                        llm_model_name = gr.Textbox(label="Model Name", value=DEFAULT_CONFIG['llm_model_name'], elem_id="llm_model")
                    
                    with gr.Column():
                        gr.Markdown("#### 🎭 Agent Personality")
                        agent_persona = gr.Dropdown(label="Persona", choices=get_persona_names(), value="🤖 Smart Assistant", elem_id="agent_persona")
                        persona_desc = gr.Markdown(value="*A helpful, concise assistant*")
                
                llm_prompt_template = gr.Textbox(label="Prompt Template", value=DEFAULT_CONFIG['llm_prompt_template'], lines=4, interactive=False)
                custom_template = gr.Textbox(label="Custom Template", value='{transcript}', lines=4, visible=False)
            
            with gr.TabItem("🗄️ Database"):
                gr.Markdown("### Customer Database Configuration")
                
                with gr.Accordion("📌 Connection Settings", open=True):
                    with gr.Row():
                        with gr.Column():
                            db_enabled = gr.Checkbox(
                                label="🔌 Enable Database Connection",
                                value=DEFAULT_CONFIG['db_enabled'],
                                info="When enabled, the agent can query customer data",
                                elem_id="db_enabled"
                            )
                            db_host = gr.Textbox(
                                label="Database Host",
                                value=DEFAULT_CONFIG['db_host'],
                                placeholder="postgres.namespace.svc.cluster.local",
                                elem_id="db_host"
                            )
                            db_port = gr.Textbox(
                                label="Database Port",
                                value=DEFAULT_CONFIG['db_port'],
                                elem_id="db_port"
                            )
                        
                        with gr.Column():
                            db_name = gr.Textbox(
                                label="Database Name",
                                value=DEFAULT_CONFIG['db_name'],
                                placeholder="customer_service",
                                elem_id="db_name"
                            )
                            db_user = gr.Textbox(
                                label="Database User",
                                value=DEFAULT_CONFIG['db_user'],
                                elem_id="db_user"
                            )
                            db_password = gr.Textbox(
                                label="Database Password",
                                value=DEFAULT_CONFIG['db_password'],
                                type="password",
                                elem_id="db_password"
                            )
                
                with gr.Accordion("🔐 Admin Credentials (for creating/deleting databases)", open=False):
                    gr.Markdown("⚠️ These credentials are only needed for creating or deleting databases.")
                    with gr.Row():
                        db_admin_user = gr.Textbox(
                            label="Admin User",
                            value=DEFAULT_CONFIG['db_admin_user'],
                            placeholder="postgres"
                        )
                        db_admin_password = gr.Textbox(
                            label="Admin Password",
                            value=DEFAULT_CONFIG['db_admin_password'],
                            type="password"
                        )
                
                gr.Markdown("---")
                gr.Markdown("### 🛠️ Database Management")
                
                with gr.Row():
                    test_conn_btn = gr.Button("🔗 Test Connection", variant="secondary")
                    get_stats_btn = gr.Button("📊 Get Statistics", variant="secondary")
                    list_dbs_btn = gr.Button("📚 List Databases", variant="secondary")
                
                with gr.Row():
                    create_db_btn = gr.Button("➕ Create Database", variant="primary")
                    init_tables_btn = gr.Button("🏗️ Initialize Tables", variant="primary")
                    drop_db_btn = gr.Button("🗑️ Drop Database", variant="stop")
                
                db_status = gr.Textbox(label="Status", lines=8, interactive=False)
            
            with gr.TabItem("🎤 Voice Input"):
                gr.Markdown("### ASR Configuration (Whisper)")
                gr.Markdown("*Whisper supports 99+ languages for input. The agent will understand any language but respond in the TTS language.*")
                
                asr_server_address = gr.Textbox(label="ASR Server", value=DEFAULT_CONFIG['asr_server_address'], elem_id="asr_server_address")
                asr_language_code = gr.Dropdown(
                    label="Input Language (Your Speech)", 
                    choices=list(ASR_LANGUAGES.keys()),
                    value="en",
                    info="Select the language you will speak in",
                    elem_id="asr_language_code"
                )
                asr_lang_display = gr.Markdown(value="**Selected:** 🇺🇸 English")
                
                def update_asr_display(lang_code):
                    return f"**Selected:** {ASR_LANGUAGES.get(lang_code, lang_code)}"
                
                asr_language_code.change(fn=update_asr_display, inputs=[asr_language_code], outputs=[asr_lang_display])
            
            with gr.TabItem("🔊 Voice Output"):
                gr.Markdown("### TTS Configuration (XTTS v2)")
                gr.Markdown("*XTTS v2 supports 17 languages with natural voice synthesis. Hebrew is experimental.*")
                
                with gr.Tabs():
                    with gr.TabItem("Settings"):
                        tts_server_address = gr.Textbox(label="TTS Server", value=DEFAULT_CONFIG['tts_server_address'], elem_id="tts_server_address")
                        tts_language_code = gr.Dropdown(
                            label="Response Language", 
                            choices=get_tts_language_codes(), 
                            value=DEFAULT_CONFIG['tts_language_code'],
                            info="The language the agent will speak in",
                            elem_id="tts_language_code"
                        )
                        lang_display = gr.Markdown(value=f"**Selected:** {get_language_display_name(DEFAULT_CONFIG['tts_language_code'])}")
                        
                        # =====================================================
                        # XTTS v2 OFFICIAL SPEAKERS (33 voices - verified)
                        # Source: Coqui TTS official documentation
                        # Note: Hebrew (he) is NOT officially supported!
                        # Supported: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, hu, ko
                        # =====================================================
                        
                        # FEMALE VOICES - 15 Official XTTS v2 speakers
                        XTTS_VOICES_FEMALE = [
                            # English/International Female
                            "Claribel Dervla",     # 🇮🇪 Irish - Default, soft melodic
                            "Daisy Studious",      # 🇺🇸 American - Clear, professional
                            "Gracie Wise",         # 🇺🇸 American - Warm, friendly
                            "Brenda Stern",        # 🇺🇸 American - Authoritative
                            "Tammy Grit",          # 🇺🇸 American - Casual, natural
                            # British Female
                            "Ana Florence",        # 🇬🇧 British - Professional
                            "Sofia Hellen",        # 🇬🇧 British - Warm, clear
                            "Alison Dietlinde",    # 🇬🇧 British - Professional
                            "Tammie Ema",          # 🇬🇧 British - Friendly
                            # European Female
                            "Henriette Usha",      # 🇩🇪 German accent - Clear
                            "Gitta Nikolina",      # 🇩🇪 German accent - Warm
                            "Tanja Adelina",       # 🇩🇪 German accent - Professional
                            "Annmarie Nele",       # 🇫🇷 French accent - Elegant
                            # Other Accents Female
                            "Asya Anara",          # 🇹🇷 Turkish accent - Soft
                            "Vjollca Johnnie",     # 🇦🇱 Albanian/Italian - Melodic
                        ]
                        
                        # MALE VOICES - 18 Official XTTS v2 speakers
                        XTTS_VOICES_MALE = [
                            # English/American Male
                            "Andrew Chipper",      # 🇺🇸 American - Professional, clear
                            "Craig Gutsy",         # 🇺🇸 American - Strong, confident
                            "Damien Black",        # 🇺🇸 American - Deep, authoritative
                            "Abrahan Mack",        # 🇺🇸 American - Warm, friendly
                            "Viktor Eka",          # 🌐 Neutral - Professional (versatile, RECOMMENDED)
                            "Royston Min",         # 🇺🇸 American/Asian - Clear
                            # British/Celtic Male
                            "Gilberto Mathias",    # 🇬🇧 British - Professional
                            "Torcull Diarmuid",    # 🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scottish/Irish - Warm
                            "Zacharie Aimilios",   # 🇬🇷 Greek accent - Clear
                            # European Male
                            "Baldur Sanjin",       # 🇩🇪 German accent - Strong
                            "Adde Michal",         # 🇫🇷 French accent - Elegant
                            "Ludvig Milivoj",      # 🇭🇷 Croatian/Slavic - Professional
                            "Ilkin Urbano",        # 🇦🇿 Azerbaijani - Clear
                            "Viktor Menelaos",     # 🇬🇷 Greek accent - Warm
                            # Spanish/Portuguese Male
                            "Dionisio Schuyler",   # 🇪🇸 Spanish accent - Professional
                            # Middle Eastern/African Male
                            "Suad Qasim",          # 🇸🇦 Arabic accent - Clear
                            "Badr Odhiambo",       # 🇰🇪 Kenyan/Arabic - Warm
                            # Asian Male
                            "Kazuhiko Atallah",    # 🇯🇵 Japanese accent - Professional
                        ]
                        
                        ALL_XTTS_VOICES = ["default"] + XTTS_VOICES_FEMALE + XTTS_VOICES_MALE
                        
                        # Recommended voices per OFFICIALLY SUPPORTED language
                        # Note: Hebrew is NOT supported - will use English voices with Hebrew text
                        VOICES_BY_LANG = {
                            "en": {
                                "female": ["Daisy Studious", "Gracie Wise", "Ana Florence", "Sofia Hellen", "Brenda Stern", "Claribel Dervla"],
                                "male": ["Andrew Chipper", "Craig Gutsy", "Damien Black", "Viktor Eka", "Abrahan Mack", "Gilberto Mathias"]
                            },
                            "es": {
                                "female": ["Claribel Dervla", "Daisy Studious", "Ana Florence", "Vjollca Johnnie"],
                                "male": ["Dionisio Schuyler", "Viktor Eka", "Andrew Chipper", "Gilberto Mathias"]
                            },
                            "fr": {
                                "female": ["Annmarie Nele", "Claribel Dervla", "Ana Florence", "Sofia Hellen"],
                                "male": ["Adde Michal", "Viktor Eka", "Gilberto Mathias", "Andrew Chipper"]
                            },
                            "de": {
                                "female": ["Henriette Usha", "Gitta Nikolina", "Tanja Adelina", "Ana Florence"],
                                "male": ["Baldur Sanjin", "Viktor Eka", "Andrew Chipper", "Gilberto Mathias"]
                            },
                            "it": {
                                "female": ["Vjollca Johnnie", "Claribel Dervla", "Ana Florence", "Sofia Hellen"],
                                "male": ["Viktor Menelaos", "Gilberto Mathias", "Viktor Eka", "Dionisio Schuyler"]
                            },
                            "pt": {
                                "female": ["Claribel Dervla", "Ana Florence", "Vjollca Johnnie", "Sofia Hellen"],
                                "male": ["Dionisio Schuyler", "Viktor Eka", "Andrew Chipper", "Gilberto Mathias"]
                            },
                            "pl": {
                                "female": ["Claribel Dervla", "Ana Florence", "Sofia Hellen", "Tanja Adelina"],
                                "male": ["Ludvig Milivoj", "Ilkin Urbano", "Viktor Eka", "Andrew Chipper"]
                            },
                            "tr": {
                                "female": ["Asya Anara", "Claribel Dervla", "Ana Florence", "Sofia Hellen"],
                                "male": ["Suad Qasim", "Badr Odhiambo", "Viktor Eka", "Andrew Chipper"]
                            },
                            "ru": {
                                "female": ["Claribel Dervla", "Ana Florence", "Tanja Adelina", "Sofia Hellen"],
                                "male": ["Ludvig Milivoj", "Ilkin Urbano", "Viktor Menelaos", "Viktor Eka"]
                            },
                            "nl": {
                                "female": ["Claribel Dervla", "Ana Florence", "Tammie Ema", "Alison Dietlinde"],
                                "male": ["Zacharie Aimilios", "Ludvig Milivoj", "Viktor Eka", "Andrew Chipper"]
                            },
                            "cs": {
                                "female": ["Claribel Dervla", "Ana Florence", "Tanja Adelina", "Sofia Hellen"],
                                "male": ["Ludvig Milivoj", "Ilkin Urbano", "Viktor Eka", "Andrew Chipper"]
                            },
                            "ar": {
                                "female": ["Asya Anara", "Claribel Dervla", "Ana Florence", "Sofia Hellen"],
                                "male": ["Suad Qasim", "Badr Odhiambo", "Viktor Eka", "Andrew Chipper"]
                            },
                            "zh-cn": {
                                "female": ["Claribel Dervla", "Daisy Studious", "Ana Florence", "Sofia Hellen"],
                                "male": ["Kazuhiko Atallah", "Royston Min", "Viktor Eka", "Andrew Chipper"]
                            },
                            "ja": {
                                "female": ["Claribel Dervla", "Daisy Studious", "Ana Florence", "Sofia Hellen"],
                                "male": ["Kazuhiko Atallah", "Royston Min", "Viktor Eka", "Andrew Chipper"]
                            },
                            "hu": {
                                "female": ["Claribel Dervla", "Ana Florence", "Tanja Adelina", "Sofia Hellen"],
                                "male": ["Ludvig Milivoj", "Viktor Menelaos", "Viktor Eka", "Andrew Chipper"]
                            },
                            "ko": {
                                "female": ["Claribel Dervla", "Daisy Studious", "Ana Florence", "Sofia Hellen"],
                                "male": ["Kazuhiko Atallah", "Royston Min", "Viktor Eka", "Andrew Chipper"]
                            },
                            "hi": {
                                "female": ["Asya Anara", "Claribel Dervla", "Ana Florence", "Sofia Hellen"],
                                "male": ["Badr Odhiambo", "Suad Qasim", "Viktor Eka", "Andrew Chipper"]
                            },
                        }
                        
                        # Function to get voices for a language (combined male + female)
                        def get_voices_for_lang(lang):
                            if lang in VOICES_BY_LANG:
                                voices = VOICES_BY_LANG[lang]
                                return ["default"] + voices.get("female", []) + voices.get("male", [])
                            return ALL_XTTS_VOICES
                        
                        # Gender selector
                        voice_gender = gr.Radio(
                            label="Voice Gender",
                            choices=["All", "Female", "Male"],
                            value="All",
                            info="Filter voices by gender"
                        )

                        tts_voice = gr.Dropdown(
                            label="Voice", 
                            choices=get_voices_for_lang("en"),
                            value="Viktor Eka",
                            allow_custom_value=True,
                            info="33 official XTTS v2 voices with accent-matched recommendations per language"
                        )
                        
                        # Update voice choices when language changes
                        def update_voice_choices(lang, gender):
                            if lang in VOICES_BY_LANG:
                                if gender == "Female":
                                    choices = ["default"] + VOICES_BY_LANG[lang].get("female", [])
                                elif gender == "Male":
                                    choices = ["default"] + VOICES_BY_LANG[lang].get("male", [])
                                else:
                                    choices = get_voices_for_lang(lang)
                            else:
                                if gender == "Female":
                                    choices = ["default"] + XTTS_VOICES_FEMALE
                                elif gender == "Male":
                                    choices = ["default"] + XTTS_VOICES_MALE
                                else:
                                    choices = ALL_XTTS_VOICES
                            return gr.update(choices=choices, value=choices[1] if len(choices) > 1 else "default")
                        
                        tts_language_code.change(
                            fn=update_voice_choices,
                            inputs=[tts_language_code, voice_gender],
                            outputs=[tts_voice]
                        )
                        
                        voice_gender.change(
                            fn=update_voice_choices,
                            inputs=[tts_language_code, voice_gender],
                            outputs=[tts_voice]
                        )
                    
                    with gr.TabItem("Voice Cloning"):
                        gr.Markdown("### 🧬 Clone a Voice")
                        gr.Markdown("Upload a short audio sample (WAV) to clone a new voice.")
                        
                        with gr.Row():
                            clone_name = gr.Textbox(label="Speaker Name", placeholder="e.g. My Custom Voice")
                            clone_file = gr.File(label="Reference Audio (WAV)", file_types=[".wav"])
                        
                        clone_btn = gr.Button("🧬 Upload & Clone Voice", variant="primary")
                        clone_status = gr.Textbox(label="Status", interactive=False)
                        
                        def upload_voice(name, file_obj, server_url):
                            if not name or not file_obj:
                                return "❌ Name and file are required"
                            
                            try:
                                url = f"http://{server_url}/speakers/upload"
                                if not server_url.startswith('http'):
                                    url = f"http://{server_url}/speakers/upload"
                                    
                                files = {'audio': open(file_obj, 'rb')}
                                data = {'name': name}
                                
                                response = requests.post(url, files=files, data=data, timeout=30)
                                
                                if response.status_code == 200:
                                    return f"✅ Success! Voice '{name}' cloned and saved."
                                else:
                                    return f"❌ Error: {response.text}"
                            except Exception as e:
                                return f"❌ Connection Error: {str(e)}"
                        
                        clone_btn.click(
                            fn=upload_voice,
                            inputs=[clone_name, clone_file, tts_server_address],
                            outputs=[clone_status]
                        )
            
            with gr.TabItem("📊 Data Viewer"):
                gr.Markdown("### View & Edit Database Records")
                gr.Markdown("Browse, add, edit, and delete records in the customer service database.")
                
                with gr.Tabs():
                    with gr.TabItem("👥 Customers"):
                        gr.Markdown("#### 👤 Customer Management")
                        with gr.Row():
                            refresh_customers_btn = gr.Button("🔄 Refresh", variant="secondary")
                        customers_table = gr.Dataframe(
                            label="Customers",
                            headers=["id", "name", "email", "phone", "pin", "created_at"],
                            interactive=False,
                            wrap=True
                        )
                        
                        gr.Markdown("#### ➕ Add / ✏️ Edit Customer")
                        with gr.Row():
                            cust_id_input = gr.Textbox(label="ID (for edit/delete)", placeholder="Leave empty for new")
                            cust_name_input = gr.Textbox(label="Name", placeholder="John Doe")
                            cust_email_input = gr.Textbox(label="Email", placeholder="john@email.com")
                            cust_phone_input = gr.Textbox(label="Phone", placeholder="+1-555-0100")
                            cust_pin_input = gr.Textbox(label="PIN Code", placeholder="4 digits", max_lines=1)
                        with gr.Row():
                            add_customer_btn = gr.Button("➕ Add", variant="primary")
                            update_customer_btn = gr.Button("✏️ Update", variant="secondary")
                            delete_customer_btn = gr.Button("🗑️ Delete", variant="stop")
                        customer_action_status = gr.Textbox(label="Status", interactive=False)
                    
                    with gr.TabItem("📋 Plans"):
                        gr.Markdown("#### 📦 Subscription Plans")
                        with gr.Row():
                            refresh_plans_btn = gr.Button("🔄 Refresh", variant="secondary")
                        plans_table = gr.Dataframe(
                            label="Plans",
                            headers=["id", "name", "price", "billing_cycle", "data_limit_gb", "support_level"],
                            interactive=False,
                            wrap=True
                        )
                        
                        gr.Markdown("#### ➕ Add / ✏️ Edit Plan")
                        with gr.Row():
                            plan_id_input = gr.Textbox(label="ID (for edit/delete)", placeholder="Leave empty for new", scale=1)
                            plan_name_input = gr.Textbox(label="Plan Name", placeholder="Premium", scale=2)
                            plan_price_input = gr.Number(label="Price ($)", value=29.99, scale=1)
                        with gr.Row():
                            plan_billing_input = gr.Dropdown(
                                label="Billing Cycle",
                                choices=["monthly", "yearly", "weekly"],
                                value="monthly",
                                scale=1
                            )
                            plan_data_input = gr.Number(label="Data Limit (GB)", value=100, scale=1)
                            plan_support_input = gr.Dropdown(
                                label="Support Level",
                                choices=["basic", "standard", "premium", "enterprise"],
                                value="standard",
                                scale=1
                            )
                        with gr.Row():
                            add_plan_btn = gr.Button("➕ Add Plan", variant="primary")
                            update_plan_btn = gr.Button("✏️ Update", variant="secondary")
                            delete_plan_btn = gr.Button("🗑️ Delete", variant="stop")
                        plan_action_status = gr.Textbox(label="Status", interactive=False)
                    
                    with gr.TabItem("📝 Subscriptions"):
                        gr.Markdown("#### 📑 Active Subscriptions")
                        with gr.Row():
                            refresh_subs_btn = gr.Button("🔄 Refresh", variant="secondary")
                        subs_table = gr.Dataframe(
                            label="Subscriptions",
                            headers=["id", "customer_name", "plan_name", "status", "start_date", "end_date", "auto_renew"],
                            interactive=False,
                            wrap=True
                        )
                        
                        gr.Markdown("#### ➕ Add New Subscription")
                        with gr.Row():
                            sub_customer_input = gr.Textbox(label="Customer ID", placeholder="Enter customer ID", scale=1)
                            sub_plan_input = gr.Textbox(label="Plan ID", placeholder="Enter plan ID", scale=1)
                            sub_status_input = gr.Dropdown(
                                label="Status",
                                choices=["active", "pending", "suspended", "cancelled"],
                                value="active",
                                scale=1
                            )
                            sub_autorenew_input = gr.Dropdown(
                                label="Auto Renew",
                                choices=["Yes", "No"],
                                value="Yes",
                                scale=1
                            )
                        with gr.Row():
                            add_sub_btn = gr.Button("➕ Add Subscription", variant="primary")
                        
                        gr.Markdown("#### ✏️ Update/Cancel Subscription")
                        with gr.Row():
                            sub_id_input = gr.Textbox(label="Subscription ID", placeholder="Enter subscription ID", scale=1)
                            sub_update_status = gr.Dropdown(
                                label="New Status",
                                choices=["active", "pending", "suspended", "cancelled"],
                                value="active",
                                scale=1
                            )
                            sub_update_renew = gr.Dropdown(
                                label="Auto Renew",
                                choices=["Yes", "No"],
                                value="Yes",
                                scale=1
                            )
                        with gr.Row():
                            update_sub_btn = gr.Button("✏️ Update", variant="secondary")
                            cancel_sub_btn = gr.Button("❌ Cancel Subscription", variant="stop")
                        sub_action_status = gr.Textbox(label="Status", interactive=False)
                    
                    with gr.TabItem("💰 Invoices"):
                        gr.Markdown("#### 🧾 Invoices")
                        with gr.Row():
                            refresh_inv_btn = gr.Button("🔄 Refresh", variant="secondary")
                        invoices_table = gr.Dataframe(
                            label="Invoices",
                            headers=["id", "customer_name", "amount", "status", "due_date", "paid_at"],
                            interactive=False,
                            wrap=True
                        )
                        
                        gr.Markdown("#### ➕ Create New Invoice")
                        with gr.Row():
                            inv_customer_input = gr.Textbox(label="Customer ID", placeholder="Enter customer ID", scale=1)
                            inv_amount_input = gr.Number(label="Amount ($)", value=99.99, scale=1)
                            inv_due_date_input = gr.Textbox(label="Due Date (YYYY-MM-DD)", placeholder="2025-02-01", scale=1)
                            inv_new_status_input = gr.Dropdown(
                                label="Status",
                                choices=["pending", "paid", "overdue"],
                                value="pending",
                                scale=1
                            )
                        with gr.Row():
                            add_invoice_btn = gr.Button("➕ Create Invoice", variant="primary")
                            delete_invoice_btn = gr.Button("🗑️ Delete", variant="stop")
                        
                        gr.Markdown("#### ✏️ Update Invoice Status")
                        with gr.Row():
                            invoice_id_input = gr.Textbox(label="Invoice ID", placeholder="Enter invoice ID")
                            invoice_status_input = gr.Dropdown(
                                label="New Status",
                                choices=["pending", "paid", "overdue"],
                                value="paid"
                            )
                            update_invoice_btn = gr.Button("✏️ Update", variant="primary")
                        invoice_action_status = gr.Textbox(label="Status", interactive=False)
                    
                    with gr.TabItem("🎫 Tickets"):
                        gr.Markdown("#### 🎟️ Support Tickets")
                        with gr.Row():
                            refresh_tickets_btn = gr.Button("🔄 Refresh", variant="secondary")
                        tickets_table = gr.Dataframe(
                            label="Support Tickets",
                            headers=["id", "customer_name", "subject", "status", "priority", "created_at", "resolved_at"],
                            interactive=False,
                            wrap=True
                        )
                        
                        gr.Markdown("#### ➕ Create New Ticket")
                        with gr.Row():
                            ticket_customer_input = gr.Textbox(label="Customer ID", placeholder="Enter customer ID", scale=1)
                            ticket_subject_input = gr.Textbox(label="Subject", placeholder="Service issue", scale=2)
                        with gr.Row():
                            ticket_desc_input = gr.Textbox(label="Description", placeholder="Describe the issue...", lines=2, scale=3)
                            ticket_priority_input = gr.Dropdown(
                                label="Priority",
                                choices=["low", "medium", "high", "urgent"],
                                value="medium",
                                scale=1
                            )
                        with gr.Row():
                            add_ticket_btn = gr.Button("➕ Create Ticket", variant="primary")
                        
                        gr.Markdown("#### ✏️ Update Ticket Status")
                        with gr.Row():
                            ticket_id_input = gr.Textbox(label="Ticket ID", placeholder="Enter ticket ID")
                            ticket_status_input = gr.Dropdown(
                                label="New Status",
                                choices=["open", "in_progress", "resolved", "closed"],
                                value="resolved"
                            )
                            update_ticket_btn = gr.Button("✏️ Update", variant="primary")
                            delete_ticket_btn = gr.Button("🗑️ Delete", variant="stop")
                        ticket_action_status = gr.Textbox(label="Status", interactive=False)
                    
                    with gr.TabItem("⬆️ Upgrade Requests"):
                        with gr.Row():
                            refresh_upgrades_btn = gr.Button("🔄 Refresh", variant="secondary")
                        upgrades_table = gr.Dataframe(
                            label="Upgrade Requests",
                            headers=["id", "customer_name", "email", "current_plan", "target_plan", "status", "created_at"],
                            interactive=False,
                            wrap=True
                        )
                        gr.Markdown("""
                        *Upgrade requests are created when customers request to upgrade their plan via voice.*
                        
                        These requests should be processed by a human agent who will:
                        1. Call the customer back
                        2. Verify the upgrade request
                        3. Process payment if needed
                        4. Update the subscription
                        """)
                    
                    with gr.TabItem("😠 Sentiment Alerts"):
                        with gr.Row():
                            refresh_alerts_btn = gr.Button("🔄 Refresh", variant="secondary")
                        
                        gr.Markdown("""
                        **Sentiment alerts are triggered when customers express frustration or intent to leave:**
                        - 🔴 **High Risk:** Cancellation intent → Agent activates **retention mode** with callback promise
                        - 🟡 **Medium Risk:** Frustration, complaints → Agent responds with empathy
                        - 🟢 **Low Risk:** Minor issues → Normal handling
                        
                        *The agent automatically detects these in 17 languages and responds appropriately.*
                        """)
                        
                        alerts_table = gr.Dataframe(
                            label="Active Alerts (Unresolved)",
                            headers=["id", "customer_name", "email", "sentiment", "churn_risk", "trigger_phrases", "message", "action", "created_at"],
                            interactive=False,
                            wrap=True
                        )
                    
                    with gr.TabItem("🔍 Custom Query"):
                        gr.Markdown("#### Run Custom SQL Queries")
                        gr.Markdown("⚠️ Only SELECT queries are allowed for safety.")
                        custom_query_input = gr.Textbox(
                            label="SQL Query",
                            placeholder="SELECT * FROM customers WHERE name LIKE '%John%'",
                            lines=3
                        )
                        run_query_btn = gr.Button("▶️ Run Query", variant="primary")
                        query_status = gr.Textbox(label="Status", interactive=False)
                        query_results_table = gr.Dataframe(label="Results", interactive=False, wrap=True)
            
            with gr.TabItem("📊 Status"):
                gr.Markdown("### 🔍 System Monitoring")
                gr.Markdown("Check the availability and health of all connected services in real-time.")
                
                with gr.Row():
                    check_status_btn = gr.Button("🔄 Refresh Status", variant="primary", size="lg")
                
                service_status_display = gr.Markdown(value="*Click 'Refresh Status' to check service health*")
                
                with gr.Accordion("📋 Service Details", open=False):
                    gr.Markdown("""
                    | Service | Purpose | Default Port |
                    |---------|---------|--------------|
                    | **Whisper ASR** | Speech-to-Text | Varies |
                    | **XTTS TTS** | Text-to-Speech | 8000 |
                    | **LLM API** | Language Model | Varies |
                    | **PostgreSQL** | Customer Data | 5432 |
                    | **WebSocket** | Voice Pipeline | 8765/8766 |
                    """)
            
            with gr.TabItem("ℹ️ About"):
                gr.Markdown(f"""
                ## 🎙️ AI Voice Agent v5.1.4
                
                **Official XTTS v2 Voices & Retention Logic - Powered by HPE Private Cloud AI**
                
                *Build: {BUILD_NUMBER}*

                This application demonstrates a complete voice-based customer service agent capable of natural conversation, intent recognition, real-time database operations, and intelligent customer retention.
                
                ---
                
                ### 🆕 What's New in v5.1.4
                
                | Feature | Description |
                |---------|-------------|
                | **Official XTTS v2 Voices** | 33 verified speakers (15 female, 18 male) |
                | **Accent-Matched Voices** | Each language gets voices with appropriate accents |
                | **Customer Retention Logic** | Agent convinces customers to stay when they want to leave |
                | **Callback Promise** | Promises a senior representative will call back |
                | **Hebrew Experimental** | Hebrew marked as experimental (not officially supported) |
                | **17-Language Retention** | Retention messages in all supported languages |
                
                ### 🛡️ Reliability Features
                
                | Feature | Description |
                |---------|-------------|
                | **TTS Fallback Chain** | XTTS → Edge-TTS → Text Display |
                | **Aggressive Caching** | 2000 phrases, 10min TTL |
                | **5x Retries** | Auto-retry with exponential backoff |
                | **Health Probes** | Background checks every 30s |
                | **Pre-warming** | Common phrases cached on startup |
                | **Graceful Degradation** | Always shows response, even without voice |
                
                ### 🔄 Pipeline Architecture

                **Pipeline Flow:**
                1. **Voice Input** 🎤 → Whisper ASR
                2. **ASR** → Intent, Sentiment & Churn Analysis
                3. **Analysis** → Retention Check + LLM Intelligence + PostgreSQL DB
                4. **LLM** → XTTS v2 (+ Edge-TTS fallback)
                5. **XTTS** → **Audio Response** 🔊
                
                ### 🎤 Supported Languages (XTTS v2)
                
                | Language | Code | Status |
                |----------|------|--------|
                | English | en | ✅ Full Support |
                | Spanish | es | ✅ Full Support |
                | French | fr | ✅ Full Support |
                | German | de | ✅ Full Support |
                | Italian | it | ✅ Full Support |
                | Portuguese | pt | ✅ Full Support |
                | Polish | pl | ✅ Full Support |
                | Turkish | tr | ✅ Full Support |
                | Russian | ru | ✅ Full Support |
                | Dutch | nl | ✅ Full Support |
                | Czech | cs | ✅ Full Support |
                | Arabic | ar | ✅ Full Support |
                | Chinese | zh-cn | ✅ Full Support |
                | Japanese | ja | ✅ Full Support |
                | Hungarian | hu | ✅ Full Support |
                | Korean | ko | ✅ Full Support |
                | Hindi | hi | ✅ Full Support |
                | **Hebrew** | **he** | ⚠️ **Experimental** |
                
                ### 🛠️ Key Features
                
                #### 1. Natural Conversation
                *   **Multi-Language Input:** Supports speech recognition in 99+ languages (Whisper).
                *   **Natural Output:** High-quality neural text-to-speech (XTTS v2) in 17 languages.
                *   **Context Awareness:** Remembers conversation history and adapts responses.
                
                #### 2. Customer Retention 🛡️
                *   **Churn Detection:** Automatically detects when customer wants to cancel/leave.
                *   **Empathetic Response:** Shows understanding and offers to help.
                *   **Callback Promise:** Promises a senior representative will call back.
                *   **Multi-Language:** Works in all 17 supported languages.
                
                #### 3. Real-World Integration
                *   **Database Actions:** The agent can actually **open, close, and query support tickets** in real-time.
                *   **Authentication:** Secure PIN verification flow for customer identification.
                *   **Sentiment Analysis:** Detects customer frustration and churn risk automatically.
                
                ### 📋 Changelog
                
                **v5.1.4 - Official Voices & Retention**
                *   Official XTTS v2 voices only (33 verified)
                *   Hebrew marked as experimental
                *   Customer retention logic with callback promise
                *   Multi-language retention (17 languages)
                *   Accent-matched voice recommendations
                
                **v5.1.3 - Multi-Language Sentiment**
                *   Expanded sentiment analysis to 17 languages
                *   Improved TTS voice quality (temperature 0.65)
                *   Enhanced text cleaning for better prosody
                
                **v5.1.2 - Upgrade Detection**
                *   Fixed upgrade intent detection (query vs action)
                *   Color-coded latency metrics
                
                **v5.1.0 - Maximum Reliability**
                *   TTS Fallback Chain (XTTS → Edge-TTS → Text)
                *   Expanded cache: 2000 phrases, 10min TTL
                *   Background health probes every 30s

                ### 📋 Quick Start Guide
                
                1.  **Initialize Database:** Go to the **Database** tab and click **"Enable & Initialize"** to create demo data.
                2.  **Start Chatting:** Switch to **Voice Chat**.
                3.  **Identify Yourself:** When asked, say a PIN (e.g., "1234" for John Smith).
                4.  **Try Commands:**
                    *   *"What is my current plan?"*
                    *   *"I have a problem, please open a ticket."*
                    *   *"Close my ticket."*
                    *   *"Upgrade my subscription to Premium."*
                    *   *"I want to cancel my subscription."* (triggers retention)
                
                ### 👥 Demo Accounts
                
                | Customer Name | PIN | Plan |
                |---------------|-----|------|
                | **John Smith** | `1234` | Premium |
                | **Sarah Johnson** | `5678` | Standard |
                | **Michael Chen** | `1111` | Basic |
                
                ---
                
                **Powered by HPE Private Cloud AI**
                """)
                
                gr.Markdown("---")
                gr.Markdown("### 💾 Settings Management")
                gr.Markdown("Your settings (LLM, Database, TTS, etc.) are automatically saved when you change them.")
                
                with gr.Row():
                    save_settings_btn = gr.Button("💾 Save Settings Now", variant="primary", size="sm")
                    clear_settings_btn = gr.Button("🗑️ Clear Saved Settings", variant="secondary", size="sm")
                
                settings_status = gr.Markdown("")
                
                # JavaScript to handle settings buttons
                save_settings_btn.click(
                    fn=lambda: "✅ Settings saved to browser storage!",
                    outputs=[settings_status],
                    js="() => { window.settingsManager.save(); return ''; }"
                )
                
                clear_settings_btn.click(
                    fn=lambda: "🗑️ Settings cleared! Refresh page to see defaults.",
                    outputs=[settings_status],
                    js="() => { window.settingsManager.clear(); return ''; }"
                )
        
        # Main save button handler (on Voice Chat tab)
        main_save_btn.click(
            fn=lambda: "✅ Settings saved!",
            outputs=[main_save_status],
            js="() => { window.settingsManager.save(); return ''; }"
        )
        
        # Event handlers
        def toggle_mode(mode):
            is_ptt = mode == "📍 Push-to-Talk"
            return gr.update(visible=is_ptt), gr.update(visible=not is_ptt)
        
        chat_mode.change(fn=toggle_mode, inputs=[chat_mode], outputs=[ptt_group, conv_group])
        
        def clear_audio():
            return gr.update(value=None), gr.update(value=None)
        
        clear_btn.click(fn=clear_audio, outputs=[input_audio, upload_audio])
        
        def on_persona_change(persona, lang, custom):
            desc = get_persona_description(persona)
            is_custom = persona == "🎯 Custom"
            prompt = build_final_prompt(persona, lang, custom if is_custom else None)
            return prompt, f"*{desc}*", gr.update(visible=is_custom)
        
        agent_persona.change(
            fn=on_persona_change,
            inputs=[agent_persona, tts_language_code, custom_template],
            outputs=[llm_prompt_template, persona_desc, custom_template]
        )
        
        def on_lang_change(lang, persona, custom, current_voice):
            prompt = build_final_prompt(persona, lang, custom if persona == "🎯 Custom" else None)
            display = f"**Selected:** {get_language_display_name(lang)}"
            
            # Update voices based on language - use helper function
            if lang in VOICES_BY_LANG:
                voices = VOICES_BY_LANG[lang]
                new_choices = ["default"] + voices.get("female", []) + voices.get("male", [])
            else:
                new_choices = ALL_XTTS_VOICES
            
            # If current voice is not in new choices, pick the first one from new choices
            # But keep it if it is "default" or valid
            new_value = current_voice
            if current_voice not in new_choices and len(new_choices) > 0:
                new_value = new_choices[1] if len(new_choices) > 1 else new_choices[0]
                
            return prompt, display, gr.update(choices=new_choices, value=new_value)
        
        tts_language_code.change(
            fn=on_lang_change,
            inputs=[tts_language_code, agent_persona, custom_template, tts_voice],
            outputs=[llm_prompt_template, lang_display, tts_voice]
        )
        
        def on_custom_change(custom, persona, lang):
            if persona == "🎯 Custom":
                return build_final_prompt(persona, lang, custom)
            return build_final_prompt(persona, lang, None)
        
        custom_template.change(
            fn=on_custom_change,
            inputs=[custom_template, agent_persona, tts_language_code],
            outputs=[llm_prompt_template]
        )
        
        # Database management buttons
        test_conn_btn.click(
            fn=test_db_connection,
            inputs=[db_host, db_port, db_name, db_user, db_password],
            outputs=[db_status]
        )
        
        get_stats_btn.click(
            fn=get_db_stats,
            inputs=[db_host, db_port, db_name, db_user, db_password],
            outputs=[db_status]
        )
        
        list_dbs_btn.click(
            fn=list_databases,
            inputs=[db_host, db_port, db_admin_user, db_admin_password],
            outputs=[db_status]
        )
        
        create_db_btn.click(
            fn=create_database_if_not_exists,
            inputs=[db_host, db_port, db_name, db_user, db_password, db_admin_user, db_admin_password],
            outputs=[db_status]
        )
        
        init_tables_btn.click(
            fn=init_tables_and_data,
            inputs=[db_host, db_port, db_name, db_user, db_password],
            outputs=[db_status]
        )
        
        drop_db_btn.click(
            fn=drop_database,
            inputs=[db_host, db_port, db_name, db_admin_user, db_admin_password],
            outputs=[db_status]
        )
        
        # Data Viewer event handlers
        db_inputs = [db_host, db_port, db_name, db_user, db_password]
        
        # Refresh buttons
        refresh_customers_btn.click(
            fn=get_customers_data,
            inputs=db_inputs,
            outputs=[customers_table]
        )
        
        refresh_plans_btn.click(
            fn=get_plans_data,
            inputs=db_inputs,
            outputs=[plans_table]
        )
        
        refresh_subs_btn.click(
            fn=get_subscriptions_data,
            inputs=db_inputs,
            outputs=[subs_table]
        )
        
        refresh_inv_btn.click(
            fn=get_invoices_data,
            inputs=db_inputs,
            outputs=[invoices_table]
        )
        
        refresh_tickets_btn.click(
            fn=get_tickets_data,
            inputs=db_inputs,
            outputs=[tickets_table]
        )
        
        refresh_upgrades_btn.click(
            fn=get_upgrade_requests_data,
            inputs=db_inputs,
            outputs=[upgrades_table]
        )
        
        refresh_alerts_btn.click(
            fn=get_sentiment_alerts_data,
            inputs=db_inputs,
            outputs=[alerts_table]
        )
        
        # Customer actions
        add_customer_btn.click(
            fn=add_customer,
            inputs=[db_host, db_port, db_name, db_user, db_password, cust_name_input, cust_email_input, cust_phone_input, cust_pin_input],
            outputs=[customer_action_status]
        )
        
        update_customer_btn.click(
            fn=update_customer,
            inputs=[db_host, db_port, db_name, db_user, db_password, cust_id_input, cust_name_input, cust_email_input, cust_phone_input],
            outputs=[customer_action_status]
        )
        
        delete_customer_btn.click(
            fn=delete_customer,
            inputs=[db_host, db_port, db_name, db_user, db_password, cust_id_input],
            outputs=[customer_action_status]
        )
        
        # Invoice actions
        update_invoice_btn.click(
            fn=update_invoice_status,
            inputs=[db_host, db_port, db_name, db_user, db_password, invoice_id_input, invoice_status_input],
            outputs=[invoice_action_status]
        )
        
        delete_invoice_btn.click(
            fn=delete_invoice,
            inputs=[db_host, db_port, db_name, db_user, db_password, invoice_id_input],
            outputs=[invoice_action_status]
        )
        
        # Ticket actions
        update_ticket_btn.click(
            fn=update_ticket_status,
            inputs=[db_host, db_port, db_name, db_user, db_password, ticket_id_input, ticket_status_input],
            outputs=[ticket_action_status]
        )
        
        delete_ticket_btn.click(
            fn=delete_ticket,
            inputs=[db_host, db_port, db_name, db_user, db_password, ticket_id_input],
            outputs=[ticket_action_status]
        )
        
        # Custom query
        run_query_btn.click(
            fn=run_custom_query,
            inputs=[db_host, db_port, db_name, db_user, db_password, custom_query_input],
            outputs=[query_results_table, query_status]
        )
        
        # Plan actions
        add_plan_btn.click(
            fn=add_plan,
            inputs=[db_host, db_port, db_name, db_user, db_password, 
                    plan_name_input, plan_price_input, plan_billing_input, plan_data_input, plan_support_input],
            outputs=[plan_action_status]
        )
        
        update_plan_btn.click(
            fn=update_plan,
            inputs=[db_host, db_port, db_name, db_user, db_password,
                    plan_id_input, plan_name_input, plan_price_input, plan_billing_input, plan_data_input, plan_support_input],
            outputs=[plan_action_status]
        )
        
        delete_plan_btn.click(
            fn=delete_plan,
            inputs=[db_host, db_port, db_name, db_user, db_password, plan_id_input],
            outputs=[plan_action_status]
        )
        
        # Subscription actions
        add_sub_btn.click(
            fn=add_subscription,
            inputs=[db_host, db_port, db_name, db_user, db_password,
                    sub_customer_input, sub_plan_input, sub_status_input, sub_autorenew_input],
            outputs=[sub_action_status]
        )
        
        update_sub_btn.click(
            fn=update_subscription,
            inputs=[db_host, db_port, db_name, db_user, db_password,
                    sub_id_input, sub_update_status, sub_update_renew],
            outputs=[sub_action_status]
        )
        
        cancel_sub_btn.click(
            fn=cancel_subscription,
            inputs=[db_host, db_port, db_name, db_user, db_password, sub_id_input],
            outputs=[sub_action_status]
        )
        
        # Create invoice
        add_invoice_btn.click(
            fn=add_invoice,
            inputs=[db_host, db_port, db_name, db_user, db_password,
                    inv_customer_input, inv_amount_input, inv_due_date_input, inv_new_status_input],
            outputs=[invoice_action_status]
        )
        
        # Create ticket
        add_ticket_btn.click(
            fn=add_ticket,
            inputs=[db_host, db_port, db_name, db_user, db_password,
                    ticket_customer_input, ticket_subject_input, ticket_desc_input, ticket_priority_input],
            outputs=[ticket_action_status]
        )
        
        # Status monitoring
        check_status_btn.click(
            fn=check_all_services,
            inputs=[asr_server_address, tts_server_address, llm_api_base,
                    db_host, db_port, db_name, db_user, db_password, db_enabled],
            outputs=[service_status_display]
        )
        
        # Audio processing - Uses global cache for customer info persistence
        all_inputs = [
            input_audio, llm_api_base, llm_api_key, llm_model_name, llm_prompt_template,
            asr_server_address, asr_language_code, tts_server_address, tts_language_code, tts_voice,
            db_enabled, db_host, db_port, db_name, db_user, db_password, session_suffix, client_session_id
        ]
        submit_btn.click(fn=process_audio, inputs=all_inputs, outputs=[audio_data_holder, status_log, input_audio, customer_info_display, performance_display, rich_card_display])
        
        upload_inputs = [
            upload_audio, llm_api_base, llm_api_key, llm_model_name, llm_prompt_template,
            asr_server_address, asr_language_code, tts_server_address, tts_language_code, tts_voice,
            db_enabled, db_host, db_port, db_name, db_user, db_password, session_suffix, client_session_id
        ]
        submit_upload_btn.click(fn=process_audio, inputs=upload_inputs, outputs=[audio_data_holder, status_log, upload_audio, customer_info_display, performance_display, rich_card_display])
        
        conv_inputs = [
            conv_audio, llm_api_base, llm_api_key, llm_model_name, llm_prompt_template,
            asr_server_address, asr_language_code, tts_server_address, tts_language_code, tts_voice,
            db_enabled, db_host, db_port, db_name, db_user, db_password, session_suffix, client_session_id
        ]
        conv_audio.change(fn=process_audio, inputs=conv_inputs, outputs=[audio_data_holder, status_log, conv_audio, customer_info_display, performance_display, rich_card_display])
        
        # Reset session button handler - also clears customer info from global cache
        import random
        import string
        def generate_new_session():
            """Reset session and clear customer info"""
            new_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            default_html = '''<div id="customer-info-content" data-customer-id="" style="padding: 24px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-radius: 16px; border: 2px dashed #adb5bd; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
            <div style="color: #495057; font-size: 16px; text-align: center;">
                <div style="font-size: 48px; margin-bottom: 12px; opacity: 0.7;">👤</div>
                <strong style="color: #343a40;">Waiting for customer identification...</strong>
            </div>
        </div>'''
            return new_suffix, default_html
        
        def refresh_customer_info_from_db(customer_id_str, host, port, dbname, user, password, enabled):
            """Refresh customer info from database"""
            if not enabled or not customer_id_str or customer_id_str.strip() == "":
                return gr.update()  # No update needed
            
            try:
                customer_id = int(customer_id_str)
            except (ValueError, TypeError):
                return gr.update()
            
            try:
                import psycopg2
                import psycopg2.extras
                
                conn = psycopg2.connect(
                    host=host, port=port, dbname=dbname, user=user, password=password
                )
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                
                # Get customer basic info
                cursor.execute("SELECT name, email, phone, created_at FROM customers WHERE id = %s", (customer_id,))
                customer = cursor.fetchone()
                
                if not customer:
                    cursor.close()
                    conn.close()
                    return gr.update()
                
                # Get tickets
                cursor.execute("""
                    SELECT id, subject, status, priority FROM support_tickets 
                    WHERE customer_id = %s ORDER BY created_at DESC LIMIT 10
                """, (customer_id,))
                tickets = cursor.fetchall()
                
                # Get invoices
                cursor.execute("""
                    SELECT id, amount, status FROM invoices 
                    WHERE customer_id = %s ORDER BY due_date DESC LIMIT 10
                """, (customer_id,))
                invoices = cursor.fetchall()
                
                # Get subscription
                cursor.execute("""
                    SELECT p.name as plan_name, p.price, s.status
                    FROM subscriptions s JOIN plans p ON s.plan_id = p.id
                    WHERE s.customer_id = %s ORDER BY s.start_date DESC LIMIT 1
                """, (customer_id,))
                subscription = cursor.fetchone()
                
                cursor.close()
                conn.close()
                
                # Calculate stats
                open_tickets = len([t for t in tickets if t['status'] == 'open'])
                overdue_inv = len([i for i in invoices if i['status'] == 'overdue'])
                paid_inv = len([i for i in invoices if i['status'] == 'paid'])
                
                # Build info dict
                info = {
                    'name': customer['name'],
                    'email': customer['email'],
                    'phone': customer.get('phone', 'N/A'),
                    'open_tickets': open_tickets,
                    'overdue_invoices': overdue_inv,
                    'plan_name': subscription['plan_name'] if subscription else '',
                    'plan_price': f"{subscription['price']:.2f}" if subscription else '',
                    'subscription_status': subscription['status'] if subscription else '',
                    'total_tickets': len(tickets),
                    'total_invoices': len(invoices),
                    'paid_invoices': paid_inv,
                    'customer_since': customer['created_at'].strftime('%b %Y') if customer.get('created_at') else '',
                    'recent_tickets': [{'subject': t['subject'], 'status': t['status'], 'priority': t['priority']} for t in tickets[:3]]
                }
                
                return gr.update(value=format_customer_info_html(info))
                
            except Exception as e:
                print(f"[Refresh] Database error: {e}")
                return gr.update()
        
        reset_btn.click(fn=generate_new_session, inputs=[], outputs=[session_suffix, customer_info_display])
        
        # Auto-refresh customer info
        refresh_trigger.click(
            fn=refresh_customer_info_from_db,
            inputs=[customer_id_holder, db_host, db_port, db_name, db_user, db_password, db_enabled],
            outputs=[customer_info_display]
        )
        
        # VAD Script - v4.0.0 with fixed threshold synchronization
        VAD_SCRIPT = """
        () => {
            if (window.vadReady) return;
            window.vadReady = true;
            
            // Generate or retrieve session ID
            if (!window.sessionId) {
                // Changed to sessionStorage to fix silence/cache issues (Issue #5)
                window.sessionId = sessionStorage.getItem('voiceAgentSession');
                if (!window.sessionId) {
                    // Generate UUID-like string
                    window.sessionId = (typeof crypto !== 'undefined' && crypto.randomUUID) ?
                        crypto.randomUUID() :
                        'session_' + Math.random().toString(36).substr(2, 16);
                    sessionStorage.setItem('voiceAgentSession', window.sessionId);
                }
            }
            
            // Sync with hidden Gradio component (try input and textarea)
            setTimeout(() => {
                const sessionInput = document.querySelector('#client-session-id textarea') || document.querySelector('#client-session-id input');
                if (sessionInput) {
                    console.log('[Init] Setting client session ID:', window.sessionId);
                    sessionInput.value = window.sessionId;
                    sessionInput.dispatchEvent(new Event('input', { bubbles: true }));
                } else {
                    console.warn('[Init] Could not find client-session-id component');
                }
            }, 1000); // Wait for Gradio to render

            // Simple state - based on working v0.1.88 code
            // FIX: Initialize thresholds from slider default value (20)
            const defaultThreshold = 20;
            window.vad = { 
                active: false, 
                paused: false, 
                stream: null, 
                ctx: null, 
                processor: null, 
                analyser: null, 
                source: null, 
                recording: false, 
                silenceFrames: 0, 
                consecutiveSpeech: 0, 
                threshold: defaultThreshold,
                thresholdStart: defaultThreshold,
                thresholdStop: Math.max(5, Math.round(defaultThreshold * 0.6)),
                currentAudio: null,
                bargeIn: false,
                minSilenceForSend: 60,
                extendedSilenceWait: 40,
                noiseFloor: 0,
                noiseFloorSamples: []
            };
            
            // Conversation history for context
            window.conversationHistory = [];
            
            // ===============================================
            // FIX v3.1.22: Customer Info Persistence (with polling restore)
            // Saves customer info and restores it when Gradio resets the display
            // ===============================================
            window.customerInfo = {
                savedHtml: null,
                resetInProgress: false,
                lastSaveTime: 0,
                
                isValidCustomer: function(html) {
                    if (!html) return false;
                    if (!html.includes('✅')) return false;
                    const idMatch = html.match(/data-customer-id="(\\d+)"/);
                    return idMatch && idMatch[1] && idMatch[1] !== '';
                },
                
                getContainer: function() {
                    const wrapper = document.getElementById('customer-info-display');
                    if (!wrapper) return null;
                    const content = wrapper.querySelector('#customer-info-content');
                    if (content) return content.parentElement;
                    const innerDiv = wrapper.querySelector('.prose') || wrapper.querySelector('div > div') || wrapper;
                    return innerDiv;
                },
                
                getHtml: function() {
                    const container = this.getContainer();
                    if (!container) return null;
                    const content = container.querySelector('#customer-info-content');
                    if (content) return content.outerHTML;
                    return container.innerHTML;
                },
                
                setHtml: function(html) {
                    const wrapper = document.getElementById('customer-info-display');
                    if (!wrapper) return false;
                    let target = wrapper.querySelector('.prose');
                    if (!target) {
                        const content = wrapper.querySelector('#customer-info-content');
                        if (content) {
                            target = content.parentElement;
                        } else {
                            target = wrapper.querySelector('div') || wrapper;
                        }
                    }
                    if (target) {
                        target.innerHTML = html;
                        return true;
                    }
                    return false;
                },
                
                // SAVE - Always saves valid customer data, even during reset!
                // This is important: new data from server should always be saved
                save: function(html) {
                    if (!this.isValidCustomer(html)) return;
                    
                    // Debounce: don't save more than once per 500ms
                    const now = Date.now();
                    if (now - this.lastSaveTime < 500 && this.savedHtml === html) return;
                    this.lastSaveTime = now;
                    
                    this.savedHtml = html;
                    sessionStorage.setItem('customerInfoHtml', html);
                    
                    // When we save new valid data, reset mode is done
                    if (this.resetInProgress) {
                        this.resetInProgress = false;
                        console.log('[CustomerInfo] Reset cancelled - new customer identified');
                    }
                    
                    const idMatch = html.match(/data-customer-id="(\\d+)"/);
                    if (idMatch && idMatch[1]) {
                        sessionStorage.setItem('customerId', idMatch[1]);
                        if (window.customerRefresh) {
                            window.customerRefresh.setCustomerId(idMatch[1]);
                        }
                    }
                    console.log('[CustomerInfo] Saved');
                },
                
                // RESTORE - Only restores if NOT in reset mode
                restore: function() {
                    if (this.resetInProgress) return null;  // Block restore during reset
                    if (!this.savedHtml) {
                        this.savedHtml = sessionStorage.getItem('customerInfoHtml');
                    }
                    return this.savedHtml;
                },
                
                clear: function() {
                    console.log('[CustomerInfo] Clearing...');
                    this.resetInProgress = true;
                    this.savedHtml = null;
                    sessionStorage.removeItem('customerInfoHtml');
                    sessionStorage.removeItem('customerId');
                    
                    if (window.customerRefresh) {
                        window.customerRefresh.stop();
                    }
                    
                    const waitingHtml = '<div id="customer-info-content" data-customer-id="" style="padding: 24px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-radius: 16px; border: 2px dashed #adb5bd;"><div style="color: #495057; font-size: 16px; text-align: center;"><div style="font-size: 48px; margin-bottom: 12px; opacity: 0.7;">👤</div><strong style="color: #343a40;">Waiting for customer identification...</strong></div></div>';
                    this.setHtml(waitingHtml);
                    
                    const holder = document.querySelector('#customer-id-holder textarea, #customer-id-holder input');
                    if (holder) {
                        holder.value = '';
                        holder.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    
                    console.log('[CustomerInfo] Cleared - waiting for new customer');
                    
                    // Reset mode expires after 5 seconds (safety net)
                    setTimeout(() => {
                        if (this.resetInProgress) {
                            this.resetInProgress = false;
                            console.log('[CustomerInfo] Reset mode expired');
                        }
                    }, 5000);
                },
                
                // CHECK - Runs every 200ms
                // Always checks for new data to save, only restores when not in reset mode
                check: function() {
                    const currentHtml = this.getHtml();
                    
                    // If current display has valid customer data, ALWAYS save it
                    if (this.isValidCustomer(currentHtml)) {
                        if (!this.savedHtml || this.savedHtml !== currentHtml) {
                            this.save(currentHtml);
                        }
                    }
                    // If display is empty/waiting AND we're not in reset mode, restore from cache
                    else if (!this.resetInProgress) {
                        if (!currentHtml || currentHtml.includes('Waiting for customer') || !currentHtml.includes('✅')) {
                            const saved = this.restore();
                            if (saved && this.isValidCustomer(saved)) {
                                if (this.setHtml(saved)) {
                                    console.log('[CustomerInfo] Restored from cache');
                                }
                            }
                        }
                    }
                }
            };
            
            // Restore from session storage on load - actually set the HTML!
            setTimeout(() => {
                const saved = window.customerInfo.restore();
                if (saved && window.customerInfo.isValidCustomer(saved)) {
                    window.customerInfo.setHtml(saved);
                    console.log('[CustomerInfo] Restored on page load');
                }
                
                // Start polling after restore attempt
                setInterval(() => window.customerInfo.check(), 200);
                console.log('[CustomerInfo] Polling started (200ms)');
            }, 800);
            
            // Listen for reset button - clear() handles everything
            setTimeout(() => {
                const resetBtn = document.getElementById('reset-session-btn');
                if (resetBtn) {
                    resetBtn.addEventListener('click', () => {
                        console.log('[CustomerInfo] Reset button clicked');
                        window.customerInfo.clear();
                    });
                    console.log('[CustomerInfo] Reset button listener attached');
                }
            }, 1000);
            
            console.log('[CustomerInfo] Initialized v5.0.0 (polling restore + auto-refresh)');
            // ===============================================
            
            // ===============================================
            // Customer Info Auto-Refresh (every 5 seconds via Gradio)
            // ===============================================
            window.customerRefresh = {
                intervalId: null,
                customerId: null,
                
                setCustomerId: function(id) {
                    this.customerId = id;
                    // Also update the hidden textbox
                    const holder = document.querySelector('#customer-id-holder textarea, #customer-id-holder input');
                    if (holder) {
                        holder.value = id || '';
                        holder.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    console.log('[CustomerInfo] Customer ID set:', id);
                },
                
                refresh: function() {
                    // Only refresh if a customer is identified
                    if (!this.customerId) return;
                    
                    // Click the hidden refresh button
                    const refreshBtn = document.querySelector('#refresh-trigger-btn');
                    if (refreshBtn) {
                        refreshBtn.click();
                        console.log('[CustomerInfo] Triggered refresh');
                    }
                },
                
                start: function() {
                    if (this.intervalId) return; // Already running
                    this.intervalId = setInterval(() => this.refresh(), 5000); // Every 5 seconds
                    console.log('[CustomerInfo] Auto-refresh started (5s interval)');
                },
                
                stop: function() {
                    if (this.intervalId) {
                        clearInterval(this.intervalId);
                        this.intervalId = null;
                    }
                    this.customerId = null;
                    console.log('[CustomerInfo] Auto-refresh stopped');
                }
            };
            
            // Start auto-refresh after page loads
            setTimeout(() => {
                // Restore customer_id from session storage if available
                const savedId = sessionStorage.getItem('customerId');
                if (savedId) {
                    window.customerRefresh.setCustomerId(savedId);
                }
                window.customerRefresh.start();
            }, 3000);
            // ===============================================
            
            // ===============================================
            // Customer Actions - Quick actions from customer panel
            // ===============================================
            window.customerActions = {
                // Close the most recent open ticket for customer
                closeTicket: function(customerId) {
                    if (!customerId) {
                        alert('No customer selected');
                        return;
                    }
                    if (confirm('Close the most recent open ticket for this customer?')) {
                        // Navigate to Data Viewer -> Tickets tab
                        const ticketsTab = document.querySelector('[data-testid="🎫 Tickets-tab-button"]') || 
                                          document.querySelector('button[aria-label*="Tickets"]');
                        if (ticketsTab) {
                            ticketsTab.click();
                            setTimeout(() => {
                                alert('Please select the ticket to close and change its status to "resolved"');
                            }, 300);
                        } else {
                            alert('Go to Data Viewer → Tickets tab to close tickets');
                        }
                    }
                },
                
                // Update ticket status/info
                updateTicket: function(customerId) {
                    if (!customerId) {
                        alert('No customer selected');
                        return;
                    }
                    // Navigate to Data Viewer -> Tickets tab
                    const dataViewerTab = document.querySelector('[data-testid="📊 Data Viewer-tab-button"]') ||
                                         document.querySelector('button[aria-label*="Data Viewer"]');
                    if (dataViewerTab) {
                        dataViewerTab.click();
                        setTimeout(() => {
                            const ticketsTab = document.querySelector('[data-testid="🎫 Tickets-tab-button"]');
                            if (ticketsTab) ticketsTab.click();
                        }, 200);
                    }
                    setTimeout(() => {
                        alert('In the Tickets tab, enter ticket ID and new status, then click Update');
                    }, 500);
                },
                
                // Create new ticket for customer
                createTicket: function(customerId) {
                    if (!customerId) {
                        alert('No customer selected');
                        return;
                    }
                    // Navigate to Data Viewer -> Tickets tab
                    const dataViewerTab = document.querySelector('[data-testid="📊 Data Viewer-tab-button"]');
                    if (dataViewerTab) {
                        dataViewerTab.click();
                        setTimeout(() => {
                            const ticketsTab = document.querySelector('[data-testid="🎫 Tickets-tab-button"]');
                            if (ticketsTab) ticketsTab.click();
                            setTimeout(() => {
                                // Pre-fill customer ID if possible
                                const custInput = document.querySelector('#ticket_customer_input input, input[placeholder*="Customer ID"]');
                                if (custInput) {
                                    custInput.value = customerId;
                                    custInput.dispatchEvent(new Event('input', { bubbles: true }));
                                }
                            }, 200);
                        }, 200);
                    }
                    setTimeout(() => {
                        alert('Fill in the ticket details and click Add to create a new ticket');
                    }, 600);
                },
                
                // View full customer history
                viewHistory: function(customerId) {
                    if (!customerId) {
                        alert('No customer selected');
                        return;
                    }
                    // Navigate to Data Viewer
                    const dataViewerTab = document.querySelector('[data-testid="📊 Data Viewer-tab-button"]');
                    if (dataViewerTab) {
                        dataViewerTab.click();
                        setTimeout(() => {
                            // Refresh all data
                            document.querySelectorAll('button').forEach(btn => {
                                if (btn.textContent.includes('🔄 Refresh')) {
                                    btn.click();
                                }
                            });
                        }, 300);
                    }
                    setTimeout(() => {
                        alert('View customer data across Tickets, Invoices, and Subscriptions tabs. Filter by Customer ID: ' + customerId);
                    }, 600);
                }
            };
            console.log('[CustomerActions] Initialized');
            // ===============================================

            // ===============================================
            // Metrics Persistence - HTML Version
            // ===============================================
            window.metricsPersistence = {
                savedHtml: null,
                
                getContainer: function() {
                    const wrapper = document.getElementById('performance-display');
                    if (!wrapper) return null;
                    // For gr.HTML, the content might be directly inside or nested
                    // We target the inner 'metrics-content' div if possible
                    const inner = wrapper.querySelector('#metrics-content');
                    if (inner) return inner.parentElement;
                    return wrapper.querySelector('.prose') || wrapper.querySelector('div') || wrapper;
                },
                
                getHtml: function() {
                    const container = this.getContainer();
                    if (!container) return null;
                    return container.innerHTML;
                },
                
                setHtml: function(html) {
                    const container = this.getContainer();
                    if (container) {
                        container.innerHTML = html;
                        return true;
                    }
                    return false;
                },
                
                check: function() {
                    const currentHtml = this.getHtml();
                    
                    // If current has valid data (look for our specific ID or text), save it
                    if (currentHtml && currentHtml.includes('Latency Report') && currentHtml.includes('metrics-content')) {
                        // Avoid saving during transient states or if essentially same
                        if (this.savedHtml !== currentHtml) {
                            this.savedHtml = currentHtml;
                            sessionStorage.setItem('metricsHtml', currentHtml);
                            console.log('[Metrics] Saved new HTML data');
                        }
                    } 
                    // If current is empty/placeholder but we have saved data, restore it
                    else if ((!currentHtml || currentHtml.trim() === '' || !currentHtml.includes('metrics-content')) && this.savedHtml) {
                        if (this.savedHtml.includes('metrics-content')) {
                            if (this.setHtml(this.savedHtml)) {
                                console.log('[Metrics] Restored HTML from cache');
                            }
                        }
                    }
                },
                
                init: function() {
                    // Restore immediately if possible
                    this.savedHtml = sessionStorage.getItem('metricsHtml');
                    if (this.savedHtml) {
                        // Wait slightly for DOM
                        setTimeout(() => this.check(), 500);
                    }
                    // Start polling
                    setInterval(() => this.check(), 200);
                    console.log('[Metrics] Persistence initialized');
                }
            };
            
            // Initialize metrics persistence
            setTimeout(() => window.metricsPersistence.init(), 1000);
            // ===============================================
            
            // ===============================================
            // Settings Persistence - Save/Load from localStorage
            // ===============================================
            window.settingsManager = {
                STORAGE_KEY: 'voiceAgentSettings',
                
                // Field IDs to persist
                fields: [
                    'llm_api_base', 'llm_api_key', 'llm_model', 'agent_persona',
                    'db_host', 'db_port', 'db_name', 'db_user', 'db_password',
                    'tts_server_address', 'tts_language_code',
                    'asr_server_address', 'asr_language_code',
                    'sensitivity-slider'
                ],
                
                save: function() {
                    const settings = {};
                    this.fields.forEach(id => {
                        // Try different selector patterns for Gradio components
                        let el = document.querySelector('#' + id + ' input, #' + id + ' textarea, #' + id + ' select');
                        if (!el) el = document.getElementById(id);
                        if (el) {
                            settings[id] = el.value;
                        }
                    });
                    // Also save checkbox states
                    const dbEnabled = document.querySelector('#db_enabled input[type="checkbox"]');
                    if (dbEnabled) settings['db_enabled'] = dbEnabled.checked;
                    
                    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(settings));
                    console.log('[Settings] Saved to localStorage');
                    return settings;
                },
                
                load: function() {
                    const saved = localStorage.getItem(this.STORAGE_KEY);
                    if (!saved) {
                        console.log('[Settings] No saved settings found');
                        return null;
                    }
                    
                    try {
                        const settings = JSON.parse(saved);
                        console.log('[Settings] Loading saved settings...');
                        
                        Object.keys(settings).forEach(id => {
                            if (id === 'db_enabled') {
                                const checkbox = document.querySelector('#db_enabled input[type="checkbox"]');
                                if (checkbox) checkbox.checked = settings[id];
                                return;
                            }
                            
                            // Try different selector patterns
                            let el = document.querySelector('#' + id + ' input, #' + id + ' textarea');
                            if (!el) el = document.getElementById(id);
                            
                            if (el && settings[id]) {
                                el.value = settings[id];
                                // Trigger input event for Gradio to detect change
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                            }
                        });
                        
                        console.log('[Settings] Restored from localStorage');
                        return settings;
                    } catch (e) {
                        console.error('[Settings] Failed to load:', e);
                        return null;
                    }
                },
                
                clear: function() {
                    localStorage.removeItem(this.STORAGE_KEY);
                    console.log('[Settings] Cleared');
                },
                
                // Auto-save when any tracked field changes
                bindAutoSave: function() {
                    const self = this;
                    this.fields.forEach(id => {
                        let el = document.querySelector('#' + id + ' input, #' + id + ' textarea, #' + id + ' select');
                        if (!el) el = document.getElementById(id);
                        if (el) {
                            el.addEventListener('change', () => self.save());
                            el.addEventListener('blur', () => self.save());
                        }
                    });
                    // Checkbox
                    const dbEnabled = document.querySelector('#db_enabled input[type="checkbox"]');
                    if (dbEnabled) {
                        dbEnabled.addEventListener('change', () => self.save());
                    }
                    console.log('[Settings] Auto-save bound to fields');
                }
            };
            
            // Load saved settings after page loads
            setTimeout(() => {
                window.settingsManager.load();
                // Bind auto-save after a delay to ensure elements exist
                setTimeout(() => window.settingsManager.bindAutoSave(), 2000);
            }, 1500);
            // ===============================================
            
            const slider = document.getElementById('sensitivity-slider');
            const sensValue = document.getElementById('sens-value');
            const thresholdMarker = document.getElementById('threshold-marker');
            
            // Calculate marker position: (value - min) / (max - min) * 100%
            // Slider range: 5-50, so position = ((value - 5) / 45) * 100
            function updateThresholdMarker(value) {
                const position = ((value - 5) / 45) * 100;
                if (thresholdMarker) thresholdMarker.style.left = position + '%';
                if (sensValue) sensValue.textContent = value;
            }
            
            // FIX: Synchronize thresholds from slider value
            function syncThresholdsFromSlider() {
                const slider = document.getElementById('sensitivity-slider');
                if (slider) {
                    const val = parseInt(slider.value);
                    window.vad.threshold = val;
                    window.vad.thresholdStart = val;
                    window.vad.thresholdStop = Math.max(5, Math.round(val * 0.6));
                    console.log('[VAD] Synced thresholds: start=' + val + ', stop=' + window.vad.thresholdStop);
                }
            }
            
            // Use polling to bind slider if not immediately available
            function bindSlider() {
                const slider = document.getElementById('sensitivity-slider');
                if (slider) {
                    // Initialize with slider's current value
                    const currentVal = parseInt(slider.value);
                    window.vad.threshold = currentVal;
                    window.vad.thresholdStart = currentVal;
                    window.vad.thresholdStop = Math.max(5, Math.round(currentVal * 0.6));
                    updateThresholdMarker(currentVal);
                    
                    slider.oninput = function() { 
                        const val = parseInt(this.value);
                        window.vad.threshold = val;
                        // Hysteresis: stop threshold is 40% lower (but minimum 5)
                        window.vad.thresholdStart = val;
                        window.vad.thresholdStop = Math.max(5, Math.round(val * 0.6));
                        updateThresholdMarker(val);
                        console.log('[VAD] Threshold changed: start=' + val + ', stop=' + window.vad.thresholdStop);
                    };
                    console.log('[VAD] Slider bound successfully with threshold=' + currentVal);
                } else {
                    setTimeout(bindSlider, 500);
                }
            }
            bindSlider();
            
            // Reset session function
            window.resetSession = function() {
                window.sessionId = 'session_' + Math.random().toString(36).substr(2, 16);
                sessionStorage.setItem('voiceAgentSession', window.sessionId);
                console.log('Session reset to: ' + window.sessionId);
                return window.sessionId;
            };
            
            // Calibrate noise threshold based on current noise floor
            window.calibrateNoise = function() {
                if (!window.vad.active) {
                    alert('Please start the microphone first, then click Auto to calibrate.');
                    return;
                }
                
                const noiseFloor = window.vad.noiseFloor;
                if (noiseFloor <= 0) {
                    alert('Please wait a moment for noise detection, then try again.');
                    return;
                }
                
                // Set threshold to noise floor + good margin
                // Formula: noise * 2 + 8 gives comfortable buffer
                // noise=2 -> threshold=12, noise=5 -> threshold=18, noise=10 -> threshold=28
                const newThreshold = Math.min(50, Math.max(8, Math.round(noiseFloor * 2 + 8)));
                
                window.vad.threshold = newThreshold;
                window.vad.thresholdStart = newThreshold;
                window.vad.thresholdStop = Math.max(5, Math.round(newThreshold * 0.6));
                
                // Update slider value AND trigger visual update
                const slider = document.getElementById('sensitivity-slider');
                if (slider) {
                    slider.value = newThreshold;
                    // Trigger input event to update marker
                    slider.dispatchEvent(new Event('input'));
                }
                
                // Update marker position directly as backup
                const position = ((newThreshold - 5) / 45) * 100;
                const marker = document.getElementById('threshold-marker');
                if (marker) marker.style.left = position + '%';
                
                const sensValue = document.getElementById('sens-value');
                if (sensValue) sensValue.textContent = newThreshold;
                
                const status = document.getElementById('conv-status');
                if (status) status.textContent = '✅ Calibrated to ' + newThreshold + ' (noise floor: ' + noiseFloor + ')';
                
                console.log('[VAD] Calibrated: noise=' + noiseFloor + ', threshold=' + newThreshold);
                
                // Flash the marker to show it moved
                if (marker) {
                    marker.style.background = '#00ff00';
                    setTimeout(() => { marker.style.background = 'yellow'; }, 500);
                }
            };
            
            // Simple audio playback detection - based on working code
            function setupAudioPlaybackDetection() {
                const observer = new MutationObserver((mutations) => { 
                    mutations.forEach((m) => { 
                        m.addedNodes.forEach((n) => { 
                            if (n.nodeName === 'AUDIO' || (n.querySelector && n.querySelector('audio'))) { 
                                const a = n.nodeName === 'AUDIO' ? n : n.querySelector('audio'); 
                                if (a) setupAudioListeners(a); 
                            } 
                        }); 
                    }); 
                });
                observer.observe(document.body, { childList: true, subtree: true });
                document.querySelectorAll('audio').forEach(setupAudioListeners);
            }
            
            // Audio listeners with barge-in support
            function setupAudioListeners(audio) { 
                if (audio.dataset.vadListening) return; 
                audio.dataset.vadListening = 'true';
                window.vad.currentAudio = audio;  // Track current audio
                
                // Add error handler for Gradio audio issues
                audio.addEventListener('error', (e) => {
                    console.log('[Audio] Gradio audio error:', e);
                    // Try to recover by reloading the audio
                    if (audio.src) {
                        console.log('[Audio] Attempting to replay...');
                        setTimeout(() => {
                            audio.load();
                            audio.play().catch(err => console.log('[Audio] Replay failed:', err));
                        }, 100);
                    }
                });
                
                audio.addEventListener('play', () => { 
                    console.log('[Audio] Playing...');
                    if (window.vad.active) { 
                        window.vad.paused = true; 
                        window.vad.recording = false;
                        window.vad.silenceFrames = 0;
                        window.vad.consecutiveSpeech = 0;
                        window.vad.currentAudio = audio;
                        const status = document.getElementById('conv-status');
                        if (status) status.textContent = '🔊 Playing... (speak to interrupt)'; 
                    } 
                }); 
                
                audio.addEventListener('ended', () => { 
                    console.log('[Audio] Ended');
                    setTimeout(() => { 
                        if (window.vad.active) { 
                            window.vad.paused = false;
                            window.vad.currentAudio = null;
                            const status = document.getElementById('conv-status');
                            if (status) status.textContent = '🎤 Listening...'; 
                        } 
                    }, 500);  // Reduced delay for faster response
                }); 
                
                audio.addEventListener('pause', () => { 
                    // Only resume if it was a natural end, not a barge-in
                    if (!window.vad.bargeIn) {
                        setTimeout(() => { 
                            if (window.vad.active && audio.ended) { 
                                window.vad.paused = false;
                                window.vad.currentAudio = null;
                                const status = document.getElementById('conv-status');
                                if (status) status.textContent = '🎤 Listening...'; 
                            } 
                        }, 500);
                    }
                    window.vad.bargeIn = false;  // Reset barge-in flag
                }); 
            }
            
            // Custom audio player fallback using Web Audio API
            window.playAudioData = async function(audioData) {
                try {
                    const ctx = new (window.AudioContext || window.webkitAudioContext)();
                    
                    // Setup visualizer if we have a canvas
                    const canvas = document.getElementById('agent-visualizer');
                    let analyser = null;
                    if (canvas) {
                        analyser = ctx.createAnalyser();
                        analyser.fftSize = 256;
                    }
                    
                    const arrayBuffer = audioData instanceof ArrayBuffer ? audioData : await audioData.arrayBuffer();
                    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
                    const source = ctx.createBufferSource();
                    source.buffer = audioBuffer;
                    
                    if (analyser) {
                        source.connect(analyser);
                        analyser.connect(ctx.destination);
                        
                        // Start animation
                        const drawAgent = () => {
                            if (!window.vad.paused) return; // Stop if not playing (paused state implies agent speaking in VAD logic)
                            
                            const width = canvas.width;
                            const height = canvas.height;
                            const canvasCtx = canvas.getContext('2d');
                            const bufferLength = analyser.frequencyBinCount;
                            const dataArray = new Uint8Array(bufferLength);
                            
                            analyser.getByteFrequencyData(dataArray);
                            
                            canvasCtx.clearRect(0, 0, width, height);
                            
                            const barWidth = (width / bufferLength) * 2.5;
                            let barHeight;
                            let x = 0;
                            
                            for(let i = 0; i < bufferLength; i++) {
                                barHeight = dataArray[i] / 2; // Scale down
                                
                                // Dynamic color based on height
                                const r = barHeight + 25 * (i/bufferLength);
                                const g = 250 * (i/bufferLength);
                                const b = 50;
                                
                                canvasCtx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
                                canvasCtx.fillRect(x, height - barHeight, barWidth, barHeight);
                                
                                x += barWidth + 1;
                            }
                            requestAnimationFrame(drawAgent);
                        };
                        drawAgent();
                    } else {
                        source.connect(ctx.destination);
                    }
                    
                    // Pause VAD during playback
                    if (window.vad.active) {
                        window.vad.paused = true;
                        const status = document.getElementById('conv-status');
                        if (status) status.textContent = '🔊 Playing...';
                    }
                    
                    source.onended = () => {
                        setTimeout(() => {
                            if (window.vad.active) {
                                window.vad.paused = false;
                                const status = document.getElementById('conv-status');
                                if (status) status.textContent = '🎤 Listening...';
                            }
                        }, 500);
                    };
                    
                    source.start(0);
                    console.log('[Audio] Playing via Web Audio API');
                } catch (err) {
                    console.error('[Audio] Web Audio API playback failed:', err);
                }
            };
            
            // Monitor for Gradio audio component updates and force playback if needed
            function monitorAudioComponent() {
                const outputContainer = document.getElementById('output-audio');
                if (!outputContainer) {
                    setTimeout(monitorAudioComponent, 1000);
                    return;
                }
                
                const observer = new MutationObserver((mutations) => {
                    mutations.forEach((m) => {
                        // Look for audio elements that might not be playing
                        const audioEls = outputContainer.querySelectorAll('audio');
                        audioEls.forEach(audio => {
                            if (audio.src && audio.paused && audio.readyState >= 2) {
                                console.log('[Audio] Detected paused audio with src, attempting play...');
                                audio.play().catch(err => {
                                    console.log('[Audio] Auto-play blocked:', err);
                                });
                            }
                        });
                    });
                });
                
                observer.observe(outputContainer, { childList: true, subtree: true, attributes: true });
                console.log('[Audio] Monitoring output-audio component');
            }
            
            // Monitor audio-data-holder for base64 audio and play it
            function monitorAudioDataHolder() {
                const audioPlayer = document.getElementById('custom-audio-player');
                const audioStatus = document.getElementById('audio-status');
                
                if (!audioPlayer) {
                    setTimeout(monitorAudioDataHolder, 1000);
                    return;
                }
                
                // Setup visualizer for the HTML5 audio element
                let agentCtx, agentAnalyser, agentSource;
                
                function setupAgentVisualizer() {
                    const canvas = document.getElementById('agent-visualizer');
                    if (!canvas || agentSource) return; // Already setup or no canvas
                    
                    try {
                        if (!agentCtx) agentCtx = new (window.AudioContext || window.webkitAudioContext)();
                        agentAnalyser = agentCtx.createAnalyser();
                        agentAnalyser.fftSize = 128;
                        
                        // Create MediaElementSource
                        // Note: This requires crossorigin="anonymous" on audio tag if loading from external URL
                        agentSource = agentCtx.createMediaElementSource(audioPlayer);
                        agentSource.connect(agentAnalyser);
                        agentAnalyser.connect(agentCtx.destination);
                        
                        console.log('[Visualizer] Agent visualizer connected');
                    } catch(e) {
                        console.error('[Visualizer] Setup failed:', e);
                    }
                }
                
                let visualizerPhase = 0;

                function drawAgentVisualizer() {
                    const canvas = document.getElementById('agent-visualizer');
                    if (!canvas || !agentAnalyser || audioPlayer.paused) {
                        if (audioPlayer.paused && canvas) {
                             const ctx = canvas.getContext('2d');
                             ctx.clearRect(0, 0, canvas.width, canvas.height);
                             // Draw idle line
                             ctx.beginPath();
                             const y = canvas.height / 2;
                             ctx.moveTo(0, y);
                             ctx.lineTo(canvas.width, y);
                             ctx.strokeStyle = 'rgba(148, 163, 184, 0.2)'; // Slate 400 very transparent
                             ctx.lineWidth = 2;
                             ctx.stroke();
                        }
                        return;
                    }
                    
                    const ctx = canvas.getContext('2d');
                    const width = canvas.width;
                    const height = canvas.height;
                    const centerY = height / 2;
                    
                    // Use Time Domain for smooth waveforms
                    const bufferLength = agentAnalyser.frequencyBinCount;
                    const dataArray = new Uint8Array(bufferLength);
                    agentAnalyser.getByteTimeDomainData(dataArray); 
                    
                    // Calculate Amplitude (RMS)
                    let sum = 0;
                    for(let i = 0; i < bufferLength; i++) {
                        const v = (dataArray[i] - 128) / 128.0;
                        sum += v * v;
                    }
                    const amplitude = Math.min(1.0, Math.sqrt(sum / bufferLength) * 3.5); // Boosted
                    
                    ctx.clearRect(0, 0, width, height);
                    
                    // Update animation phase
                    visualizerPhase += 0.15;
                    
                    // IMPROVED Multi-wave configuration (Smoother Siri-style)
                    const waves = [
                        { color: 'rgba(59, 130, 246, 0.9)', freq: 0.012, speed: 1.5, amp: 1.0 },  // Blue
                        { color: 'rgba(139, 92, 246, 0.9)', freq: 0.018, speed: 2.0, amp: 0.8 },   // Violet
                        { color: 'rgba(14, 165, 233, 0.8)', freq: 0.008, speed: 1.0, amp: 1.1 },   // Cyan
                        { color: 'rgba(236, 72, 153, 0.7)', freq: 0.022, speed: 0.8, amp: 0.6 }    // Pink
                    ];
                    
                    ctx.globalCompositeOperation = 'screen';
                    ctx.shadowBlur = 20;
                    ctx.lineCap = 'round';
                    ctx.lineJoin = 'round';
                    
                    waves.forEach((wave, i) => {
                        ctx.beginPath();
                        ctx.strokeStyle = wave.color;
                        ctx.shadowColor = wave.color;
                        ctx.lineWidth = 4; // Thicker lines for impact
                        
                        for (let x = 0; x < width; x++) {
                            // Normalized position (-1 to 1) for windowing
                            const normX = (x / width) * 2 - 1;
                            // Blackman-Harris window for very smooth tapering at edges
                            // Or simple cosine window^2
                            const window = Math.pow(Math.cos(normX * Math.PI / 2), 2);
                            
                            // Multi-frequency modulation for organic feel
                            const mod = Math.sin(x * 0.05 + visualizerPhase * 0.5);
                            
                            const y = centerY + 
                                      Math.sin(x * wave.freq + visualizerPhase * wave.speed + i) * 
                                      (amplitude * height * 0.45 * wave.amp) * 
                                      (1 + mod * 0.1) * // slight modulation
                                      window;
                            
                            if (x === 0) ctx.moveTo(x, y);
                            else ctx.lineTo(x, y);
                        }
                        ctx.stroke();
                    });
                    
                    // Add subtle glow center orb
                    if (amplitude > 0.05) {
                        const gradient = ctx.createRadialGradient(width/2, height/2, 0, width/2, height/2, 100);
                        gradient.addColorStop(0, `rgba(139, 92, 246, ${amplitude * 0.2})`);
                        gradient.addColorStop(1, 'rgba(0,0,0,0)');
                        ctx.fillStyle = gradient;
                        ctx.fillRect(0, 0, width, height);
                    }
                    
                    ctx.globalCompositeOperation = 'source-over';
                    ctx.shadowBlur = 0;
                    
                    requestAnimationFrame(drawAgentVisualizer);
                }
                
                // Set up audio player events
                audioPlayer.addEventListener('play', () => {
                    console.log('[Audio] Playing...');
                    
                    // Initialize visualizer context on first play (user interaction required for AudioContext)
                    setupAgentVisualizer();
                    if (agentCtx && agentCtx.state === 'suspended') {
                        agentCtx.resume();
                    }
                    drawAgentVisualizer();
                    
                    if (audioStatus) audioStatus.textContent = '🔊 Playing...';
                    if (window.vad && window.vad.active) {
                        window.vad.paused = true;
                        window.vad.recording = false;
                        window.vad.currentAudio = audioPlayer;
                        const status = document.getElementById('conv-status');
                        if (status) status.textContent = '🔊 Playing... (speak to interrupt)';
                    }
                });
                
                audioPlayer.addEventListener('ended', () => {
                    console.log('[Audio] Ended');
                    if (audioStatus) audioStatus.textContent = 'Ready for next response';
                    setTimeout(() => {
                        if (window.vad && window.vad.active) {
                            window.vad.paused = false;
                            window.vad.currentAudio = null;
                            const status = document.getElementById('conv-status');
                            if (status) status.textContent = '🎤 Listening...';
                        }
                    }, 500);
                });
                
                // Retry logic for audio errors
                let retryCount = 0;

                audioPlayer.addEventListener('error', (e) => {
                    console.error('[Audio] Error:', e);
                    if (audioStatus) audioStatus.textContent = '❌ Audio error (Retrying...)';

                    // Attempt retry twice
                    if (retryCount < 2 && audioPlayer.src) {
                        retryCount++;
                        console.log('[Audio] Retrying playback (' + retryCount + '/2)...');
                        // Add cache buster
                        const cleanSrc = audioPlayer.src.split('&retry=')[0];
                        const separator = cleanSrc.includes('?') ? '&' : '?';
                        audioPlayer.src = cleanSrc + separator + 'retry=' + Date.now();
                        setTimeout(() => {
                            audioPlayer.load();
                            audioPlayer.play().catch(err => console.error('Retry play failed', err));
                        }, 500);
                    } else {
                        if (audioStatus) audioStatus.textContent = '❌ Audio error (Failed)';
                    }
                });
                
                // Find the hidden textbox with audio data
                const checkForAudioData = () => {
                    // Find the audio-data-holder textarea
                    const holder = document.querySelector('#audio-data-holder textarea, [id*="audio-data-holder"] textarea');
                    
                    if (holder && holder.value && holder.value.length > 5) {
                        let audioUrl = holder.value.trim();
                        
                        // v5.1.0: Support both base64 data URLs and file paths
                        if (audioUrl.startsWith('data:audio')) {
                            // Base64 data URL - use directly
                            console.log('[Audio] Base64 data URL detected, length:', audioUrl.length);
                        } else if (!audioUrl.startsWith('http') && !audioUrl.startsWith('/file=')) {
                            // File path - add /file= prefix
                            audioUrl = '/file=' + audioUrl;
                        }
                        
                        // Compare with the raw value from the textbox to detect changes
                        if (audioPlayer.dataset.lastRawValue !== holder.value) {
                            console.log('[Audio] New audio detected!');
                            console.log('[Audio] Type:', audioUrl.startsWith('data:') ? 'base64' : 'file');
                            retryCount = 0; // Reset retry count for new file
                            
                            audioPlayer.dataset.lastRawValue = holder.value;
                            
                            // For base64, use as-is. For files, add timestamp
                            if (audioUrl.startsWith('data:')) {
                                audioPlayer.src = audioUrl;
                            } else {
                                audioPlayer.src = audioUrl + (audioUrl.includes('?') ? '&' : '?') + 't=' + Date.now();
                            }
                            
                            // FORCE LOAD AND PLAY - v5.1.0 aggressive approach
                            console.log('[Audio] Loading audio...');
                            if (audioStatus) audioStatus.textContent = '⏳ Loading audio...';
                            
                            audioPlayer.load();
                            
                            // Immediate play attempt
                            const tryPlay = () => {
                                audioPlayer.muted = false;
                                audioPlayer.volume = 1.0;
                                
                                const playPromise = audioPlayer.play();
                                if (playPromise !== undefined) {
                                    playPromise.then(_ => {
                                        console.log('[Audio] ✅ Playback started!');
                                        if (audioStatus) audioStatus.textContent = '🔊 Playing...';
                                    }).catch(error => {
                                        console.error('[Audio] Play blocked:', error.name);
                                        if (audioStatus) audioStatus.textContent = '⚠️ Click page to enable audio';
                                        
                                        // Add click listener to enable audio
                                        const enableAudio = () => {
                                            audioPlayer.play().then(() => {
                                                console.log('[Audio] ✅ Enabled by click');
                                                if (audioStatus) audioStatus.textContent = '🔊 Playing...';
                                            }).catch(e => console.error('[Audio] Still blocked:', e));
                                            document.removeEventListener('click', enableAudio);
                                        };
                                        document.addEventListener('click', enableAudio, { once: true });
                                    });
                                }
                            };
                            
                            // Try playing immediately and after small delay
                            tryPlay();
                            setTimeout(tryPlay, 100);
                            setTimeout(tryPlay, 500);
                        }
                    }
                };
                
                // Check periodically for new audio (faster polling)
                setInterval(checkForAudioData, 250);
                
                // Also observe for changes
                const observer = new MutationObserver((mutations) => {
                    checkForAudioData();
                });
                
                const container = document.querySelector('[id*="audio-data-holder"]');
                if (container) {
                    observer.observe(container, { childList: true, subtree: true, characterData: true, attributes: true });
                }
                
                console.log('[Audio] Monitoring audio-data-holder');
            }
            
            // Start monitoring after page load
            setTimeout(monitorAudioComponent, 2000);
            setTimeout(monitorAudioDataHolder, 1000);
            
            // Barge-in: Stop AI audio when user starts speaking
            function handleBargeIn() {
                if (window.vad.currentAudio && !window.vad.currentAudio.paused) {
                    console.log('[VAD] Barge-in detected - stopping AI audio');
                    window.vad.bargeIn = true;
                    window.vad.currentAudio.pause();
                    window.vad.currentAudio.currentTime = 0;
                    window.vad.paused = false;
                    window.vad.currentAudio = null;
                    document.getElementById('conv-status').textContent = '🔴 Recording...';
                }
            }
            
            window.startVAD = async function() {
                const status = document.getElementById('conv-status'), debug = document.getElementById('conv-debug'), bar = document.getElementById('level-bar');
                if (!status || window.vad.active) return;
                
                // FIX: Synchronize thresholds from slider before starting
                syncThresholdsFromSlider();
                
                try {
                    status.textContent = '🎤 Requesting mic...';
                    const stream = await navigator.mediaDevices.getUserMedia({ 
                        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } 
                    });
                    window.vad.stream = stream; 
                    window.vad.active = true; 
                    window.vad.paused = false;
                    
                    const ctx = new (window.AudioContext || window.webkitAudioContext)(); 
                    window.vad.ctx = ctx;
                    const source = ctx.createMediaStreamSource(stream); 
                    window.vad.source = source;
                    
                    const analyser = ctx.createAnalyser(); 
                    analyser.fftSize = 512; 
                    analyser.smoothingTimeConstant = 0.3;
                    source.connect(analyser); 
                    window.vad.analyser = analyser;
                    
                    const dataArray = new Uint8Array(analyser.frequencyBinCount);
                    const processor = ctx.createScriptProcessor(4096, 1, 1); 
                    window.vad.processor = processor; 
                    let pcmChunks = [];
                    
                    processor.onaudioprocess = (e) => { 
                        if (window.vad.recording && !window.vad.paused) { 
                            const input = e.inputBuffer.getChannelData(0); 
                            const pcm = new Int16Array(input.length); 
                            for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, input[i] * 32768)); 
                            pcmChunks.push(pcm); 
                        } 
                    };
                    source.connect(processor); 
                    processor.connect(ctx.destination);
                    
                    // Visualizer Function
                    function drawVisualizer() {
                        if (!window.vad.active) return;
                        
                        const canvas = document.getElementById('audio-visualizer');
                        if (!canvas) {
                            requestAnimationFrame(drawVisualizer);
                            return;
                        }
                        
                        const ctx = canvas.getContext('2d');
                        const width = canvas.width;
                        const height = canvas.height;
                        
                        // Use time domain data for waveform
                        const bufferLength = window.vad.analyser.frequencyBinCount;
                        const dataArrayTime = new Uint8Array(bufferLength);
                        window.vad.analyser.getByteTimeDomainData(dataArrayTime);
                        
                        ctx.clearRect(0, 0, width, height);
                        
                        // Draw Gradient Background for Waveform
                        let gradient = ctx.createLinearGradient(0, 0, 0, height);
                        
                        // Check if we are recording or paused
                        let primaryColor = '#06b6d4'; // Cyan/Teal (Listening)
                        
                        if (window.vad.recording) {
                            primaryColor = '#ef4444'; // Red (Recording)
                            gradient.addColorStop(0, 'rgba(239, 68, 68, 0.6)');
                            gradient.addColorStop(1, 'rgba(239, 68, 68, 0.05)');
                        } else if (window.vad.paused) {
                            primaryColor = '#8b5cf6'; // Violet (Agent Speaking)
                            gradient.addColorStop(0, 'rgba(139, 92, 246, 0.6)');
                            gradient.addColorStop(1, 'rgba(139, 92, 246, 0.05)');
                        } else {
                            // Listening state - Tech Cyan
                            gradient.addColorStop(0, 'rgba(6, 182, 212, 0.6)');
                            gradient.addColorStop(1, 'rgba(6, 182, 212, 0.05)');
                        }
                        
                        // Draw Waveform
                        ctx.lineWidth = 3;
                        ctx.strokeStyle = primaryColor;
                        ctx.lineCap = 'round';
                        
                        // Add glow effect
                        ctx.shadowBlur = 8;
                        ctx.shadowColor = primaryColor;
                        
                        ctx.beginPath();
                        
                        const sliceWidth = width * 1.0 / bufferLength;
                        let x = 0;
                        
                        // Start point (center left)
                        ctx.moveTo(0, height / 2);
                        
                        for(let i = 0; i < bufferLength; i++) {
                            const v = dataArrayTime[i] / 128.0;
                            const y = v * height / 2;
                            
                            // Use Quadratic Bezier for smoother curve
                            if (i > 0) {
                                const prevX = (i - 1) * sliceWidth;
                                const prevV = dataArrayTime[i-1] / 128.0;
                                const prevY = prevV * height / 2;
                                const avgX = (prevX + x) / 2;
                                const avgY = (prevY + y) / 2;
                                ctx.quadraticCurveTo(prevX, prevY, avgX, avgY);
                            }
                            
                            x += sliceWidth;
                        }
                        
                        ctx.lineTo(width, height / 2);
                        ctx.stroke();
                        
                        // Reset shadow for fill
                        ctx.shadowBlur = 0;
                        
                        // Fill area under the curve
                        ctx.lineTo(width, height);
                        ctx.lineTo(0, height);
                        ctx.closePath();
                        ctx.fillStyle = gradient;
                        ctx.fill();
                        
                        // Draw Threshold Indicator
                        // Calculate Y position based on threshold (slider 5-100)
                        // Canvas amplitude is from 0 to height/2 (center) to height
                        
                        // Scale threshold: 5-100 map to distance from center
                        // Threshold 20 means trigger at 20% of max amplitude
                        const center = height / 2;
                        const thresholdPercent = window.vad.threshold / 100;
                        const thresholdPx = thresholdPercent * (height / 2);
                        
                        const topY = center - thresholdPx;
                        const bottomY = center + thresholdPx;
                        
                        // Draw Threshold Lines
                        ctx.lineWidth = 1;
                        ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)'; // Subtle white
                        ctx.setLineDash([4, 4]);
                        
                        // Top line
                        ctx.beginPath();
                        ctx.moveTo(0, topY);
                        ctx.lineTo(width, topY);
                        ctx.stroke();
                        
                        // Bottom line
                        ctx.beginPath();
                        ctx.moveTo(0, bottomY);
                        ctx.lineTo(width, bottomY);
                        ctx.stroke();
                        ctx.setLineDash([]);
                        
                        // Draw Label
                        ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
                        ctx.font = '10px sans-serif';
                        ctx.fillText('SENSITIVITY', 5, topY - 5);
                        
                        requestAnimationFrame(drawVisualizer);
                    }
                    
                    // Start visualizer
                    drawVisualizer();
                    
                    function checkAudio() {
                        if (!window.vad.active) return;
                        
                        analyser.getByteFrequencyData(dataArray);
                        let sum = 0; 
                        for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
                        const avgLevel = Math.round(sum / dataArray.length);
                        
                        // Track noise floor (background noise level)
                        if (!window.vad.recording && !window.vad.paused) {
                            window.vad.noiseFloorSamples.push(avgLevel);
                            if (window.vad.noiseFloorSamples.length > 30) {
                                window.vad.noiseFloorSamples.shift();
                            }
                            // Noise floor is the average of recent quiet samples
                            if (window.vad.noiseFloorSamples.length >= 10) {
                                const sorted = [...window.vad.noiseFloorSamples].sort((a,b) => a-b);
                                // Use 30th percentile as noise floor (ignore spikes)
                                window.vad.noiseFloor = sorted[Math.floor(sorted.length * 0.3)] || 0;
                            }
                        }
                        
                        // Use hysteresis: different thresholds for start vs stop
                        const startThreshold = window.vad.thresholdStart;
                        const stopThreshold = window.vad.thresholdStop;
                        
                        // Determine if this is speech based on current state
                        const isSpeechStart = avgLevel > startThreshold;  // Higher threshold to START
                        const isSpeechContinue = avgLevel > stopThreshold;  // Lower threshold to CONTINUE
                        const isSpeech = window.vad.recording ? isSpeechContinue : isSpeechStart;
                        
                        // Barge-in detection: if user speaks loudly while audio is playing
                        // IMPROVED: Harder to interrupt (higher threshold + longer duration)
                        // Also respects a 1.5-second "grace period" after playback starts to prevent echo triggers
                        const isGracePeriod = window.vad.currentAudio && (window.vad.currentAudio.currentTime < 1.5);
                        
                        // Dynamic barge-in threshold:
                        // 1. Must be significantly louder than background (threshold * 3.0)
                        // 2. Must be sustained for longer (15 frames = ~0.35s)
                        // 3. Absolute minimum volume requirement (40) to avoid quiet echo triggering it
                        const bargeInThreshold = Math.max(40, startThreshold * 3.0);
                        
                        if (window.vad.paused && !isGracePeriod && avgLevel > bargeInThreshold) {
                            window.vad.consecutiveSpeech++;
                            // Require ~350ms of continuous loud speech to interrupt
                            if (window.vad.consecutiveSpeech >= 15) { 
                                console.log('[VAD] Barge-in triggered: Level ' + avgLevel + ' > ' + bargeInThreshold);
                                handleBargeIn();
                            }
                            requestAnimationFrame(checkAudio);
                            return;
                        }
                        
                        if (window.vad.paused) { requestAnimationFrame(checkAudio); return; }
                        
                        // FIX: Show more detailed debug info including threshold comparison
                        const noiseInfo = window.vad.noiseFloor > 0 ? ' | Noise: ' + window.vad.noiseFloor : '';
                        const speechIndicator = isSpeech ? ' [SPEECH]' : '';
                        if (debug) debug.textContent = 'Level: ' + avgLevel + ' | Threshold: ' + startThreshold + '/' + stopThreshold + noiseInfo + speechIndicator + ' | Session: ' + (window.sessionId || 'none').substr(-8);
                        
                        // FIX: Update bar to show level relative to threshold
                        if (bar) { 
                            // Align bar scale with slider scale (5-50 range)
                            const barPercent = Math.max(0, Math.min(100, ((avgLevel - 5) / 45) * 100));
                            bar.style.width = barPercent + '%'; 
                            // Color indicates: green = below threshold, red = above threshold (recording)
                            bar.style.background = isSpeech ? 'linear-gradient(90deg, #ef4444, #f87171)' : 'linear-gradient(90deg, #22c55e, #4ade80)'; 
                        }
                        
                        if (isSpeech) { 
                            window.vad.consecutiveSpeech++; 
                            window.vad.silenceFrames = 0; 
                            // Start recording after 4 consecutive speech frames
                            if (!window.vad.recording && window.vad.consecutiveSpeech >= 4) { 
                                window.vad.recording = true; 
                                pcmChunks = []; 
                                status.textContent = '🔴 Recording...'; 
                            } 
                        }
                        else { 
                            window.vad.consecutiveSpeech = 0; 
                            if (window.vad.recording) { 
                                window.vad.silenceFrames++; 
                                
                                // Dynamic silence threshold based on recording length
                                const totalLen = pcmChunks.reduce((a, c) => a + c.length, 0);
                                const recordingDuration = totalLen / ctx.sampleRate;  // seconds
                                
                                // INCREASED PATIENCE: Wait longer before stopping
                                // Short recordings (< 1.5s): wait longer for more speech
                                // Long recordings (> 3s): send faster
                                let silenceThreshold = 90;  // default frames (~1.5s)
                                if (recordingDuration < 1.5) {
                                    silenceThreshold = 120;  // wait longer for short recordings (~2s)
                                } else if (recordingDuration > 3.0) {
                                    silenceThreshold = 60;  // send faster for long recordings (~1s)
                                }
                                
                                if (window.vad.silenceFrames >= silenceThreshold) { 
                                    window.vad.recording = false; 
                                    
                                    // Minimum ~0.5 seconds of audio (22050 samples at 44.1kHz)
                                    const anyAudioPlaying = Array.from(document.querySelectorAll('audio')).some(a => !a.paused && !a.ended);
                                    if (totalLen >= 10000 && !window.vad.paused && !anyAudioPlaying) { 
                                        status.textContent = '📤 Sending...'; 
                                        const wavBuf = new Int16Array(totalLen); 
                                        let off = 0; 
                                        for (const c of pcmChunks) { wavBuf.set(c, off); off += c.length; } 
                                        const wavBlob = createWAV(wavBuf, ctx.sampleRate); 
                                        // Use starts-with selector for better compatibility
                                        const inp = document.querySelector('[id^="conv-audio-input"] input[type="file"]'); 
                                        if (inp) { 
                                            const file = new File([wavBlob], 'rec.wav', { type: 'audio/wav' }); 
                                            const dt = new DataTransfer(); 
                                            dt.items.add(file); 
                                            inp.files = dt.files; 
                                            inp.dispatchEvent(new Event('change', { bubbles: true })); 
                                        } 
                                    } else if (totalLen < 10000) {
                                        console.log('[VAD] Recording too short, discarding');
                                    } else if (window.vad.paused || anyAudioPlaying) {
                                        console.log('[VAD] Discarding recording - audio is playing');
                                    }
                                    pcmChunks = []; 
                                    setTimeout(() => { 
                                        if (window.vad.active && !window.vad.recording && !window.vad.paused) 
                                            status.textContent = '🎤 Listening...'; 
                                    }, 300); 
                                } 
                            } else { 
                                status.textContent = '🎤 Listening...'; 
                            } 
                        }
                        requestAnimationFrame(checkAudio);
                    }
                    
                    setupAudioPlaybackDetection(); 
                    status.textContent = '🎤 Listening...'; 
                    checkAudio();
                } catch (err) { 
                    status.textContent = '❌ ' + err.message; 
                    window.vad.active = false; 
                }
            };
            
            window.stopVAD = function() { 
                window.vad.active = false; 
                if (window.vad.processor) {
                    try { window.vad.processor.disconnect(); } catch(e) {}
                }
                if (window.vad.source) {
                    try { window.vad.source.disconnect(); } catch(e) {}
                }
                if (window.vad.stream) {
                    window.vad.stream.getTracks().forEach(t => t.stop());
                }
                if (window.vad.ctx && window.vad.ctx.state !== 'closed') {
                    window.vad.ctx.close().catch(e => console.log('AudioContext close error:', e));
                }
                const bar = document.getElementById('level-bar'), status = document.getElementById('conv-status'); 
                if (bar) bar.style.width = '0%'; 
                if (status) status.textContent = '⏹️ Stopped'; 
            };
            
            function createWAV(pcm, sampleRate) { 
                const buffer = new ArrayBuffer(44 + pcm.length * 2); 
                const view = new DataView(buffer); 
                const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); }; 
                writeStr(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); writeStr(8, 'WAVE'); writeStr(12, 'fmt '); 
                view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); 
                view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); 
                view.setUint16(32, 2, true); view.setUint16(34, 16, true); writeStr(36, 'data'); 
                view.setUint32(40, pcm.length * 2, true); 
                for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true); 
                return new Blob([buffer], { type: 'audio/wav' }); 
            }
        }
        """
        demo.load(fn=None, inputs=None, outputs=None, js=VAD_SCRIPT)
    
    return demo

if __name__ == "__main__":
    print(f"Starting Voice Agent v5.1.4 (Build {BUILD_NUMBER}) - Official XTTS v2 Voices, Customer Retention, 17 Languages...")
    demo = create_ui()
    demo.queue()
    demo.launch(server_name="0.0.0.0", server_port=8080, show_error=True)