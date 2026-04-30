from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import time
from . import models, schemas, database, seed, routers_admin, routers_settings, routers_chat, routers_ai, routers_video
from .database import engine
from .core import get_settings, get_cors_config, setup_logging, get_logger, SECURITY_HEADERS

# Initialize settings
settings = get_settings()

# Setup logging
setup_logging(
    level=settings.LOG_LEVEL,
    json_format=(settings.LOG_FORMAT == "json"),
    log_file=settings.LOG_FILE if settings.LOG_FILE else None
)

logger = get_logger(__name__)

# Create tables first
models.Base.metadata.create_all(bind=engine)

# Initialize FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.DEBUG,
    docs_url="/docs" if settings.DEBUG else None,  # Disable docs in production
    redoc_url="/redoc" if settings.DEBUG else None,
)

# -----------------------------------------------------------------
# CORS configuration
# -----------------------------------------------------------------
# Production-ready CORS configuration using environment variables
# Set ALLOWED_ORIGINS in .env (e.g., "https://yourdomain.com,http://localhost:3000")
cors_config = get_cors_config(settings)
app.add_middleware(CORSMiddleware, **cors_config)

logger.info(f"CORS configured with origins: {settings.ALLOWED_ORIGINS}")

# -----------------------------------------------------------------
# Middleware Configuration
# -----------------------------------------------------------------

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request, call_next):
    """Add security headers to all responses"""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


# Request ID and Logging Middleware
@app.middleware("http")
async def log_requests(request, call_next):
    """Log all requests with timing"""
    start_time = time.time()
    
    # Generate request ID
    request_id = f"{int(start_time * 1000)}"
    
    logger.info(
        "Request started",
        extra={"extra_fields": {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else "unknown"
        }}
    )
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(process_time)
    
    logger.info(
        "Request completed",
        extra={"extra_fields": {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "process_time": process_time
        }}
    )
    
    return response


# Trusted Host Middleware (Production)
if settings.is_production:
    # Extract hosts from ALLOWED_ORIGINS
    allowed_hosts = [
        origin.replace("https://", "").replace("http://", "").split(":")[0]
        for origin in settings.ALLOWED_ORIGINS
    ]
    # TrustedHostMiddleware can cause issues in Kubernetes where internal traffic
    # (e.g. from frontend SSR) uses service names like 'lawfirm-backend' which
    # aren't in ALLOWED_ORIGINS. Ingress usually handles host validation anyway.
    # app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    # logger.info(f"Trusted hosts: {allowed_hosts}")
    pass

# -----------------------------------------------------------------
# Routers
# -----------------------------------------------------------------

app.include_router(routers_admin.router)
app.include_router(routers_settings.router)
app.include_router(routers_chat.router)
app.include_router(routers_ai.router)
app.include_router(routers_video.router)


# -----------------------------------------------------------------
# Dependencies
# -----------------------------------------------------------------

def get_db():
    """Database session dependency"""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -----------------------------------------------------------------
# Startup/Shutdown Events
# -----------------------------------------------------------------

@app.on_event("startup")
def startup_event():
    """Application startup tasks"""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"Environment: {'Production' if settings.is_production else 'Development'}")
    logger.info(f"Debug mode: {settings.DEBUG}")
    
    # Seed database
    db = database.SessionLocal()
    try:
        seed.seed_db(db)
        logger.info("Database seeding completed")
    except Exception as e:
        logger.error(f"Database seeding failed: {e}", exc_info=True)
    finally:
        db.close()
    
    logger.info("Application startup complete")


@app.on_event("shutdown")
def shutdown_event():
    """Application shutdown tasks"""
    logger.info("Application shutting down")

# -----------------------------------------------------------------
# Health Check Endpoints
# -----------------------------------------------------------------

