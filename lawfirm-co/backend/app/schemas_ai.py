from pydantic import BaseModel
from typing import List, Optional

class Personae(BaseModel):
    name: str
    role: str
    category: str  # e.g., "Main Party", "Key Witness", "Peripheral"
    description: Optional[str] = None

class DebugStep(BaseModel):
    step_name: str
    used_model: str
    prompt_sent: str
    raw_response: str
    content_snippet: str
    error: Optional[str] = None

class DramatisPersonaeResponse(BaseModel):
    personae: List[Personae]
    debug_steps: List[DebugStep] = []
