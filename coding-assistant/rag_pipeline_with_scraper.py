"""
Unified RAG + Scraper Pipeline for Open WebUI
---------------------------------------------
"""

import os
import re
import time
import asyncio
import json
import logging
import requests

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Union, Generator, Iterator

from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from pydantic import BaseModel, Field

import tiktoken

import weaviate
# Fallback for v3
from weaviate import Client as V3Client
# Weaviate v4 Client importieren
from weaviate import WeaviateClient
from weaviate.classes import config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PARENT_CHUNK_TOKENS = 350
CHILD_CHUNK_TOKENS = 50
OVERLAP_TOKENS = 20
MAX_API_TOKENS = 380

# Weaviate Client type
WeaviateClientType = Optional[Union['WeaviateClient', 'V3Client']]

# ================================================================
# Unified Pipeline
# ================================================================
class Pipeline:
    class Valves(BaseModel):
        EMBEDDING_MODEL_URL: str = Field(
            default="https://integrate.api.nvidia.com/v1",
            description="Embedding Model Endpoint")
        EMBEDDING_MODEL_NAME: str = Field(
            default="nvidia/nv-embedqa-e5-v5",
            description="Embedding Model Name")
        EMBEDDING_MODEL_API_KEY: str = Field(
            default="YOUR_EMBEDDING_MODEL_API_KEY",
            description="API Key for accessing the embedding model")
        WEAVIATE_URL: str = Field(
            default="http://weaviate.open-webui.svc.cluster.local:80",
            description="Weaviate database URL")
        COLLECTION_NAME: str = Field(
            default="HierarchicalManualChunks",
            description="Weaviate collection name")
        LLM_MODEL_URL: str = Field(
            default="https://integrate.api.nvidia.com/v1",
            description="LLM Model Endpoint")
        LLM_MODEL_NAME: str = Field(
            default="meta/llama-3.1-8b-instruct",
            description="LLM Model Name")
        LLM_MODEL_API_KEY: str = Field(
            default="YOUR_LLM_MODEL_API_KEY",
            description="API Key for accessing the LLM model")        
        TOP_K: int = Field(
            default=8,
            description="Number of chunks to retrieve")
        HPE_BASE_URL: str = Field(
            default="https://support.hpe.com",
            description="HPE Support Page URL")
        HPE_SUPPORT_PAGE: str = Field(
            default="https://support.hpe.com/connect/s/product?language=en_US&kmpmoid=1014847366&tab=manuals&cep=on",
            description="HPE Private Cloud AI Support Page")
        
        #OLLAMA_BASE_URL: str = Field(default="http://host.docker.internal:11434")
        #OLLAMA_MODEL: str = Field(default="llama3:8b")

    def __init__(self):
        self.type = "manifold"
        self.id = "rag_weaviate"
        self.name = "RAG "

        self.valves = self.Valves()
        self.weaviate_client: WeaviateClientType = None
        self.client_version = 0
        self.collection_exists = False

        self.tokenizer = tiktoken.get_encoding("cl100k_base")

        self._connect_weaviate()

    # ============================================================
    # Connect to Weaviate
    # ============================================================
    def _connect_weaviate(self) -> bool:
        if not weaviate not in globals():
            logger.error("Weaviate client not installed.")
            return False
        try:
            url = self.valves.WEAVIATE_URL
            parsed = urlparse(url)
            scheme = parsed.scheme or 'http'
            grpc_hostname = parsed.hostname.replace("weaviate", "weaviate-grpc", 1)
            self.weaviate_client = weaviate.connect_to_custom(
                http_host=parsed.hostname or "weaviate", 
                http_port=parsed.port or 8080,
                http_secure=False if scheme == 'http' else True,
                grpc_host=grpc_hostname,
                grpc_port=50051,
                grpc_secure=False if scheme == 'http' else True,
            )
            if self.weaviate_client.is_ready():
                logger.info("Connected to Weaviate (v{self.client_version}) at {url}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Weaviate: {e}")
            return False

    # ============================================================
    # Scraper + Ingestion
    # ============================================================
    async def scrape_and_ingest(self, max_manuals=1):
        if 'async_playwright' not in globals():
            raise RuntimeError("Playwright and BeautifulSoup required.")

        def extract_row_metadata(self, row):
            metadata = {
                'date': ''
            }            
            cells = row.find_all('td', role='gridcell')            
            for cell in cells:
                if cell.get('data-label') == 'Date' or 'date' in cell.get('data-col-key-value', '').lower():
                    date_text = cell.get_text(strip=True)
                    if date_text and date_text != '':
                        metadata['date'] = date_text
                
                cell_text = cell.get_text(strip=True)                
                if not metadata['date'] and re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}', cell_text):
                    metadata['date'] = cell_text
            return metadata

        def count_tokens(self, text):
            if not text or not text.strip():
                return 0
            return len(self.tokenizer.encode(text.strip()))

        def smart_chunk_by_tokens(self, text, max_tokens, overlap_tokens=OVERLAP_TOKENS):
            if not text or not text.strip():
                return []
            text = text.strip()
            tokens = self.tokenizer.encode(text)
            safe_max_tokens = min(max_tokens, MAX_API_TOKENS)
            if len(tokens) <= safe_max_tokens:
                return [text]
            chunks = []
            start = 0
            while start < len(tokens):
                end = min(start + safe_max_tokens, len(tokens))
                chunk_tokens = tokens[start:end]
                chunk_text = self.tokenizer.decode(chunk_tokens).strip()
                if chunk_text:
                    actual_tokens = count_tokens(chunk_text)
                    if actual_tokens > MAX_API_TOKENS:
                        further_split = smart_chunk_by_tokens(chunk_text, max_tokens, overlap_tokens // 2)
                        chunks.extend(further_split)
                    else:
                        chunks.append(chunk_text)
                if end >= len(tokens):
                    break
                if end - overlap_tokens <= start:
                    start = end
                else:
                    start = end - overlap_tokens
            return chunks

        def validate_and_split_chunk(self, text, max_tokens=None):
            if max_tokens is None:
                max_tokens = MAX_API_TOKENS
            if not text or not text.strip():
                return [text] if text else []
            text = text.strip()
            tokens = self.tokenizer.encode(text)
            if len(tokens) <= max_tokens:
                return [text]
            reduced_overlap = max(5, OVERLAP_TOKENS // 3)
            smaller_chunks = smart_chunk_by_tokens(text, max_tokens, reduced_overlap)
            validated_chunks = []
            for i, chunk in enumerate(smaller_chunks):
                chunk_tokens = count_tokens(chunk)
                if chunk_tokens <= MAX_API_TOKENS:
                    validated_chunks.append(chunk)
                else:
                    no_overlap_chunks = smart_chunk_by_tokens(chunk, max_tokens - 10, 0)
                    validated_chunks.extend(no_overlap_chunks)
            return validated_chunks

        def create_normal_hierarchical_chunks(self, text, title, url, document_id):
            all_chunks = []
            text = text.strip()
            parent_chunks_texts = smart_chunk_by_tokens(text, PARENT_CHUNK_TOKENS)
            parent_chunk_index = 0
            for original_parent_index, parent_text in enumerate(parent_chunks_texts):
                validated_parent_chunks = validate_and_split_chunk(parent_text)
                for validated_parent_text in validated_parent_chunks:
                    parent_token_count = count_tokens(validated_parent_text)
                    if parent_token_count > MAX_API_TOKENS:
                        continue
                    parent_chunk_id = f"{document_id}_parent_{parent_chunk_index}"
                    parent_chunk = {
                        "chunk_id": parent_chunk_id,
                        "chunk_type": "parent",
                        "title": title,
                        "url": url,
                        "document_id": document_id,
                        "text": validated_parent_text,
                        "parent_id": None,
                        "child_ids": [],
                        "chunk_index": parent_chunk_index,
                        "token_count": parent_token_count
                    }
                    child_ids = []
                    if parent_token_count > CHILD_CHUNK_TOKENS * 2:
                        child_chunks_texts = smart_chunk_by_tokens(validated_parent_text, CHILD_CHUNK_TOKENS)
                        child_chunk_index = 0
                        for original_child_index, child_text in enumerate(child_chunks_texts):
                            validated_child_chunks = validate_and_split_chunk(child_text)
                            for validated_child_text in validated_child_chunks:
                                child_token_count = count_tokens(validated_child_text)
                                if child_token_count > MAX_API_TOKENS:
                                    continue
                                child_chunk_id = f"{parent_chunk_id}_child_{child_chunk_index}"
                                child_ids.append(child_chunk_id)
                                child_chunk = {
                                    "chunk_id": child_chunk_id,
                                    "chunk_type": "child",
                                    "title": title,
                                    "url": url,
                                    "document_id": document_id,
                                    "text": validated_child_text,
                                    "parent_id": parent_chunk_id,
                                    "child_ids": [],
                                    "chunk_index": child_chunk_index,
                                    "token_count": child_token_count
                                }
                                all_chunks.append(child_chunk)
                                child_chunk_index += 1
                    parent_chunk["child_ids"] = child_ids
                    all_chunks.append(parent_chunk)
                    parent_chunk_index += 1
            return all_chunks        

        def process_tables(self, soup):
            tables = soup.find_all('table')
            table_texts = []
            for table in tables:
                headers = [th.get_text(strip=True) for th in table.find_all('th')]
                rows = []
                for tr in table.find_all('tr'):
                    cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                    if cells:
                        if headers and len(headers) == len(cells):
                            row_text = '\n'.join([f"{h}: {v}" for h, v in zip(headers, cells)])
                        else:
                            row_text = ' | '.join(cells)
                        rows.append(row_text)
                if rows:
                    table_texts.append('\n\n'.join(rows))
                table.decompose()
            return '\n\n'.join(table_texts)

        def process_links(self, soup):
            for a in soup.find_all('a'):
                href = a.get('href', '')
                text = a.get_text(strip=True)
                if href:
                    a.replace_with(f"{text} ({href})")
                else:
                    a.replace_with(text)

        def clean_html_entry(self, entry):
            html_content = entry.get('content', entry.get('text', ''))
            soup = BeautifulSoup(html_content, 'html.parser')
            table_text = process_tables(soup)
            process_links(soup)
            text_clean = soup.get_text(separator='\n')
            text_clean = re.sub(r'\n\s*\n', '\n\n', text_clean.strip())
            text_clean = re.sub(r'^\(#ariaid-title\d+\)\s*', '', text_clean)
            if table_text:
                text_clean += '\n\n' + table_text
            cleaned_entry = {
                "title": entry.get("title", None),
                "url": entry.get("url", None),
                "text_clean": text_clean,
            }
            return cleaned_entry

        def clean_filename(self, title):
            clean_title = re.sub(r'[^\w\s-]', '', title)  
            clean_title = re.sub(r'[-\s]+', '_', clean_title)  
            clean_title = clean_title.strip('_')  
            clean_title = clean_title[:100]  
            return clean_title

        async def scrape_manual_main_block(self, url, browser):
            page = await browser.new_page()
            try:
                # GEÄNDERT: domcontentloaded + längerer Timeout
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(3000)  # 3 Sekunden für JavaScript
                
                # Warte auf main content
                try:
                    await page.wait_for_selector('main.ditasrc', timeout=10000)
                except:
                    try:
                        await page.wait_for_selector('main', timeout=8000)
                    except:
                        print(f"Warning: Main selector not found for {url}, trying anyway...")
                
                await page.wait_for_timeout(1000)
                content = await page.content()
                
                soup = BeautifulSoup(content, 'html.parser')
                main_block = soup.find('main', {'role': 'main', 'class': 'ditasrc'})  

                if not main_block:
                    main_block = soup.find('main', {'role': 'main'})
                    if not main_block:
                        # Versuche auch ohne role attribute
                        main_block = soup.find('main')
                        if not main_block:
                            print(f"No <main> block found for {url}")
                            return None

                main_html = str(main_block)
                return main_html
                
            except Exception as e:
                print(f"Error scraping {url}: {e}")
                return None
            finally:
                await page.close()

        async def toc_scraper(url, browser):
            page = await browser.new_page()
            try:
                # GEÄNDERT: domcontentloaded + längerer Timeout
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(3000)  # 3 Sekunden warten für JavaScript
                content = await page.content()
            except Exception as e:
                print(f"Error loading TOC page {url}: {e}")
                return {"url": url, "toc": []}
            finally:
                await page.close()

            soup = BeautifulSoup(content, 'html.parser')
            toc_container = soup.find(id="contentListArea")
            if not toc_container:
                return {"url": url, "toc": []}

            processed_ids = set()

            def extract_node_info(node):
                node_id = str(node)
                if node_id in processed_ids:
                    return None
                processed_ids.add(node_id)

                title = ''
                href = None

                link = node.find('a', href=True)
                if link:
                    title = link.get_text(strip=True)
                    href = link.get('href')
                else:
                    span = node.find('span', {'data-for': True})
                    if span:
                        title = span.get_text(strip=True)
                        href = span.get('href')
                    else:
                        div = node.find('div')
                        if div:
                            title = div.get_text(strip=True)

                return {'title': title, 'href': href}

            def build_hierarchy(start_node, level=0):
                node_info = extract_node_info(start_node)
                if not node_info:
                    return None

                result = {
                    'title': node_info['title'],
                    'url': node_info['href'],
                    'level': level,
                    'children': []
                }
                
                child_ul = start_node.find('ul', class_='content-menu-nested')
                if child_ul:
                    child_items = child_ul.find_all('li', class_='content-menu-topicref')
                    for child in child_items:
                        child_result = build_hierarchy(child, level + 1)
                        if child_result:
                            result['children'].append(child_result)

                return result
            
            chapters = toc_container.find_all('li', class_='content-menu-topicref')
            results = []
            for chapter in chapters:
                chapter_data = build_hierarchy(chapter, 0)
                if chapter_data and chapter_data['title']:
                    results.append(chapter_data)

            return {"url": url, "toc": results}

        async def scrape_document_contents(toc_data, browser):
            contents = []

            async def scrape_node(node):
                if 'url' in node and node['url']:
                    print(f"Scraping TOC link: {node['title']} -> {node['url']}")
                    main_html = await scrape_manual_main_block(node['url'], browser)
                    if main_html:
                        contents.append({
                            'title': node['title'],
                            'url': node['url'],
                            'content': main_html 
                        })
                    # Small delay to avoid overwhelming the server
                    await asyncio.sleep(0.5)
                for child in node.get('children', []):
                    await scrape_node(child)

            # Rekursiv durch alle Top-Nodes gehen
            for top_node in toc_data['toc']:
                await scrape_node(top_node)

            print(f"Scraped {len(contents)} sections from TOC")
            return contents

        async def scrape_hpe_manuals(page):
            all_manuals_data = []
            current_page = 1
            consecutive_empty_pages = 0            
            try:
                await page.goto(
                    self.valves.HPE_SUPPORT_PAGE,
                    wait_until='domcontentloaded',
                    timeout=60000,
                )
                await page.wait_for_timeout(3000)

                while True:
                    await page.wait_for_selector('table.slds-table', timeout=15000)
                    html_content = await page.content()
                    soup = BeautifulSoup(html_content, 'html.parser')

                    manuals_data = []
                    table = soup.find('table', class_='slds-table')

                    if table:
                        rows = table.find_all('tr', role='row')
                        for row in rows:
                            link_elements = row.find_all('a', href=True)
                            for link_element in link_elements:
                                href = link_element['href']
                                title = link_element.get_text(strip=True)
                                if href and 'hpesc' in href:
                                    if not href.startswith('http'):
                                        href = f"{self.valves.HPE_BASE_URL}{href}"
                                    metadata = extract_row_metadata(row)
                                    manual_entry = {
                                        'title': title,
                                        'link': href,
                                        'date': metadata.get('date', '')
                                    }
                                    manuals_data.append(manual_entry)
                                    logger.info(f"{title} ({metadata.get('date', 'No date')}) -> {href}")

                    if len(manuals_data) == 0:
                        consecutive_empty_pages += 1
                        logger.info(f"No new entries on page {current_page}")
                    else:
                        consecutive_empty_pages = 0

                    all_manuals_data.extend(manuals_data)

                    if consecutive_empty_pages >= 2:
                        logger.info("2 consecutive empty pages - stopping loop")
                        break
                    
                    next_page_number = current_page + 1
                    try:
                        page_button = await page.query_selector(f'a.pagination-item[data-number="{next_page_number}"]')
                        if page_button:
                            logger.info(f"Going to page {next_page_number}")
                            await page_button.click()
                            # GEÄNDERT: domcontentloaded statt networkidle
                            await page.wait_for_load_state('domcontentloaded')
                            await page.wait_for_timeout(2000)
                            current_page += 1
                        else:
                            logger.info(f"Page {next_page_number} not found - end of pagination")
                            break
                    except Exception as e:
                        logger.error(f"Error clicking page {next_page_number}: {e}")
                        break
                    
                    await asyncio.sleep(1)
                return all_manuals_data
            finally:
                await browser.close()

        async def download_pdf(url, browser):
            page = await browser.new_page()
            pdf_bytes = None
            def handle_response(response):
                content_type = response.headers.get('content-type', '').lower()
                if 'application/pdf' in content_type:
                    nonlocal pdf_bytes
                    pdf_bytes = response.body()
            page.on("response", handle_response)
            try:
                # GEÄNDERT: domcontentloaded statt networkidle
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(5000)
                if pdf_bytes:
                    print(f"PDF downloaded (in memory): {len(pdf_bytes)} bytes")
                    return pdf_bytes
                return False
            except Exception as e:
                print(f"Error: {e}")
                return False
            finally:
                await page.close()


        async def check_if_pdf(url, browser):
            page = await browser.new_page()
            found_pdf = False
            
            def handle_response(response):
                nonlocal found_pdf
                content_type = response.headers.get('content-type', '').lower()
                if 'application/pdf' in content_type:
                    found_pdf = True
                    print(f"PDF detected via network response: {response.url}")

            page.on("response", handle_response)            
            try:
                print(f"Loading page to detect content type: {url}")
                # GEÄNDERT: Längerer Timeout
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(3000)
                
                if found_pdf:
                    print("Confirmed: Page loads PDF content")
                    return True
                else:
                    print("No PDF content detected - treating as HTML")
                    return False
                    
            except Exception as e:
                print(f"Error checking content type: {e}")
                return False
            finally:
                await page.close()

        async def process_document(manual, browser, process_pdfs=True):
            url = manual['link']
            title = manual['title']
            print(f"\n=== Processing document: {title} ===")
            print(f"Date: {manual.get('date', 'Unknown')}")
            print(f"URL: {url}")
            print("Analyzing document type...")
            is_pdf = await check_if_pdf(url, browser)
            if is_pdf and process_pdfs:
                print("Document identified as PDF - starting download...")
                pdf_bytes = await download_pdf(url, browser)
                if pdf_bytes:
                    return {
                        'type': 'pdf',
                        'title': title,
                        'url': url,
                        'date': manual.get('date', ''),
                        'content': pdf_bytes,
                        'chunks': []
                    }
                else:
                    print(f"Failed to download PDF: {title}")
                    return None
            else:
                print("Processing as HTML document - scraping TOC and content...")
                try:
                    toc_data = await toc_scraper(url, browser)
                    if not toc_data['toc']:
                        print("No TOC found, trying direct content scraping...")
                        page = await browser.new_page()
                        try:
                            # GEÄNDERT: domcontentloaded + längere Wartezeiten
                            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                            await page.wait_for_timeout(3000)
                            
                            # Versuche verschiedene Selektoren
                            try:
                                await page.wait_for_selector('main', timeout=10000)
                            except:
                                print("Main element not loading, trying anyway...")
                            
                            content = await page.content()
                            soup = BeautifulSoup(content, 'html.parser')
                            
                            # Versuche verschiedene main-Selektoren
                            main_block = (soup.find('main', {'role': 'main'}) or 
                                        soup.find('main') or 
                                        soup.find('div', {'id': 'content'}) or
                                        soup.find('article'))
                            
                            if main_block:
                                scraped_contents = [{
                                    'title': title,
                                    'url': url,
                                    'content': str(main_block)
                                }]
                                print(f"Found content with selector: {main_block.name}")
                            else:
                                print("No main content found with any selector")
                                return None
                        finally:
                            await page.close()
                    else:
                        scraped_contents = await scrape_document_contents(toc_data, browser)
                    
                    if not scraped_contents:
                        print("No content scraped")
                        return None
                    
                    cleaned_entries = [clean_html_entry(entry) for entry in scraped_contents]
                    print(f"HTML cleaned: {len(cleaned_entries)} sections")
                    
                    chunks = []
                    for idx, entry in enumerate(cleaned_entries):
                        text = entry.get("text_clean", "")
                        if not text or len(text) < 50:
                            print(f"Skipping entry {idx} - too short or empty")
                            continue
                            
                        entry_title = entry.get("title", title)
                        entry_url = entry.get("url", url)
                        document_id = f"{clean_filename(entry_title)}_{idx}"
                        
                        entry_chunks = create_normal_hierarchical_chunks(text, entry_title, entry_url, document_id)
                        for chunk in entry_chunks:
                            chunk['date'] = manual.get('date', '')
                        chunks.extend(entry_chunks)
                    
                    print(f"HTML chunked: {len(chunks)} chunks")
                    
                    if len(chunks) == 0:
                        print("Warning: No chunks created from this document")
                        return None
                    
                    document_data = {
                        'type': 'html',
                        'metadata': {
                            'title': title,
                            'url': url,
                            'date': manual.get('date', ''),
                            'scraped_sections': len(cleaned_entries),
                            'toc_structure': toc_data.get('toc', [])
                        },
                        'chunks': chunks
                    }
                    return document_data
                except Exception as e:
                    print(f"Error scraping HTML document: {e}")
                    import traceback
                    traceback.print_exc()
                    return None

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            manuals = await scrape_hpe_manuals(page)
            if not manuals:
                logger.warning("No manuals found.")
                return
            logger.info(f"Found {len(manuals)} manuals, processing first {max_manuals}...")
            results = []
            all_chunks = []
            for i, manual in enumerate(manuals[:1], start=1):
                logger.info(f"\n{'='*60}")
                logger.info(f"Processing document {i}/{min(1, len(manuals))}")
                logger.info(f"{'='*60}")
                result = await process_document(manual, browser, process_pdfs=True)
                if result and result.get('chunks'):
                    all_chunks.extend(result['chunks'])
                    results.append(result)
                    logger.info(f"Document processed: {len(result['chunks'])} chunks created")
                else:
                    logger.info(f"Document failed or had no chunks")
                await asyncio.sleep(1)
            await browser.close()

        if len(all_chunks) > 0:
            logger.info(f"Uploading {len(all_chunks)} chunks to Weaviate...")
            self.weaviate_import(all_chunks)
            logger.info("Weaviate import complete.")
        else:
            logger.warning("No chunks to upload to Weaviate.")

    # ============================================================
    # Helper: basic chunking + ingestion
    # ============================================================

    def get_nvidia_embedding(self, text):
        print(f"[DEBUG] Starting embedding request for text: {text[:60]}...")
        headers = {
            "Authorization": f"Bearer {self.valves.EMBEDDING_MODEL_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "input": text,
            "model": self.valves.EMBEDDING_MODEL_NAME,
            "input_type": "passage"
        }
        response = requests.post(f"{self.valves.EMBEDDING_MODEL_URL}/embeddings", headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        print(f"[DEBUG] Embedding response received.")
        if 'data' in result and len(result['data']) > 0:
            return np.array(result['data'][0]['embedding'])
        else:
            raise Exception(f"Unexpected response format: {result}")
    
    def weaviate_import(self, chunks):
        client = self.weaviate_client
        name = self.valves.COLLECTION_NAME
        if client.collections.exists(name):
            client.collections.delete(name)
        manual_chunks = client.collections.create(
            name=name,
            properties=[
                config.Property(name="chunk_id", data_type=config.DataType.TEXT),
                config.Property(name="chunk_type", data_type=config.DataType.TEXT),
                config.Property(name="text", data_type=config.DataType.TEXT),
                config.Property(name="title", data_type=config.DataType.TEXT),
                config.Property(name="url", data_type=config.DataType.TEXT),
                config.Property(name="document_id", data_type=config.DataType.TEXT),
                config.Property(name="parent_id", data_type=config.DataType.TEXT),
                config.Property(name="child_ids", data_type=config.DataType.TEXT_ARRAY),
                config.Property(name="chunk_index", data_type=config.DataType.INT),
                config.Property(name="document_title", data_type=config.DataType.TEXT),
                config.Property(name="document_date", data_type=config.DataType.TEXT),
                config.Property(name="document_url", data_type=config.DataType.TEXT),                
            ],
            vectorizer_config=config.Configure.Vectorizer.none(),
        )
        with manual_chunks.batch.fixed_size(batch_size=10) as batch:
            for i, chunk in enumerate(chunks):
                try:
                    print(f"[{i+1}/{len(chunks)}] Processing chunk: {chunk['chunk_id']}")
                    embedding = self.get_nvidia_embedding(chunk["text"])
                    
                    properties = {
                        "chunk_id": chunk["chunk_id"],
                        "chunk_type": chunk["chunk_type"],
                        "text": chunk["text"],
                        "title": chunk["title"],
                        "url": chunk["url"],
                        "document_id": chunk["document_id"],
                        "parent_id": chunk["parent_id"] if chunk["parent_id"] else "",
                        "child_ids": chunk["child_ids"],
                        "chunk_index": chunk["chunk_index"],
                        "document_title": chunk.get("title", ""),
                        "document_date": chunk.get("date", ""),
                        "document_url": chunk.get("url", "")
                    }
                    batch.add_object(
                        properties=properties,
                        vector=embedding.tolist()
                    )
                    print(f"  ✓ Added to batch")
                except Exception as e:
                    print(f"  ✗ Error with chunk {chunk['chunk_id']}: {e}")
                    continue

    # ============================================================
    # Utility: NVIDIA embedding
    # ============================================================
    def get_user_text_embedding(self, text: str) -> Optional[np.ndarray]:
        headers = {
            "Authorization": f"Bearer {self.valves.EMBEDDING_MODEL_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "input": text, 
            "model": self.valves.EMBEDDING_MODEL_NAME, 
            "input_type": "query"
            }
        r = requests.post(
            f"{self.valves.EMBEDDING_MODEL_URL}/embeddings", 
            headers=headers, 
            json=payload, 
            timeout=30
            )
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return np.array(data[0]["embedding"], dtype=np.float32)
        else:
            logger.error(f"Unexpected NVIDIA API response: {r}")
            return None

    # ============================================================
    # Query + Generate (RAG)
    # ============================================================
    def search_chunks(self, query: str, k: int = None) -> List[Dict[str, Any]]:
        if k is None:
            k = self.valves.TOP_K
                
        query_embedding = self.get_user_text_embedding(query)
        if query_embedding is None:
            logger.error("Failed to generate query embedding.")
            return []
        client = self.weaviate_client
        if not client:
            logger.error("Weaviate not connected.")
            return []
        try:
            col = client.collections.get(self.valves.COLLECTION_NAME)
            res = col.query.near_vector(
                near_vector=query_embedding.tolist(),
                limit=k * 3,
                return_properties=["chunk_id", "chunk_type", "text", "title", "url", "parent_id", "chunk_index"],
                return_metadata=None
            )
            objects = [o.properties for o in res.objects]

            chunks = []
            
            if objects:
                # First pass: Prioritize smaller, detailed child chunks
                for obj in objects:
                    if obj.get("chunk_type") == "child":
                        text = obj.get("text", "").strip()
                        if len(text) > 1:
                            chunks.append({
                                "chunk_id": obj.get("chunk_id", ""),
                                "chunk_type": obj.get("chunk_type", ""),
                                "text": text,
                                "title": obj.get("title", ""),
                                "url": obj.get("url", ""),
                                "parent_id": obj.get("parent_id", "")
                            })
                            
                            if len(chunks) >= k:
                                break
                
                # Second pass: Fallback to larger parent chunks if not enough children were found
                if len(chunks) < k:
                    for obj in objects:
                        if obj.get("chunk_type") == "parent" and obj.get("chunk_id") not in [c["chunk_id"] for c in chunks]:
                            text = obj.get("text", "").strip()
                            if len(text) > 100:
                                chunks.append({
                                    "chunk_id": obj.get("chunk_id", ""),
                                    "chunk_type": "parent_fallback",
                                    "text": text,
                                    "title": obj.get("title", ""),
                                    "url": obj.get("url", ""),
                                    "parent_id": obj.get("parent_id", "")
                                })
                                
                                if len(chunks) >= k:
                                    break

            logger.info(f"Retrieved {len(chunks)} chunks for query")
            return chunks[:k]

        except Exception as e:
            logger.error(f"Search error: {e}", exc_info=True)
            return []

    def create_context(self, chunks: List[Dict[str, Any]]) -> str:
        if not chunks:
            return ""
        ctx = []
        for i, ch in enumerate(chunks, 1):
            title = ch.get('title', 'Unknown')
            text = ch.get('text', '')
            url = ch.get('url', 'N/A')
            ctx.append(f"[Source {i}: {title} | URL: {url}]\n{text}")
        return "\n\n---\n\n".join(ctx)

    def generate_with_llm(self, prompt, stream=False):
        url = self.valves.LLM_MODEL_URL
        headers = {
            "Authorization": f"Bearer {self.valves.LLM_MODEL_API_KEY}",
            "Content-Type": "application/json",
        }        
        try:
            r = requests.post(
                f"{url}/chat/completions",
                json= {
                    "model": self.valves.LLM_MODEL_NAME, 
                    "messages": [
                        {"role": "system", "content": prompt},
                    ],
                    "temperature": 0.1
                },
                headers=headers,
                stream=True,         
                timeout=120,
            )
            r.raise_for_status()
            if stream:
                def generate():
                    for line in r.iter_lines():
                        if line:
                            import json
                            try:
                                chunk = json.loads(line)
                                if 'response' in chunk:
                                    yield chunk['response']
                            except Exception:
                                continue
                return generate()
            else:
                return r.json().get("response", "No response generated")
        except Exception as e:
            return f"Error: {e}"

    def pipes(self) -> List[dict]:
        return [
            {
                "id": "rag.documentation",
                "name": "RAG Documentation"
            }
        ]
    
    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> Union[str, Generator, Iterator]:
        logger.info(f"pipe:{__name__}")
        logger.info(f"User message: {user_message}")

        # --- Command trigger for ingestion ---
        if "scrape" in user_message.lower() or "update database" in user_message.lower():
            import asyncio
            try:
                asyncio.run(self.scrape_and_ingest(max_manuals=1))
                return "Scraping and ingestion complete! You can now ask questions about the new documentation."
            except Exception as e:
                return f"Failed to run scraper: {e}"
       
        try:
            # Check if streaming is requested
            stream = body.get("stream", False)        
            chunks = self.search_chunks(user_message)
            if not chunks:
                return "I couldn't find any relevant information in the documentation database. Please try rephrasing your question or ensure the documentation has been indexed in Weaviate."
            context = self.create_context(chunks)
            enhanced_prompt = f"""You are a helpful technical documentation assistant. Answer the user's question based on the provided documentation context.

DOCUMENTATION CONTEXT:
{context}

INSTRUCTIONS:
- Provide detailed, step-by-step answers when applicable
- Use specific information from the documentation
- If the documentation doesn't fully answer the question, acknowledge this
- Be comprehensive and include all relevant details
- Do not mention "Source X" - integrate information naturally

USER QUESTION:
{user_message}

DETAILED ANSWER:"""
            # 4. Generate response with LLM
            logger.info(f"Generating response with context from {len(chunks)} chunks")
            response = self.generate_with_llm(enhanced_prompt, stream=stream)            
            return response            
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            return f"An error occurred while processing your request: {str(e)}"  