@app.get("/")
def read_root():
    """Root endpoint"""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "status": "operational"
    }


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Health check endpoint for monitoring and load balancers
    Checks database connectivity
    """
    try:
        # Test database connection
        db.execute("SELECT 1")
        db_status = "healthy"
    except Exception as e:
        logger.error(f"Database health check failed: {e}", exc_info=True)
        db_status = "unhealthy"
        raise HTTPException(status_code=503, detail="Database unavailable")
    
    return {
        "status": "healthy",
        "service": "lawfirm-api",
        "version": settings.APP_VERSION,
        "database": db_status
    }


@app.get("/health/ready")
def readiness_check():
    """Kubernetes readiness probe"""
    return {"status": "ready"}

@app.get("/health/live")
def liveness_check():
    """Kubernetes liveness probe"""
    return {"status": "alive"}


@app.post("/cases/{case_id}/documents", response_model=schemas.Document)
async def upload_document(
    case_id: int, 
    file: UploadFile = File(...), 
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Upload a document to a case"""
    logger.info(f"Uploading document to case {case_id}: {file.filename}")

    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    try:
        content = await file.read()
    
        # Simple text extraction based on file extension
        text_content = description or ""
        if file.filename.endswith((".txt", ".md", ".log", ".html", ".csv", ".json", ".xml", ".rtf")):
            try:
                text_content = content.decode("utf-8")
            except Exception as e:
                logger.warning(f"Failed to decode file {file.filename}: {e}")
                pass
                
        doc = models.Document(
            title=file.filename,
            content=text_content,
            case_id=case_id
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        
        logger.info(f"Document uploaded successfully: {doc.id}")
        return doc

    except Exception as e:
        logger.error(f"Error uploading document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to upload document")


@app.delete("/cases/{case_id}/documents/{document_id}")
def delete_document(case_id: int, document_id: int, db: Session = Depends(get_db)):
    """Delete a document from a case"""
    logger.info(f"Deleting document {document_id} from case {case_id}")
    
    # Verify case exists
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # Find and delete document
    document = db.query(models.Document).filter(
        models.Document.id == document_id,
        models.Document.case_id == case_id
    ).first()
    
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    db.delete(document)
    db.commit()
    
    logger.info(f"Document deleted successfully: {document_id}")
    return {"message": "Document deleted successfully", "document_id": document_id}


@app.post("/cases/{case_id}/evidence")
def create_evidence(case_id: int, evidence: schemas.EvidenceCreate, db: Session = Depends(get_db)):
    """Add evidence to a case"""
    logger.info(f"Adding evidence to case {case_id}")
    
    # Verify case exists
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    db_evidence = models.Evidence(
        description=evidence.description,
        evidence_type=evidence.evidence_type,
        location_found=evidence.location_found,
        case_id=case_id
    )
    db.add(db_evidence)
    db.commit()
    db.refresh(db_evidence)
    
    logger.info(f"Evidence added successfully: {db_evidence.id}")
    return db_evidence

# -----------------------------------------------------------------
# Case Management Endpoints
# -----------------------------------------------------------------

@app.post("/cases", response_model=schemas.Case)
def create_case(case: schemas.CaseCreate, db: Session = Depends(get_db)):
    """Create a new case"""
    logger.info(f"Creating new case: {case.title}")
    
    # Verify lead attorney exists
    lawyer = db.query(models.Lawyer).filter(models.Lawyer.id == case.lead_attorney_id).first()
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lead attorney not found")
    
    db_case = models.Case(
        title=case.title,
        description=case.description,
        status=case.status,
        case_type=case.case_type,
        defendant_name=case.defendant_name,
        lead_attorney_id=case.lead_attorney_id
    )
    db.add(db_case)
    db.commit()
    db.refresh(db_case)
    
    logger.info(f"Case created successfully: {db_case.id}")
    return db_case


@app.get("/cases", response_model=List[schemas.Case])
def read_cases(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """Get all cases with pagination"""
    cases = db.query(models.Case).offset(skip).limit(limit).all()
    logger.debug(f"Retrieved {len(cases)} cases")
    return cases


@app.get("/cases/{case_id}", response_model=schemas.Case)
def read_case(case_id: int, db: Session = Depends(get_db)):
    """Get a specific case by ID"""
    case = db.query(models.Case).filter(models.Case.id == case_id).first()
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/lawyers", response_model=List[schemas.Lawyer])
def read_lawyers(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """Get all lawyers with pagination"""
    lawyers = db.query(models.Lawyer).offset(skip).limit(limit).all()
    logger.debug(f"Retrieved {len(lawyers)} lawyers")
    return lawyers
