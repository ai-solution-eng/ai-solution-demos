"""
Test script for RAG pipeline functionality
"""
from app.rag_memory import (
    extract_text_from_content,
    chunk_text,
    InMemoryRAG,
    build_rag_context
)

def test_text_extraction():
    """Test text extraction from different formats"""
    print("=" * 60)
    print("Testing Text Extraction")
    print("=" * 60)
    
    # Test plain text
    text_content = b"This is a sample legal document about contract law."
    extracted = extract_text_from_content(text_content, "sample.txt")
    print(f"✓ Plain text extraction: {extracted[:50]}...")
    
    # Test JSON
    json_content = b'{"case": "Smith v. Jones", "verdict": "guilty"}'
    extracted = extract_text_from_content(json_content, "case.json")
    print(f"✓ JSON extraction: {extracted[:50]}...")
    
    print()

def test_chunking():
    """Test text chunking"""
    print("=" * 60)
    print("Testing Text Chunking")
    print("=" * 60)
    
    sample_text = """
    This is a legal document about contract law. Contracts are legally binding agreements.
    They require offer, acceptance, and consideration. The parties must have legal capacity.
    Contracts can be written or oral. Written contracts are easier to enforce in court.
    Breach of contract occurs when one party fails to perform their obligations.
    Remedies for breach include damages, specific performance, and rescission.
    """ * 3  # Repeat to make it longer
    
    chunks = chunk_text(sample_text, chunk_size=200, overlap=30)
    print(f"✓ Created {len(chunks)} chunks from {len(sample_text)} characters")
    print(f"✓ First chunk: {chunks[0][:80]}...")
    if len(chunks) > 1:
        print(f"✓ Second chunk: {chunks[1][:80]}...")
    print()

def test_rag_pipeline():
    """Test complete RAG pipeline"""
    print("=" * 60)
    print("Testing Complete RAG Pipeline")
    print("=" * 60)
    
    # Create sample documents
    documents = [
        {
            'title': 'contract_law.txt',
            'content': b"""
            Contract Law Basics
            
            A contract is a legally binding agreement between two or more parties.
            Essential elements include: offer, acceptance, consideration, and intention to create legal relations.
            
            Types of Contracts:
            1. Bilateral contracts - both parties exchange promises
            2. Unilateral contracts - one party makes a promise in exchange for an act
            3. Express contracts - terms are explicitly stated
            4. Implied contracts - terms are inferred from conduct
            
            Breach of Contract:
            When a party fails to perform their contractual obligations, it constitutes a breach.
            Remedies include damages, specific performance, and rescission.
            """,
            'id': 'doc1'
        },
        {
            'title': 'criminal_law.txt',
            'content': b"""
            Criminal Law Overview
            
            Criminal law deals with conduct that is harmful to society.
            The prosecution must prove guilt beyond a reasonable doubt.
            
            Elements of a Crime:
            1. Actus reus - the guilty act
            2. Mens rea - the guilty mind
            
            Types of Crimes:
            - Felonies: serious crimes like murder, robbery
            - Misdemeanors: less serious crimes like petty theft
            - Infractions: minor violations like traffic tickets
            
            Criminal defendants have rights including the right to counsel and trial by jury.
            """,
            'id': 'doc2'
        }
    ]
    
    # Test queries
    queries = [
        "What are the essential elements of a contract?",
        "What is the difference between felonies and misdemeanors?",
        "What remedies are available for breach of contract?"
    ]
    
    for query in queries:
        print(f"\nQuery: {query}")
        context, num_chunks = build_rag_context(query, documents, top_k=3)
        print(f"✓ Retrieved {num_chunks} relevant chunks")
        if context:
            # Show first 150 characters of context
            preview = context.replace('\n', ' ')[:150]
            print(f"  Preview: {preview}...")
    
    print("\n" + "=" * 60)
    print("✓ All RAG tests completed successfully!")
    print("=" * 60)

def test_in_memory_rag():
    """Test InMemoryRAG class directly"""
    print("\n" + "=" * 60)
    print("Testing InMemoryRAG Class")
    print("=" * 60)
    
    rag = InMemoryRAG()
    
    documents = [
        {
            'title': 'evidence_rules.txt',
            'content': b"""
            Rules of Evidence
            
            Evidence must be relevant and material to be admissible in court.
            Hearsay is generally inadmissible unless it falls under an exception.
            The best evidence rule requires original documents when available.
            """,
            'id': 'doc3'
        }
    ]
    
    # Index documents
    num_chunks = rag.index_documents(documents)
    print(f"✓ Indexed {num_chunks} chunks")
    
    # Retrieve relevant chunks
    results = rag.retrieve("What is the best evidence rule?", top_k=2)
    print(f"✓ Retrieved {len(results)} results")
    
    for i, (chunk, score, metadata) in enumerate(results, 1):
        print(f"\n  Result {i} (score: {score:.3f}):")
        print(f"  Document: {metadata['document_title']}")
        print(f"  Chunk: {chunk[:100]}...")
    
    print()

if __name__ == "__main__":
    try:
        test_text_extraction()
        test_chunking()
        test_rag_pipeline()
        test_in_memory_rag()
        
        print("\n" + "🎉 " * 20)
        print("ALL TESTS PASSED!")
        print("🎉 " * 20)
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
