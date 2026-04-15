import json
import re
import time
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.prompts import (
    build_fact_check_claim_extraction_prompt,
    build_fact_check_classification_prompt,
    build_fact_check_evidence_prompt,
)
from app.schemas.api import FactCheckResult, FactCheckSource

SEARCH_FACT_CHECK_STATUSES = {"controversial", "likely_false"}
VISIBLE_FACT_CHECK_STATUSES = {"likely_false"}
FACT_CHECK_STATUSES = {"clean", "controversial", "likely_false", "error"}
CLAIM_STANCES = {"asserts", "endorses", "rejects", "reports", "unclear"}
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def fact_check_is_flagged(result: dict[str, Any] | None) -> bool:
    status = str((result or {}).get("status") or "").strip().lower()
    return status in VISIBLE_FACT_CHECK_STATUSES


def should_fact_check_text(text: str) -> bool:
    cleaned = " ".join((text or "").split())
    if len(cleaned) < 12:
        return False
    words = cleaned.split()
    return len(words) >= 3


def normalize_fact_check_result(payload: dict[str, Any] | None, *, claim_text: str = "") -> dict[str, Any]:
    data = payload or {}
    status = str(data.get("status") or "clean").strip().lower()
    if status not in FACT_CHECK_STATUSES:
        status = "clean"
    sources_payload = data.get("sources") or []
    sources: list[FactCheckSource] = []
    if isinstance(sources_payload, list):
        for source in sources_payload:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            title = str(source.get("title") or "").strip()
            snippet = str(source.get("snippet") or "").strip()
            if not (url or title or snippet):
                continue
            sources.append(FactCheckSource(title=title, url=url, snippet=snippet))
    result = FactCheckResult(
        status=status,
        motivation=str(data.get("motivation") or "").strip(),
        provider=str(data.get("provider") or "").strip(),
        claim_text=str(data.get("claim_text") or claim_text or "").strip(),
        checked_at=int(data.get("checked_at") or 0) or None,
        sources=sources,
    )
    return result.model_dump()


def normalize_claim_extraction_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload or {}
    has_checkable_claim = bool(data.get("has_checkable_claim"))
    claim_text = " ".join(str(data.get("claim_text") or "").split())
    speaker_stance = str(data.get("speaker_stance") or "unclear").strip().lower()
    if speaker_stance not in CLAIM_STANCES:
        speaker_stance = "unclear"
    motivation = str(data.get("motivation") or "").strip()
    if not claim_text:
        has_checkable_claim = False
    return {
        "has_checkable_claim": has_checkable_claim,
        "claim_text": claim_text,
        "speaker_stance": speaker_stance,
        "motivation": motivation,
    }


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}


async def _run_json_prompt(
    *,
    llm_client: AsyncOpenAI,
    llm_model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    chat_resp = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=max_tokens,
        stop=["\n\n```"],
    )
    return _extract_json_payload(chat_resp.choices[0].message.content or "")


async def search_tavily(
    *,
    query: str,
    api_key: str,
    max_sources: int,
) -> list[dict[str, str]]:
    if not query or not api_key:
        return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "advanced",
                "include_answer": False,
                "max_results": max_sources,
            },
        )
        response.raise_for_status()
        payload = response.json()
    sources: list[dict[str, str]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("content") or "").strip()
        if not (url or title or snippet):
            continue
        sources.append({"title": title, "url": url, "snippet": snippet})
    return sources[:max_sources]


async def fact_check_text(
    *,
    text: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
    tavily_api_key: str = "",
    max_sources: int = 3,
) -> dict[str, Any]:
    cleaned = " ".join((text or "").split())
    checked_at = int(time.time() * 1000)
    if not should_fact_check_text(cleaned):
        return normalize_fact_check_result(
            {
                "status": "clean",
                "motivation": "No concrete factual claim detected.",
                "provider": "llm",
                "claim_text": cleaned,
                "checked_at": checked_at,
                "sources": [],
            },
            claim_text=cleaned,
        )

    extracted = normalize_claim_extraction_result(
        await _run_json_prompt(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_fact_check_claim_extraction_prompt(cleaned),
            max_tokens=180,
        )
    )
    claim_text = extracted.get("claim_text") or ""
    speaker_stance = extracted.get("speaker_stance") or "unclear"
    extraction_motivation = extracted.get("motivation") or ""

    if not extracted.get("has_checkable_claim") or not claim_text:
        return normalize_fact_check_result(
            {
                "status": "clean",
                "motivation": extraction_motivation or "No concrete factual claim detected.",
                "provider": "llm",
                "claim_text": "",
                "checked_at": checked_at,
                "sources": [],
            },
            claim_text="",
        )

    initial = await _run_json_prompt(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_fact_check_classification_prompt(
            cleaned,
            claim_text,
            speaker_stance,
        ),
        max_tokens=220,
    )
    factual_claim = bool(initial.get("factual_claim"))
    status = str(initial.get("status") or "clean").strip().lower()
    motivation = str(initial.get("motivation") or "").strip()
    search_query = str(initial.get("search_query") or "").strip()

    if not factual_claim:
        status = "clean"
        motivation = motivation or "No concrete factual claim detected."

    result: dict[str, Any] = {
        "status": status if status in FACT_CHECK_STATUSES else "clean",
        "motivation": motivation,
        "provider": "llm",
        "claim_text": claim_text,
        "checked_at": checked_at,
        "sources": [],
    }

    if result["status"] not in SEARCH_FACT_CHECK_STATUSES or not tavily_api_key:
        return normalize_fact_check_result(result, claim_text=claim_text)

    query = search_query or claim_text
    sources = await search_tavily(query=query, api_key=tavily_api_key, max_sources=max_sources)
    if not sources:
        return normalize_fact_check_result(result, claim_text=claim_text)

    evidence_result = await _run_json_prompt(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_fact_check_evidence_prompt(
            cleaned,
            claim_text,
            speaker_stance,
            sources,
        ),
        max_tokens=180,
    )
    result.update(
        {
            "status": str(evidence_result.get("status") or result["status"]).strip().lower(),
            "motivation": str(evidence_result.get("motivation") or result["motivation"]).strip(),
            "provider": "llm+tavily",
            "sources": sources,
        }
    )
    return normalize_fact_check_result(result, claim_text=claim_text)
