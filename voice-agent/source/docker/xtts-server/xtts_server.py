#!/usr/bin/env python3
"""
XTTS v2 TTS Server
==================
High-quality multilingual TTS with voice cloning
Supports: EN, ES, FR, DE, IT, PT, PL, TR, RU, NL, CS, AR, ZH, JA, HU, KO, HI

GPU optimized for L40S
v4.1.0 - Fixed audio format (returns proper WAV from synthesize)
"""

import os
# Accept Coqui TOS before importing TTS
os.environ["COQUI_TOS_AGREED"] = "1"

# Set model cache directory (before importing TTS)
MODEL_CACHE_DIR = os.getenv("TTS_HOME", os.getenv("XDG_CACHE_HOME", "/model-cache"))
if MODEL_CACHE_DIR and os.path.isdir(MODEL_CACHE_DIR):
    os.environ["TTS_HOME"] = MODEL_CACHE_DIR
    os.makedirs(os.path.join(MODEL_CACHE_DIR, "tts"), exist_ok=True)

import io
import logging
import time
import torch
import numpy as np
from pathlib import Path
from typing import Optional, List
from functools import lru_cache

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import scipy.io.wavfile as wavfile

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("XTTS-Server")

# =============================================================================
# CONFIGURATION
# =============================================================================
SAMPLE_RATE = 24000
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "en")
DEFAULT_SPEAKER = os.getenv("DEFAULT_SPEAKER", "Ana Florence")
SPEAKERS_DIR = Path(os.getenv("SPEAKERS_DIR", "/app/speakers"))

# Supported languages (official XTTS v2 languages only)
SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish", 
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "tr": "Turkish",
    "ru": "Russian",
    "nl": "Dutch",
    "cs": "Czech",
    "ar": "Arabic",
    "zh-cn": "Chinese",
    "ja": "Japanese",
    "hu": "Hungarian",
    "ko": "Korean",
}

