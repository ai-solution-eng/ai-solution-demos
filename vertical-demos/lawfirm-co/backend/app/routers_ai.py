import json
import re
import io
import base64
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import httpx
from . import database, models, models_settings, schemas_ai, schemas
from .core import get_logger
from .utils import get_llm_config

logger = get_logger(__name__)

# Try to import reportlab, if fails, we'll handle it
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

router = APIRouter(
    prefix="/ai",
    tags=["ai"],
)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def call_llm(endpoint, api_key, messages, model="gpt-oss-20b"):
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Normalize endpoint
        base_url = endpoint.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url.rstrip('/v1')
        
        response = await client.post(
            f"{base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.0
            }
        )
        if response.status_code != 200:
            raise Exception(f"LLM Error ({response.status_code}): {response.text}")
        
        data = response.json()
        if "choices" not in data or not data["choices"]:
            raise Exception("Empty response from LLM")
            
        return data["choices"][0]["message"]["content"]

async def get_available_models(llm_endpoint: str, api_key: str):
    """Query LLM endpoint for available models"""
    try:
        # Normalize endpoint
        base_url = llm_endpoint.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url.rstrip('/v1')
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/v1/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                # Extract model IDs from response
                if "data" in result:
                    return [model["id"] for model in result["data"]]
                return []
            return []
    except Exception as e:
        logger.error(f"Error fetching models: {e}")
        return []

@router.post("/cases/{case_id}/dramatis-personae", response_model=schemas_ai.DramatisPersonaeResponse)
async def generate_dramatis_personae(case_id: int, db: Session = Depends(get_db)):
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    llm_endpoint, api_key = get_llm_config(db)
    if not llm_endpoint:
        raise HTTPException(status_code=500, detail="LLM not configured")

    # Determine model to use
    available_models = await get_available_models(llm_endpoint, api_key)
    model_to_use = available_models[0] if available_models else "gpt-oss-20b"
    logger.info(f"Using model: {model_to_use}")

    # 1. Extract from each document
    extracted_people = []
    debug_steps = []
    
    # Also include case description/evidence in a "virtual" document
    case_summary = f"Title: {case.title}\nDescription: {case.description}\nDefendant: {case.defendant_name}"
    for ev in case.evidence:
        case_summary += f"\nEvidence: {ev.description} ({ev.evidence_type})"
    
    docs_to_process = [{"title": "Case Summary", "content": case_summary}]
    
    from . import rag_memory
    
    for doc in case.documents:
        if doc.content:
            # Try to extract text based on file extension
            try:
                content_to_process = doc.content
                
                # Handle Data URIs if present
                if content_to_process.startswith("data:"):
                    header, encoded = content_to_process.split(",", 1)
                    content_bytes = base64.b64decode(encoded)
                else:
                    # Assume raw content string
                    content_bytes = content_to_process.encode('utf-8')
                
                extracted_text, success = rag_memory.extract_text_from_content(content_bytes, doc.title)
                
                if success and extracted_text:
                    docs_to_process.append({"title": doc.title, "content": extracted_text})
                else:
                    # Fallback to raw content if extraction fails or returns empty
                    docs_to_process.append({"title": doc.title, "content": doc.content})
            except Exception as e:
                logger.error(f"Error extracting text from {doc.title}: {e}")
                docs_to_process.append({"title": doc.title, "content": doc.content})

    logger.info(f"Processing {len(docs_to_process)} documents for Dramatis Personae...")

    for doc in docs_to_process:
        prompt = f"""
        Analyze the following text and identify all people mentioned.
        For each person, provide:
        - Name
        - Role (e.g., Defendant, Witness, Lawyer, Judge, etc.)
        - Category (Main Party, Key Witness, Peripheral)
        - Brief Description
        
        Text ({doc['title']}):
        {doc['content'][:15000]} 
        
        Return ONLY a JSON array of objects with keys: name, role, category, description.
        If no people are found, return [].
        """
        
        step_info = {
            "step_name": f"Extraction - {doc['title']}",
            "used_model": model_to_use,
            "prompt_sent": prompt,
            "content_snippet": doc['content'][:200] + "...",
            "raw_response": "",
            "error": None
        }
        
        try:
            response = await call_llm(llm_endpoint, api_key, [{"role": "user", "content": prompt}], model=model_to_use)
            step_info["raw_response"] = response
            
            # Try to parse JSON
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                people = json.loads(json_match.group(0))
                if isinstance(people, list):
                    extracted_people.extend(people)
            else:
                step_info["error"] = "No JSON array found in response"
                
        except Exception as e:
            logger.error(f"Error processing doc {doc['title']}: {e}")
            step_info["error"] = str(e)
            
        debug_steps.append(schemas_ai.DebugStep(**step_info))

    # 2. Consolidate
    if not extracted_people:
        return {"personae": [], "debug_steps": debug_steps}

    consolidation_prompt = f"""
    Here is a list of people identified from various documents in a legal case. 
    Merge duplicate entries (resolve aliases, e.g., "John Smith" and "Mr. Smith").
    Standardize roles and categories.
    Ensure "Key Witness" category is used for witnesses likely to be required to give live evidence.
    
    Raw List:
    {json.dumps(extracted_people)}
    
    Return ONLY a JSON array of objects with keys: name, role, category, description.
    """
    
    cons_step_info = {
        "step_name": "Consolidation",
        "used_model": model_to_use,
        "prompt_sent": consolidation_prompt,
        "content_snippet": f"Input list size: {len(extracted_people)} items",
        "raw_response": "",
        "error": None
    }
    
    final_people = extracted_people
    try:
        final_response = await call_llm(llm_endpoint, api_key, [{"role": "user", "content": consolidation_prompt}], model=model_to_use)
        cons_step_info["raw_response"] = final_response
        
        json_match = re.search(r'\[.*\]', final_response, re.DOTALL)
        if json_match:
            final_people = json.loads(json_match.group(0))
        else:
             cons_step_info["error"] = "No JSON array found in consolidation response"
             
    except Exception as e:
        logger.error(f"Error consolidating: {e}")
        cons_step_info["error"] = str(e)
        
    debug_steps.append(schemas_ai.DebugStep(**cons_step_info))
    
    return {"personae": final_people, "debug_steps": debug_steps}

