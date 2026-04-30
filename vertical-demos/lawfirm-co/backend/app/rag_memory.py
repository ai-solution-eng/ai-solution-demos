"""
In-memory RAG pipeline for demo purposes.
Uses API-based embeddings and cosine similarity for retrieval.

Enhanced with comprehensive error handling and runtime debugging.
"""
from typing import List, Tuple, Dict, Optional
import numpy as np
import httpx
import re
import logging
from enum import Enum
from datetime import datetime

# Configure logging
logger = logging.getLogger(__name__)

class RAGPhase(str, Enum):
    """RAG pipeline phases for tracking"""
    DOCUMENT_RETRIEVAL = "document_retrieval"
    CONTENT_PARSING = "content_parsing"
    TEXT_CHUNKING = "text_chunking"
    EMBEDDING_GENERATION = "embedding_generation"
    SIMILARITY_SEARCH = "similarity_search"
    CONTEXT_BUILDING = "context_building"

class RAGStatus:
    """Track RAG pipeline status and errors"""
    def __init__(self):
        self.phase_status: Dict[str, Dict] = {}
        self.errors: List[Dict] = []
        self.warnings: List[Dict] = []
        self.start_time = datetime.now()
        
    def log_phase_start(self, phase: RAGPhase, details: str = ""):
        """Log the start of a pipeline phase"""
        self.phase_status[phase.value] = {
            "status": "started",
            "start_time": datetime.now(),
            "details": details
        }
        logger.info(f"[RAG] Phase {phase.value} started: {details}")
    
    def log_phase_success(self, phase: RAGPhase, details: str = "", metrics: Dict = None):
        """Log successful completion of a phase"""
        if phase.value in self.phase_status:
            elapsed = (datetime.now() - self.phase_status[phase.value]["start_time"]).total_seconds()
            self.phase_status[phase.value].update({
                "status": "success",
                "end_time": datetime.now(),
                "elapsed_seconds": elapsed,
                "details": details,
                "metrics": metrics or {}
            })
            logger.info(f"[RAG] Phase {phase.value} completed: {details} ({elapsed:.2f}s)")
    
    def log_phase_error(self, phase: RAGPhase, error: Exception, details: str = ""):
        """Log phase error"""
        error_info = {
            "phase": phase.value,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "details": details,
            "timestamp": datetime.now()
        }
        self.errors.append(error_info)
        
        if phase.value in self.phase_status:
            self.phase_status[phase.value].update({
                "status": "failed",
                "error": error_info
            })
        
        logger.error(f"[RAG] Phase {phase.value} failed: {error} - {details}")
    
    def log_warning(self, phase: RAGPhase, message: str):
        """Log a warning"""
        warning_info = {
            "phase": phase.value,
            "message": message,
            "timestamp": datetime.now()
        }
        self.warnings.append(warning_info)
        logger.warning(f"[RAG] Warning in {phase.value}: {message}")
    
    def get_summary(self) -> Dict:
        """Get summary of RAG pipeline execution"""
        total_elapsed = (datetime.now() - self.start_time).total_seconds()
        return {
            "total_elapsed_seconds": total_elapsed,
            "phases": self.phase_status,
            "errors": self.errors,
            "warnings": self.warnings,
            "success": len(self.errors) == 0
        }