# =============================================================================
# MODEL MANAGER
# =============================================================================
class XTTSManager:
    """Manages XTTS v2 model with GPU support"""
    
    def __init__(self):
        self.model = None
        self.device = None
        self.speakers = {}
        self._initialized = False
    
    def initialize(self):
        """Initialize the model"""
        if self._initialized:
            return
        
        from TTS.api import TTS
        
        # Check model cache
        cache_dir = os.environ.get("TTS_HOME", "~/.local/share/tts")
        cache_path = Path(cache_dir).expanduser()
        model_path = cache_path / "tts_models--multilingual--multi-dataset--xtts_v2"
        
        if model_path.exists():
            logger.info(f"✅ Using cached model from: {model_path}")
        else:
            logger.info(f"📥 Model will be downloaded to: {cache_path}")
        
        # Determine device
        if torch.cuda.is_available():
            self.device = "cuda"
            gpu_name = torch.cuda.get_device_name(0)
            gpu_mem = torch.cuda.get_device_properties(0).total_memory / 1e9
            logger.info(f"🚀 Using GPU: {gpu_name} ({gpu_mem:.1f}GB)")
        else:
            self.device = "cpu"
            logger.warning("⚠️ GPU not available, using CPU (will be slow)")
        
        # Load model
        logger.info("Loading XTTS v2 model...")
        start = time.time()
        self.model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)
        elapsed = time.time() - start
        logger.info(f"✅ Model loaded in {elapsed:.1f}s")
        
        # Load custom speaker files
        self._load_speakers()
        
        self._initialized = True
    
    def _load_speakers(self):
        """Load speaker reference audio files"""
        SPEAKERS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Built-in speakers from model
        try:
            # Get built-in speaker names
            if hasattr(self.model, 'synthesizer') and self.model.synthesizer:
                if hasattr(self.model.synthesizer.tts_model, 'speaker_manager'):
                    sm = self.model.synthesizer.tts_model.speaker_manager
                    if sm and hasattr(sm, 'name_to_id'):
                        # Log the type of name_to_id for debugging
                        logger.info(f"Speaker manager name_to_id type: {type(sm.name_to_id)}")
                        
                        # Handle both dict and list/keys view
                        speaker_names = []
                        if isinstance(sm.name_to_id, dict):
                            speaker_names = list(sm.name_to_id.keys())
                        else:
                            speaker_names = list(sm.name_to_id)
                            
                        for name in speaker_names:
                            self.speakers[name] = {"type": "builtin", "name": name}
                        logger.info(f"Loaded {len(self.speakers)} built-in speakers: {list(self.speakers.keys())[:5]}...")
                    else:
                        logger.warning("Speaker manager missing name_to_id")
                else:
                    logger.warning("TTS model missing speaker_manager")
        except Exception as e:
            logger.warning(f"Could not load built-in speakers: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        
        # Custom speaker files
        for wav_file in SPEAKERS_DIR.glob("*.wav"):
            name = wav_file.stem
            self.speakers[name] = {"type": "file", "path": str(wav_file)}
            logger.info(f"Loaded custom speaker: {name}")
    
    def get_speakers(self) -> List[str]:
        """Get list of available speakers"""
        return list(self.speakers.keys())
    
    def synthesize(
        self,
        text: str,
        language: str = "en",
        speaker: str = None,
        speaker_wav: bytes = None,
    ) -> bytes:
        """Synthesize speech"""
        if not self._initialized:
            self.initialize()
        
        start = time.time()
        
        # Validate language
        lang_code = language.lower()
        if lang_code not in SUPPORTED_LANGUAGES:
            logger.warning(f"Unsupported language: {language}, falling back to English")
            lang_code = "en"
        
        # Determine speaker wav
        speaker_wav_path = None
        use_speaker_name = speaker  # The speaker name to pass to model.tts
        
        if speaker_wav:
            # Use provided audio as reference
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(speaker_wav)
                speaker_wav_path = f.name
                use_speaker_name = None  # Will use wav file instead
        elif speaker:
            # FIX v1.3.0: Try to use the speaker even if not in our cached list
            # The model itself knows about built-in speakers
            if speaker in self.speakers:
                speaker_info = self.speakers[speaker]
                if speaker_info["type"] == "file":
                    speaker_wav_path = speaker_info["path"]
                    use_speaker_name = None  # Will use wav file
                    logger.info(f"Using custom speaker file: {speaker}")
                else:
                    # Built-in speaker from our cached list
                    use_speaker_name = speaker
                    logger.info(f"Using known built-in speaker: {speaker}")
            elif speaker != "default":
                # Speaker not in our cache, but might still work with the model
                use_speaker_name = speaker
                logger.info(f"Trying speaker '{speaker}' (not in cache, may be built-in)")
            else:
                # "default" - use our default
                use_speaker_name = DEFAULT_SPEAKER
                logger.info(f"Using default speaker: {DEFAULT_SPEAKER}")
        else:
            # No speaker specified - use default
            default_file = SPEAKERS_DIR / f"{DEFAULT_SPEAKER}.wav"
            if default_file.exists():
                speaker_wav_path = str(default_file)
                use_speaker_name = None
                logger.info(f"Using default speaker file: {DEFAULT_SPEAKER}")
            else:
                use_speaker_name = DEFAULT_SPEAKER
                logger.info(f"Using default speaker name: {DEFAULT_SPEAKER}")
        
        # Generate audio
        logger.info(f"Synthesizing: lang={lang_code}, speaker_name={use_speaker_name}, speaker_wav={speaker_wav_path is not None}, text=\"{text[:50]}...\"")
        
        try:
            if speaker_wav_path:
                wav = self.model.tts(
                    text=text,
                    speaker_wav=speaker_wav_path,
                    language=lang_code,
                )
            else:
                # Use speaker name
                wav = self.model.tts(
                    text=text,
                    speaker=use_speaker_name,
                    language=lang_code,
                )
            
            # Convert to WAV bytes (with header)
            wav_array = np.array(wav)
            wav_array = (wav_array * 32767).astype(np.int16)
            
            # Create WAV file in memory
            wav_buffer = io.BytesIO()
            wavfile.write(wav_buffer, SAMPLE_RATE, wav_array)
            audio_bytes = wav_buffer.getvalue()
            
            elapsed = time.time() - start
            logger.info(f"Generated {len(audio_bytes):,} bytes WAV in {elapsed:.2f}s")
            
            return audio_bytes
            
        finally:
            # Cleanup temp file
            if speaker_wav and 'speaker_wav_path' in locals():
                try:
                    os.unlink(speaker_wav_path)
                except:
                    pass

# Global model manager
xtts_manager = XTTSManager()

# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI(
    title="XTTS v2 Server",
    description="High-quality multilingual TTS with voice cloning",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class TTSRequest(BaseModel):
    text: str
    language: str = "en"
    speaker: Optional[str] = None

class TTSResponse(BaseModel):
    success: bool
    message: str

@app.on_event("startup")
async def startup():
    """Initialize model on startup"""
    logger.info("=" * 60)
    logger.info("XTTS v2 Server v4.1.0 - Optimized for PVC caching")
    logger.info("=" * 60)
    
    # Initialize model (will load on first request if not preloaded)
    preload = os.getenv("PRELOAD_MODEL", "true").lower() == "true"
    if preload:
        xtts_manager.initialize()
    
    logger.info("Server ready!")

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": "4.1.0",
        "model_loaded": xtts_manager._initialized,
        "device": xtts_manager.device or "not initialized",
        "gpu_available": torch.cuda.is_available(),
    }

@app.get("/info")
async def info():
    """Get server information"""
    return {
        "version": "4.1.0",
        "model": "XTTS v2",
        "sample_rate": SAMPLE_RATE,
        "languages": SUPPORTED_LANGUAGES,
        "speakers": xtts_manager.get_speakers() if xtts_manager._initialized else [],
        "default_language": DEFAULT_LANGUAGE,
        "default_speaker": DEFAULT_SPEAKER,
        "device": xtts_manager.device,
    }

@app.get("/languages")
async def languages():
    """Get supported languages"""
    return SUPPORTED_LANGUAGES

@app.get("/speakers")
async def speakers():
    """Get available speakers"""
    if not xtts_manager._initialized:
        xtts_manager.initialize()
    return {"speakers": xtts_manager.get_speakers()}

