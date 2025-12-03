from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    model: Optional[str] = None  # Allow user to specify model
    documents: Optional[List[Dict[str, str]]] = []  # List of {filename, content_base64}

class ChatResponse(BaseModel):
    response: str
    context_used: bool = False
    debug_info: Optional[Dict[str, Any]] = None
