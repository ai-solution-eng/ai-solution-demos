# ─────────────────────────────────────────────────────────────
# rag_pipeline_with_scraper.py
# ─────────────────────────────────────────────────────────────

import logging
logger = logging.getLogger(__name__)

class Pipeline:
    """
    This is the class Open-WebUI will instantiate.
    It must define at least `pipe(self, user_message, …)`.
    """
    def __init__(self):
        logger.info("✅ OpenWebUIPipeline initialized")

    def pipe(self, user_message, model_id=None, messages=None, body=None):
        # For quick test
        return f"Pipeline working! You said: {user_message}"