@router.post("/cases/{case_id}/dramatis-personae/save-pdf")
async def save_dramatis_personae_pdf(case_id: int, personae: schemas_ai.DramatisPersonaeResponse, db: Session = Depends(get_db)):
    if not HAS_REPORTLAB:
        raise HTTPException(status_code=501, detail="PDF generation library (reportlab) not installed")

    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        elements.append(Paragraph(f"Dramatis Personae: {case.title}", styles['Title']))
        elements.append(Spacer(1, 12))
        
        # Prepare table data
        data = [["Name", "Role", "Category", "Description"]]
        for p in personae.personae:
            # Wrap text to fit in columns
            data.append([
                Paragraph(p.name, styles['Normal']),
                Paragraph(p.role, styles['Normal']),
                Paragraph(p.category, styles['Normal']),
                Paragraph(p.description or "", styles['Normal'])
            ])
            
        t = Table(data, colWidths=[100, 80, 80, 250])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ]))
        elements.append(t)
        doc.build(elements)
        
        pdf_content = buffer.getvalue()
        
        # Save as document with Base64 content
        b64_pdf = base64.b64encode(pdf_content).decode('utf-8')
        
        new_doc = models.Document(
            title="Dramatis Personae.pdf",
            content=f"data:application/pdf;base64,{b64_pdf}",
            case_id=case_id
        )
        
        db.add(new_doc)
        db.commit()
        db.refresh(new_doc)
        
        return {"message": "Saved successfully", "document_id": new_doc.id}
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        raise HTTPException(status_code=500, detail=f"Error generating PDF: {str(e)}")
