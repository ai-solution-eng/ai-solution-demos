from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import httpx
import base64
from . import database, models, models_settings, schemas_chat, rag_memory
from .core import get_logger
from .utils import get_llm_config, get_embedding_config

logger = get_logger(__name__)

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()



def build_case_context(case: models.Case) -> tuple[str, list[str]]:
    """
    Build comprehensive context from case data
    Returns: (context_string, non_readable_documents)
    """
    context = f"""# CASE INFORMATION
Title: {case.title}
Status: {case.status}
Type: {case.case_type}
Defendant: {case.defendant_name}
Date Opened: {case.date_opened}
Description: {case.description}

# LEAD ATTORNEY
Name: {case.lead_attorney.full_name if case.lead_attorney else 'Unassigned'}
Email: {case.lead_attorney.email if case.lead_attorney else 'N/A'}
Specialization: {case.lead_attorney.specialization if case.lead_attorney else 'N/A'}

# EVIDENCE ({len(case.evidence)} items)
"""
    for i, ev in enumerate(case.evidence, 1):
        context += f"\n{i}. [{ev.evidence_type}] {ev.description}"
        context += f"\n   Location: {ev.location_found}"
        context += f"\n   Collected: {ev.collected_date}\n"
    
    # Add Document Context (List only)
    non_readable_docs = [] # Legacy tracking, kept for compatibility
    
    context += "\n# DOCUMENTS\n"
    # List document titles for context
    for i, doc in enumerate(case.documents, 1):
        context += f"\n{i}. {doc.title} (Created: {doc.created_date})"

    return context, non_readable_docs

async def detect_vlm_capability(llm_endpoint: str, api_key: str, model: str) -> bool:
    """Detect if the model supports vision (VLM)"""
    # Common VLM model patterns
    vlm_patterns = [
        'vision', 'vlm', 'vl-',  # Generic vision patterns
        'gpt-4-turbo', 'gpt-4o', 'gpt-4-vision',  # OpenAI
        'claude-3', 'claude-3-opus', 'claude-3-sonnet',  # Anthropic
        'gemini-pro-vision', 'gemini-1.5',  # Google
        'qwen-vl', 'qwen2-vl', 'qwen2.5-vl',  # Qwen vision models
        'llava', 'bakllava',  # LLaVA models
        'cogvlm', 'internvl', 'minicpm-v'  # Other VLMs
    ]
    model_lower = model.lower()
    return any(pattern in model_lower for pattern in vlm_patterns)

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
                logger.debug("Found models:")
                logger.debug(result)
                # Extract model IDs from response
                if "data" in result:
                    return [model["id"] for model in result["data"]]
                return []
            return []
    except Exception as e:
        logger.error(f"Error fetching models: {e}")
        return []

@router.get("/models")
async def list_available_models(db: Session = Depends(get_db)):
    """Get list of available LLM models"""
    llm_endpoint, api_key = get_llm_config(db)
    if not llm_endpoint or not api_key:
        raise HTTPException(
            status_code=503, 
            detail="LLM endpoint not configured. Please configure in Admin."
        )
    
    models = await get_available_models(llm_endpoint, api_key)
    
    if not models:
        # Fallback to common models if endpoint doesn't support /v1/models
        models = ["gpt-oss-20b"]
    
    return {
        "models": models,
        "default": models[0] if models else None
    }