# No local model - using API-based embeddings
async def generate_embeddings(
    texts: List[str],
    endpoint: str,
    api_key: str,
    model: str = "text-embedding-ada-002",
    status: Optional[RAGStatus] = None,
    input_type: Optional[str] = None
) -> np.ndarray:
    """
    Generate embeddings using an API endpoint (OpenAI-compatible).
    
    Args:
        texts: List of texts to embed
        endpoint: API endpoint URL
        api_key: API key for authentication
        model: Model name to use for embeddings
        status: RAGStatus object for tracking
        input_type: Optional input type ('passage' or 'query') for APIs that require it
    
    Returns:
        numpy array of embeddings
    
    Raises:
        Exception: If embedding generation fails
    """
    phase = RAGPhase.EMBEDDING_GENERATION
    
    if status:
        status.log_phase_start(phase, f"Generating embeddings for {len(texts)} text(s) using model '{model}'")
    
    try:
        # Normalize endpoint
        base_url = endpoint.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url.rstrip('/v1')
        
        embeddings_url = f"{base_url}/v1/embeddings"
        
        if status:
            logger.debug(f"Endpoint: {embeddings_url}")
            logger.debug(f"Model: {model}")
            logger.debug(f"Texts to embed: {len(texts)}")
            if input_type:
                logger.debug(f"Input type: {input_type}")
        
        # Build request payload
        payload = {
            "input": texts,
            "model": model
        }
        
        # Add input_type if provided (required by some APIs like Ollama)
        if input_type:
            payload["input_type"] = input_type
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                embeddings_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            
            if response.status_code != 200:
                error_msg = f"Embedding API error ({response.status_code}): {response.text}"
                if status:
                    status.log_phase_error(phase, Exception(error_msg), f"HTTP {response.status_code}")
                raise Exception(error_msg)
            
            result = response.json()
            
            # Extract embeddings from response
            # OpenAI format: {"data": [{"embedding": [...]}, ...]}
            if "data" not in result:
                error_msg = f"Unexpected API response format: {result}"
                if status:
                    status.log_phase_error(phase, Exception(error_msg), "Missing 'data' field")
                raise Exception(error_msg)
            
            embeddings = [item["embedding"] for item in result["data"]]
            embedding_array = np.array(embeddings)
            
            if status:
                status.log_phase_success(
                    phase,
                    f"Generated {len(embeddings)} embeddings",
                    {
                        "num_embeddings": len(embeddings),
                        "embedding_dimension": len(embeddings[0]) if embeddings else 0,
                        "model": model
                    }
                )
            
            return embedding_array
            
    except httpx.TimeoutException as e:
        error_msg = "Embedding API request timed out (60s)"
        if status:
            status.log_phase_error(phase, e, error_msg)
        raise Exception(error_msg)
    except httpx.RequestError as e:
        error_msg = f"Network error connecting to embedding API: {str(e)}"
        if status:
            status.log_phase_error(phase, e, error_msg)
        raise Exception(error_msg)
    except Exception as e:
        if status:
            status.log_phase_error(phase, e, "Unexpected error during embedding generation")
        raise


def extract_text_from_content(
    content: bytes, 
    filename: str,
    status: Optional[RAGStatus] = None
) -> Tuple[str, bool]:
    """
    Extract text from document content based on file extension.
    
    Args:
        content: Raw file bytes
        filename: Name of the file (used to determine extension)
        status: RAGStatus object for tracking
    
    Returns:
        Tuple of (extracted_text, success)
    """
    phase = RAGPhase.CONTENT_PARSING
    extension = filename.lower().split('.')[-1] if '.' in filename else 'unknown'
    
    if status:
        status.log_phase_start(phase, f"Parsing '{filename}' (format: {extension}, size: {len(content)} bytes)")
    
    try:
        # Text-based formats - simple UTF-8 decode
        if extension in ['txt', 'md', 'log', 'csv', 'json', 'xml', 'html']:
            try:
                text = content.decode('utf-8', errors='ignore')
                if status:
                    status.log_phase_success(
                        phase, 
                        f"Successfully decoded {extension.upper()} file",
                        {"format": extension, "text_length": len(text)}
                    )
                return text, True
            except Exception as e:
                if status:
                    status.log_phase_error(phase, e, f"Failed to decode {extension.upper()} file")
                return "", False
        
        # RTF format
        elif extension == 'rtf':
            try:
                from striprtf.striprtf import rtf_to_text
                text = content.decode('utf-8', errors='ignore')
                extracted = rtf_to_text(text)
                if status:
                    status.log_phase_success(
                        phase,
                        "Successfully extracted RTF content",
                        {"format": "rtf", "text_length": len(extracted)}
                    )
                return extracted, True
            except ImportError as e:
                if status:
                    status.log_warning(phase, "striprtf library not available, using fallback")
                # Fallback to basic decode
                text = content.decode('utf-8', errors='ignore')
                return text, True
            except Exception as e:
                if status:
                    status.log_phase_error(phase, e, "RTF extraction failed, attempting fallback")
                # Fallback to basic decode
                try:
                    text = content.decode('utf-8', errors='ignore')
                    return text, True
                except:
                    return "", False
        
        # PDF format
        elif extension == 'pdf':
            try:
                import pypdf
                from io import BytesIO
                
                pdf_file = BytesIO(content)
                reader = pypdf.PdfReader(pdf_file)
                
                num_pages = len(reader.pages)
                if status:
                    logger.debug(f"PDF has {num_pages} page(s)")
                
                text_parts = []
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text()
                    text_parts.append(page_text)
                    if status and (i + 1) % 10 == 0:
                        logger.debug(f"Processed {i + 1}/{num_pages} pages")
                
                extracted = '\n'.join(text_parts)
                if status:
                    status.log_phase_success(
                        phase,
                        f"Successfully extracted PDF ({num_pages} pages)",
                        {"format": "pdf", "pages": num_pages, "text_length": len(extracted)}
                    )
                return extracted, True
            except ImportError as e:
                error_msg = "pypdf library not available - cannot process PDF files"
                if status:
                    status.log_phase_error(phase, e, error_msg)
                return "", False
            except Exception as e:
                if status:
                    status.log_phase_error(phase, e, f"PDF extraction failed: {str(e)}")
                return "", False
        
        else:
            # Unknown format - try basic decode
            if status:
                status.log_warning(phase, f"Unknown file format '.{extension}', attempting UTF-8 decode")
            try:
                text = content.decode('utf-8', errors='ignore')
                if len(text.strip()) > 0:
                    if status:
                        status.log_phase_success(
                            phase,
                            f"Successfully decoded unknown format as text",
                            {"format": extension, "text_length": len(text)}
                        )
                    return text, True
                else:
                    if status:
                        status.log_warning(phase, "Decoded content is empty")
                    return "", False
            except Exception as e:
                if status:
                    status.log_phase_error(phase, e, "Failed to decode as UTF-8")
                return "", False
                
    except Exception as e:
        if status:
            status.log_phase_error(phase, e, f"Unexpected error parsing {filename}")
        return "", False