@app.get("/speakers/extended")
async def speakers_extended():
    """Get speakers with extended info (for UI)"""
    if not xtts_manager._initialized:
        xtts_manager.initialize()
    
    speakers = xtts_manager.get_speakers()
    
    # Categorize known speakers by gender (based on XTTS v2 built-in speakers)
    female_patterns = [
        "Claribel", "Daisy", "Gracie", "Tammie", "Alison", "Ana", "Annmarie", 
        "Asya", "Brenda", "Gitta", "Henriette", "Sofia", "Tammy", "Tanja", 
        "Vjollca", "Nova", "Maja", "Uta", "Lidiya", "Chandra", "Szofi", 
        "Camilla", "Lilya", "Zofija", "Narelle", "Barbora", "Alexandra", 
        "Alma", "Rosemary", "Ige", "Roziya"
    ]
    
    result = {
        "total": len(speakers),
        "female": [s for s in speakers if any(p in s for p in female_patterns)],
        "male": [s for s in speakers if not any(p in s for p in female_patterns) and s != "default"],
        "custom": [s for s in speakers if s not in [sp.get("name") for sp in xtts_manager.speakers.values() if sp.get("type") == "builtin"]],
        "all": speakers
    }
    return result

@app.post("/tts")
async def synthesize(request: TTSRequest):
    """Synthesize speech from text - returns raw PCM (16-bit signed, 24kHz)"""
    if not request.text or not request.text.strip():
        raise HTTPException(400, "Empty text")
    
    try:
        # Get WAV data from synthesizer
        wav_bytes = xtts_manager.synthesize(
            text=request.text,
            language=request.language,
            speaker=request.speaker,
        )
        
        # Strip WAV header (44 bytes) to return raw PCM
        # WAV header is always 44 bytes for simple PCM format
        if len(wav_bytes) > 44 and wav_bytes[:4] == b'RIFF':
            pcm_data = wav_bytes[44:]
        else:
            # Already PCM or unexpected format
            pcm_data = wav_bytes
        
        from starlette.responses import Response as StarletteResponse
        response = StarletteResponse(
            content=pcm_data,
            media_type="audio/pcm",
        )
        response.headers["X-Sample-Rate"] = str(SAMPLE_RATE)
        response.headers["X-Channels"] = "1"
        response.headers["X-Sample-Width"] = "2"
        response.headers["X-Language"] = request.language
        response.headers["X-Speaker"] = request.speaker or DEFAULT_SPEAKER
        response.headers["Content-Length"] = str(len(pcm_data))
        return response
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"TTS Error: {e}")
        raise HTTPException(500, str(e))


@app.post("/tts/wav")
async def synthesize_wav(request: TTSRequest):
    """Synthesize speech from text - returns WAV file"""
    if not request.text or not request.text.strip():
        raise HTTPException(400, "Empty text")
    
    try:
        audio_bytes = xtts_manager.synthesize(
            text=request.text,
            language=request.language,
            speaker=request.speaker,
        )
        
        return Response(
            content=audio_bytes,
            media_type="audio/wav",
            headers={
                "X-Sample-Rate": str(SAMPLE_RATE),
                "X-Language": request.language,
                "X-Speaker": request.speaker or DEFAULT_SPEAKER,
            }
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"TTS Error: {e}")
        raise HTTPException(500, str(e))

@app.post("/tts/clone")
async def synthesize_with_clone(
    text: str = Form(...),
    language: str = Form("en"),
    speaker_audio: UploadFile = File(...),
):
    """Synthesize speech with voice cloning from uploaded audio"""
    if not text or not text.strip():
        raise HTTPException(400, "Empty text")
    
    try:
        # Read uploaded audio
        audio_data = await speaker_audio.read()
        
        audio_bytes = xtts_manager.synthesize(
            text=text,
            language=language,
            speaker_wav=audio_data,
        )
        
        return Response(
            content=audio_bytes,
            media_type="audio/wav",
            headers={
                "X-Sample-Rate": str(SAMPLE_RATE),
                "X-Language": language,
                "X-Voice-Cloned": "true",
            }
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"TTS Clone Error: {e}")
        raise HTTPException(500, str(e))

@app.post("/speakers/upload")
async def upload_speaker(
    name: str = Form(...),
    audio: UploadFile = File(...),
):
    """Upload a new speaker reference audio"""
    if not name or not name.strip():
        raise HTTPException(400, "Speaker name required")
    
    # Sanitize name
    safe_name = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        raise HTTPException(400, "Invalid speaker name")
    
    try:
        # Save audio file
        speaker_path = SPEAKERS_DIR / f"{safe_name}.wav"
        audio_data = await audio.read()
        
        with open(speaker_path, "wb") as f:
            f.write(audio_data)
        
        # Register speaker
        xtts_manager.speakers[safe_name] = {"type": "file", "path": str(speaker_path)}
        
        logger.info(f"Uploaded new speaker: {safe_name}")
        
        return {"success": True, "speaker": safe_name}
    except Exception as e:
        logger.error(f"Upload Error: {e}")
        raise HTTPException(500, str(e))

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)