@router.post("/cases/{case_id}", response_model=schemas_chat.ChatResponse)
async def chat_with_case(
    case_id: int, 
    chat_request: schemas_chat.ChatRequest,
    db: Session = Depends(get_db)
):
    """
    Chat endpoint with case context and automatic RAG.
    
    RAG Processing:
    - Automatically uses all case documents from the database
    - Optionally accepts additional uploaded documents in the request
    - Performs semantic search across all documents to find relevant chunks
    - Augments LLM context with the most relevant information
    
    No user action required - documents are automatically included!
    """
    # Verify case exists
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # Get LLM configuration
    llm_endpoint, api_key = get_llm_config(db)
    if not llm_endpoint or not api_key:
        raise HTTPException(status_code=500, detail="LLM configuration missing. Please configure in Admin settings.")
    
    # Build case context (without RAG chunks)
    case_context, non_readable_docs = build_case_context(case)
    
    # Debug logging for development
    logger.debug("="*60)
    logger.debug(f"CHAT REQUEST DEBUG - Case ID: {case_id}")
    logger.debug("="*60)
    logger.debug(f"Case Title: {case.title}")
    logger.debug(f"Case Documents in DB: {len(case.documents)}")
    if case.documents:
        for i, doc in enumerate(case.documents, 1):
            logger.debug(f"  {i}. {doc.title} (ID: {doc.id}, Created: {doc.created_date})")
    logger.debug(f"Query: {chat_request.message[:100]}...")
    logger.debug(f"Additional Uploaded Documents: {len(chat_request.documents) if chat_request.documents else 0}")
    
    # Process RAG documents - automatically use case documents from DB
    rag_context = ""
    chunks_used = 0
    rag_status = {}
    
    logger.info("RAG PROCESSING START")
    
    try:
        # Prepare documents for RAG processing
        rag_documents = []
        
        # 1. First, add all case documents from database
        if case.documents:
            logger.debug(f"Loading {len(case.documents)} document(s) from database...")
            for idx, doc in enumerate(case.documents, 1):
                logger.debug(f"  [DB-{idx}] Processing: {doc.title}")
                
                # Convert document content to bytes
                if doc.content:
                    try:
                        # Document content is stored as text in DB
                        content_bytes = doc.content.encode('utf-8')
                        logger.debug(f"      Content length: {len(content_bytes)} bytes")
                        
                        rag_documents.append({
                            'title': doc.title,
                            'content': content_bytes,
                            'id': f"db_{doc.id}"
                        })
                        logger.debug(f"      ✓ Successfully prepared for RAG")
                    except Exception as e:
                        logger.warning(f"      ✗ Error preparing document: {e}")
                        continue
                else:
                    logger.warning(f"      ⚠ Document has no content, skipping")
        else:
            logger.info("No documents found in database for this case")
        
        # 2. Then, add any additionally uploaded documents (optional)
        if chat_request.documents and len(chat_request.documents) > 0:
            logger.debug(f"Processing {len(chat_request.documents)} additional uploaded document(s)...")
            
            for idx, doc_dict in enumerate(chat_request.documents, 1):
                filename = doc_dict.get('filename', 'unknown.txt')
                content_b64 = doc_dict.get('content', '')
                
                logger.debug(f"  [Upload-{idx}] Processing: {filename}")
                logger.debug(f"      Base64 content length: {len(content_b64)} chars")
                
                # Decode base64 content
                try:
                    content_bytes = base64.b64decode(content_b64)
                    logger.debug(f"      Decoded to {len(content_bytes)} bytes")
                    
                    rag_documents.append({
                        'title': filename,
                        'content': content_bytes,
                        'id': f"upload_{filename}"
                    })
                    logger.debug(f"      ✓ Successfully prepared for RAG")
                except Exception as e:
                    logger.warning(f"      ✗ Error decoding: {e}")
                    continue
        
        # 3. Build RAG context using all documents
        if rag_documents:
            # Get embedding configuration
            emb_endpoint, emb_api_key, emb_model = get_embedding_config(db)
            
            if not emb_endpoint or not emb_api_key:
                logger.warning(f"⚠ Embedding API not configured - skipping RAG")
                logger.warning(f"  Configure embedding_endpoint and embedding_api_key in settings")
                rag_status = {
                    "enabled": False,
                    "reason": "Embedding API not configured"
                }
            else:
                logger.info("─"*60)
                logger.info(f"Building RAG context from {len(rag_documents)} total document(s)...")
                logger.info(f"Using embedding model: {emb_model}")
                
                # Call enhanced RAG pipeline with status tracking
                rag_context, chunks_used, rag_status = await rag_memory.build_rag_context(
                    query=chat_request.message,
                    documents=rag_documents,
                    endpoint=emb_endpoint,
                    api_key=emb_api_key,
                    model=emb_model,
                    top_k=5  # Retrieve top 5 most relevant chunks
                )
                
                if rag_context:
                    logger.info(f"✓ RAG: Retrieved {chunks_used} relevant chunks")
                    logger.info(f"  Context length: {len(rag_context)} characters")
                else:
                    logger.warning(f"✗ RAG: No context generated (empty result)")
        else:
            logger.info(f"⚠ No documents available for RAG processing")
            rag_status = {
                "enabled": False,
                "reason": "No documents available"
            }
            
    except Exception as e:
        logger.error(f"✗ Error in RAG processing: {e}", exc_info=True)
        # Continue without RAG context if there's an error
        rag_context = ""
        chunks_used = 0
        rag_status = {
            "enabled": False,
            "error": str(e),
            "error_type": type(e).__name__
        }
    
    logger.info("RAG PROCESSING END")
    
    # Determine model to use
    model_to_use = chat_request.model
    if not model_to_use:
        available_models = await get_available_models(llm_endpoint, api_key)
        if available_models:
            model_to_use = available_models[0]
        else:
            model_to_use = "gpt-oss-20b"
    
    # Detect if model supports vision
    is_vlm = await detect_vlm_capability(llm_endpoint, api_key, model_to_use)
    
    # Prepare system message
    system_content = f"""You are a legal assistant helping prosecutors analyze case details.

{case_context}
"""
    
    # Add RAG context if available
    if rag_context:
        system_content += f"""
# RELEVANT DOCUMENT EXCERPTS
The following excerpts were retrieved from uploaded documents as most relevant to the query:

{rag_context}
"""
    
    system_content += """
Provide accurate, professional responses based on this case data. If asked about information not in the case files, clearly state that."""
    
    # Add notification about non-readable documents if any
    notification = ""
    if non_readable_docs:
        notification = "\n\n**Note:** The following documents contain binary/non-text content and were not included in the context:\n"
        for doc_name in non_readable_docs:
            notification += f"- {doc_name}\n"
    
    # Prepare messages for LLM
    # Only use special Qwen format if it's actually a VLM model AND we have visual content
    # For text-only queries, use standard format even for Qwen models
    use_qwen_format = 'qwen' in model_to_use.lower() and is_vlm
    
    if use_qwen_format:
        # Qwen VLM format: no system role, context in user message
        messages = []
        
        # Add conversation history
        for msg in chat_request.history:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Combine system context with user message for Qwen
        combined_message = f"{system_content}\n\n---\n\nUser Question: {chat_request.message}"
        messages.append({"role": "user", "content": combined_message})
    else:
        # Standard format with system role (works for most models including Qwen for text-only)
        messages = [{"role": "system", "content": system_content}]
        
        # Add conversation history
        for msg in chat_request.history:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Add current user message
        messages.append({"role": "user", "content": chat_request.message})
    
    # Call LLM API
    try:
        # Normalize endpoint
        base_url = llm_endpoint.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url.rstrip('/v1')

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_to_use,
                    "messages": messages,
                    "temperature": 0.0,
                    "max_tokens": 2000
                }
            )
            
            if response.status_code != 200:
                error_detail = response.text
                logger.error(f"LLM API Error: {error_detail}")
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM API error ({response.status_code}): {error_detail}"
                )
            
            result = response.json()
            
            # Check if response has choices
            if not result.get("choices") or len(result["choices"]) == 0:
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM returned empty response: {result}"
                )
            
            assistant_message = result["choices"][0]["message"]["content"]
            
            if not assistant_message:
                raise HTTPException(
                    status_code=502,
                    detail="LLM returned empty message content"
                )
            
            # Append notification about non-readable documents
            if notification:
                assistant_message += notification
            
            # Build debug information
            debug_info = {
                "model": model_to_use,
                "is_vlm": is_vlm,
                "system_message": system_content,
                "evidence_count": len(case.evidence),
                "case_documents_count": len(case.documents),
                "additional_uploaded_documents": len(chat_request.documents) if chat_request.documents else 0,
                "non_readable_documents": non_readable_docs,
                "message_count": len(messages),
                "total_tokens_estimate": len(str(messages)) // 4,
                "rag_chunks_used": chunks_used,
                "rag_enabled": chunks_used > 0,
                "rag_status": rag_status  # Include comprehensive RAG pipeline status
            }
            
            return schemas_chat.ChatResponse(
                response=assistant_message,
                context_used=True,
                debug_info=debug_info
            )
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LLM request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error communicating with LLM: {str(e)}")