def chunk_text(
    text: str, 
    chunk_size: int = 500, 
    overlap: int = 50,
    status: Optional[RAGStatus] = None
) -> List[str]:
    """
    Split text into overlapping chunks.
    
    Args:
        text: Input text to chunk
        chunk_size: Target size of each chunk in characters
        overlap: Number of characters to overlap between chunks
        status: RAGStatus object for tracking
    
    Returns:
        List of text chunks
    """
    phase = RAGPhase.TEXT_CHUNKING
    
    if status:
        status.log_phase_start(phase, f"Chunking text ({len(text)} chars, chunk_size={chunk_size}, overlap={overlap})")
    
    try:
        if not text or len(text) == 0:
            if status:
                status.log_warning(phase, "Input text is empty")
            return []
        
        # Clean up excessive whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            
            # Try to break at sentence boundary if possible
            if end < len(text):
                # Look for sentence endings near the chunk boundary
                search_start = max(start, end - 100)
                search_end = min(len(text), end + 100)
                search_text = text[search_start:search_end]
                
                # Find last sentence ending
                for delimiter in ['. ', '.\n', '! ', '?\n', '? ']:
                    last_delim = search_text.rfind(delimiter)
                    if last_delim != -1:
                        end = search_start + last_delim + 1
                        break
            
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            
            # Move start position with overlap
            start = end - overlap if end < len(text) else end
        
        if status:
            status.log_phase_success(
                phase,
                f"Created {len(chunks)} chunks",
                {
                    "num_chunks": len(chunks),
                    "avg_chunk_size": sum(len(c) for c in chunks) // len(chunks) if chunks else 0,
                    "total_text_length": len(text)
                }
            )
        
        return chunks
        
    except Exception as e:
        if status:
            status.log_phase_error(phase, e, "Failed to chunk text")
        return []


def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """Calculate cosine similarity between two vectors"""
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)


class InMemoryRAG:
    """In-memory RAG pipeline for document retrieval using API-based embeddings"""
    
    def __init__(self, endpoint: str, api_key: str, model: str, status: Optional[RAGStatus] = None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.chunks: List[str] = []
        self.embeddings: Optional[np.ndarray] = None
        self.metadata: List[Dict] = []
        self.status = status
    
    async def index_documents(self, documents: List[Dict]) -> Tuple[int, List[str]]:
        """
        Index documents by chunking and embedding them.
        
        Args:
            documents: List of dicts with 'content' (bytes), 'title' (str), 'id' (str)
        
        Returns:
            Tuple of (num_chunks_created, list_of_failed_documents)
        """
        phase = RAGPhase.DOCUMENT_RETRIEVAL
        
        if self.status:
            self.status.log_phase_start(phase, f"Indexing {len(documents)} document(s)")
        
        all_chunks = []
        all_metadata = []
        failed_documents = []
        
        try:
            for idx, doc in enumerate(documents, 1):
                doc_title = doc.get('title', f'document_{idx}')
                doc_id = doc.get('id', 'unknown')
                
                if self.status:
                    logger.debug(f"[{idx}/{len(documents)}] Processing: {doc_title}")
                
                try:
                    # Extract text from document
                    text, success = extract_text_from_content(
                        doc['content'], 
                        doc_title,
                        self.status
                    )
                    
                    if not success or not text:
                        failed_documents.append(doc_title)
                        if self.status:
                            self.status.log_warning(
                                phase, 
                                f"Failed to extract text from '{doc_title}'"
                            )
                        continue
                    
                    if self.status:
                        logger.debug(f"Extracted {len(text)} characters")
                    
                    # Chunk the text
                    chunks = chunk_text(text, status=self.status)
                    
                    if not chunks:
                        failed_documents.append(doc_title)
                        if self.status:
                            self.status.log_warning(
                                phase,
                                f"No chunks created from '{doc_title}'"
                            )
                        continue
                    
                    if self.status:
                        logger.debug(f"Created {len(chunks)} chunk(s)")
                    
                    # Store chunks with metadata
                    for chunk in chunks:
                        all_chunks.append(chunk)
                        all_metadata.append({
                            'document_title': doc_title,
                            'document_id': doc_id
                        })
                    
                except Exception as e:
                    failed_documents.append(doc_title)
                    if self.status:
                        self.status.log_warning(
                            phase,
                            f"Error processing '{doc_title}': {str(e)}"
                        )
                    continue
            
            if not all_chunks:
                if self.status:
                    self.status.log_phase_error(
                        phase,
                        Exception("No chunks created from any document"),
                        f"Failed documents: {', '.join(failed_documents)}"
                    )
                return 0, failed_documents
            
            if self.status:
                self.status.log_phase_success(
                    phase,
                    f"Extracted {len(all_chunks)} chunks from {len(documents) - len(failed_documents)}/{len(documents)} documents",
                    {
                        "total_chunks": len(all_chunks),
                        "successful_documents": len(documents) - len(failed_documents),
                        "failed_documents": len(failed_documents)
                    }
                )
            
            # Generate embeddings for all chunks using API
            if self.status:
                logger.debug(f"Generating embeddings for {len(all_chunks)} chunks...")
            
            self.chunks = all_chunks
            self.metadata = all_metadata
            self.embeddings = await generate_embeddings(
                all_chunks,
                self.endpoint,
                self.api_key,
                self.model,
                self.status,
                input_type="passage"  # Document chunks are passages
            )
            
            return len(all_chunks), failed_documents
            
        except Exception as e:
            if self.status:
                self.status.log_phase_error(phase, e, "Unexpected error during document indexing")
            raise
    
    async def retrieve(self, query: str, top_k: int = 5) -> List[Tuple[str, float, Dict]]:
        """
        Retrieve most relevant chunks for a query.
        
        Args:
            query: Search query
            top_k: Number of top chunks to retrieve
        
        Returns:
            List of (chunk_text, similarity_score, metadata) tuples
        """
        phase = RAGPhase.SIMILARITY_SEARCH
        
        if self.status:
            self.status.log_phase_start(phase, f"Searching for top {top_k} relevant chunks")
        
        try:
            if not self.chunks or self.embeddings is None:
                if self.status:
                    self.status.log_warning(phase, "No indexed documents available for search")
                return []
            
            if self.status:
                logger.debug(f"Searching across {len(self.chunks)} chunks")
            
            # Generate query embedding using API
            query_embeddings = await generate_embeddings(
                [query],
                self.endpoint,
                self.api_key,
                self.model,
                self.status,
                input_type="query"  # User query
            )
            query_embedding = query_embeddings[0]
            
            # Calculate similarities
            similarities = []
            for i, chunk_embedding in enumerate(self.embeddings):
                similarity = cosine_similarity(query_embedding, chunk_embedding)
                similarities.append((i, similarity))
            
            # Sort by similarity (descending)
            similarities.sort(key=lambda x: x[1], reverse=True)
            
            if self.status:
                logger.debug(f"Similarity scores range: {similarities[0][1]:.3f} to {similarities[-1][1]:.3f}")
            
            # Get top-k results
            results = []
            for i, score in similarities[:top_k]:
                results.append((
                    self.chunks[i],
                    float(score),
                    self.metadata[i]
                ))
            
            if self.status:
                self.status.log_phase_success(
                    phase,
                    f"Retrieved {len(results)} relevant chunks",
                    {
                        "num_results": len(results),
                        "top_score": float(results[0][1]) if results else 0,
                        "avg_score": sum(r[1] for r in results) / len(results) if results else 0
                    }
                )
            
            return results
            
        except Exception as e:
            if self.status:
                self.status.log_phase_error(phase, e, "Failed to retrieve relevant chunks")
            raise


async def build_rag_context(
    query: str,
    documents: List[Dict],
    endpoint: str,
    api_key: str,
    model: str = "text-embedding-ada-002",
    top_k: int = 5
) -> Tuple[str, int, Dict]:
    """
    Build RAG context by retrieving relevant chunks from documents.
    
    Args:
        query: User query
        documents: List of document dicts with 'content' (bytes) and 'title' (str)
        endpoint: Embedding API endpoint
        api_key: API key for authentication
        model: Embedding model name
        top_k: Number of chunks to retrieve
    
    Returns:
        Tuple of (context_string, num_chunks_retrieved, status_dict)
    """
    # Initialize status tracking
    status = RAGStatus()
    
    logger.info("="*60)
    logger.info("RAG PIPELINE EXECUTION")
    logger.info("="*60)
    logger.info(f"Query: {query[:100]}{'...' if len(query) > 100 else ''}")
    logger.info(f"Documents: {len(documents)}")
    logger.info(f"Embedding Model: {model}")
    logger.info(f"Top-K: {top_k}")
    logger.info("="*60)
    
    try:
        if not documents:
            status.log_warning(RAGPhase.DOCUMENT_RETRIEVAL, "No documents provided")
            return "", 0, status.get_summary()
        
        # Create RAG instance with status tracking
        rag = InMemoryRAG(endpoint, api_key, model, status)
        
        # Index documents
        logger.info("PHASE 1: Document Indexing")
        logger.info("-" * 60)
        num_chunks, failed_docs = await rag.index_documents(documents)
        
        if num_chunks == 0:
            error_msg = "No chunks created from documents"
            if failed_docs:
                error_msg += f". Failed documents: {', '.join(failed_docs)}"
            status.log_phase_error(
                RAGPhase.DOCUMENT_RETRIEVAL,
                Exception(error_msg),
                "Document indexing failed"
            )
            return "", 0, status.get_summary()
        
        # Retrieve relevant chunks
        logger.info("PHASE 2: Similarity Search")
        logger.info("-" * 60)
        results = await rag.retrieve(query, top_k=top_k)
        
        if not results:
            status.log_warning(RAGPhase.SIMILARITY_SEARCH, "No relevant chunks found")
            return "", 0, status.get_summary()
        
        # Build context string
        logger.info("PHASE 3: Context Building")
        logger.info("-" * 60)
        phase = RAGPhase.CONTEXT_BUILDING
        status.log_phase_start(phase, f"Building context from {len(results)} chunks")
        
        try:
            context_parts = []
            total_chars = 0
            
            for i, (chunk, score, metadata) in enumerate(results, 1):
                doc_title = metadata.get('document_title', 'Unknown')
                context_parts.append(
                    f"--- Document: {doc_title} (Relevance: {score:.2f}) ---"
                )
                context_parts.append(chunk)
                context_parts.append("")
                total_chars += len(chunk)
                
                if status:
                    logger.debug(f"[{i}] {doc_title}: {len(chunk)} chars (score: {score:.3f})")
            
            context = "\n".join(context_parts)
            
            status.log_phase_success(
                phase,
                f"Built context from {len(results)} chunks",
                {
                    "num_chunks": len(results),
                    "total_characters": total_chars,
                    "avg_chunk_size": total_chars // len(results) if results else 0
                }
            )
            
            # Print summary
            logger.info("="*60)
            logger.info("RAG PIPELINE SUMMARY")
            logger.info("="*60)
            summary = status.get_summary()
            logger.info(f"Total Time: {summary['total_elapsed_seconds']:.2f}s")
            logger.info(f"Success: {summary['success']}")
            logger.info(f"Errors: {len(summary['errors'])}")
            logger.info(f"Warnings: {len(summary['warnings'])}")
            logger.info(f"Chunks Retrieved: {len(results)}")
            logger.info(f"Context Size: {len(context)} characters")
            if failed_docs:
                logger.info(f"Failed Documents: {', '.join(failed_docs)}")
            logger.info("="*60)
            
            return context, len(results), summary
            
        except Exception as e:
            status.log_phase_error(phase, e, "Failed to build context string")
            raise
            
    except Exception as e:
        logger.error("="*60)
        logger.error("RAG PIPELINE FAILED")
        logger.error("="*60)
        logger.error(f"Error: {str(e)}")
        logger.error("="*60)
        
        # Log the error if not already logged
        if not status.errors:
            status.log_phase_error(
                RAGPhase.CONTEXT_BUILDING,
                e,
                "Unexpected error in RAG pipeline"
            )
        
        return "", 0, status.get_summary()
