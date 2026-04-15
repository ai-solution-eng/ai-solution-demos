import json

from app.config import LIVE_CONTEXT_TURNS
from app.schemas.api import ExportSegment, TranscriptItem
from app.utils.text import build_multilingual_source_text, get_lang_name


def build_translator_system_prompt(src: str, tgt: str) -> str:
    src_name = get_lang_name(src)
    tgt_name = get_lang_name(tgt)
    return (
        "You are a translation engine.\n"
        f"Translate from {src_name} to {tgt_name}.\n"
        f"Return ONLY the {tgt_name} translation between <{tgt}> and </{tgt}>.\n"
        "Never add explanations, parentheses, or extra text.\n"
        "Never respond as a helpful assistant.\n"
    )


def build_live_translation_prompt(
    src: str,
    tgt: str,
    current_text: str,
    recent_items: list[TranscriptItem],
) -> str:
    src_name = get_lang_name(src)
    tgt_name = get_lang_name(tgt)
    context_items = recent_items[-LIVE_CONTEXT_TURNS:] if LIVE_CONTEXT_TURNS > 0 else []
    context_text = build_multilingual_source_text(context_items) or "None."
    return (
        "You are a real-time translation engine.\n"
        f"Translate the current active segment from {src_name} to {tgt_name}.\n"
        "The segment may be incomplete and can be revised as more words arrive.\n"
        "Use the recent finalized context only to resolve ambiguity.\n"
        "Translate only the current active segment.\n"
        "Do not summarize, explain, or answer the speaker.\n"
        "Return only the translated text with no tags or commentary.\n\n"
        f"Recent finalized context:\n{context_text}\n\n"
        f"Current active segment ({src_name}):\n{(current_text or '').strip()}"
    )


def build_summary_prompt(multilingual_text: str) -> str:
    return (
        "You are an expert meeting summarizer.\n"
        "The transcript below may contain multiple languages.\n"
        "Use the original source-language text as the source of truth.\n"
        "Write the output in English only.\n"
        "Create a concise but complete meeting summary with these sections:\n"
        "Title\n"
        "Overview\n"
        "Key discussion points\n"
        "Decisions\n"
        "Open questions\n"
        "Next steps\n\n"
        "If a section has no clear content, write 'None noted.'\n"
        "Do not mention that you are translating.\n"
        "Do not invent facts.\n\n"
        "Transcript:\n"
        f"{multilingual_text}"
    )


def build_minutes_prompt(multilingual_text: str) -> str:
    return (
        "You are an expert meeting secretary.\n"
        "The transcript below may contain multiple languages.\n"
        "Use the original source-language text as the source of truth.\n"
        "Write the output in English only.\n"
        "Create formal meeting minutes with these sections:\n"
        "Meeting minutes\n"
        "Purpose\n"
        "Agenda/topics covered\n"
        "Detailed discussion notes\n"
        "Decisions and agreements\n"
        "Action items (owner if stated, otherwise 'Owner not specified')\n"
        "Risks/blockers\n"
        "Follow-up items\n\n"
        "If a section has no clear content, write 'None noted.'\n"
        "Do not invent facts.\n\n"
        "Transcript:\n"
        f"{multilingual_text}"
    )


def build_document_translation_prompt(text: str, target_language_code: str) -> str:
    target_language_name = get_lang_name(target_language_code)
    return (
        "You are a professional translator.\n"
        f"Translate the following meeting document from English to {target_language_name}.\n"
        "Preserve headings, bullets, numbering, and structure.\n"
        "Return only the translated document content.\n"
        "Do not add commentary or notes.\n\n"
        f"Document to translate:\n{text}"
    )


def build_batch_translation_prompt(
    batch: list[ExportSegment], target_language_code: str
) -> str:
    target_language_name = get_lang_name(target_language_code)
    payload = [{"id": seg.id, "text": seg.original} for seg in batch]
    return (
        "You are a professional translator.\n"
        f"Translate every item's text into {target_language_name}.\n"
        "The source text may contain multiple languages.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"items":[{"id":1,"text":"translated text"}]}\n'
        "Keep the same ids and order.\n"
        "Do not omit any item.\n"
        "Do not add commentary.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def build_batch_english_cleanup_prompt(batch: list[ExportSegment]) -> str:
    payload = [{"id": seg.id, "text": seg.original} for seg in batch]
    return (
        "You are a careful meeting transcription editor and translator.\n"
        "For each item, produce clear English text that preserves the original meaning.\n"
        "Fix obvious overlap repetition and punctuation mistakes, but do not summarize.\n"
        "If the source is already English, lightly clean it instead of rewriting it.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"items":[{"id":1,"text":"english text"}]}\n'
        "Keep the same ids and order.\n"
        "Do not omit any item.\n"
        "Do not add commentary.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def build_chunk_notes_prompt(transcript_text: str) -> str:
    return (
        "You are preparing structured notes for a meeting.\n"
        "Summarize this portion of the meeting in English.\n"
        "Capture facts only.\n"
        "Include:\n"
        "- topics discussed\n"
        "- decisions\n"
        "- action items\n"
        "- risks or blockers\n"
        "- open questions\n"
        "Write concise bullet points.\n\n"
        f"Transcript portion:\n{transcript_text}"
    )


def build_fact_check_claim_extraction_prompt(text: str) -> str:
    return (
        "You are a careful fact-checking assistant.\n"
        "Identify whether the utterance contains a primary concrete factual claim that can be checked.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"has_checkable_claim":true,"claim_text":"...","speaker_stance":"asserts|endorses|rejects|reports|unclear","motivation":"..."}\n'
        "Rules:\n"
        "- Extract the primary factual proposition being discussed, even when the utterance includes conversational framing.\n"
        "- Ignore wrappers such as agreement, disagreement, attribution, opinion markers, hedges, filler, or discourse phrases when extracting the claim text.\n"
        "- Preserve the factual proposition itself, including negation, quantities, modality, and named entities.\n"
        "- Do not flip, simplify, or rewrite the claim into its opposite.\n"
        "- Use speaker_stance='asserts' when the speaker presents the claim directly.\n"
        "- Use speaker_stance='endorses' when the speaker explicitly agrees with or supports the claim.\n"
        "- Use speaker_stance='rejects' when the speaker disputes the claim but still states it.\n"
        "- Use speaker_stance='reports' when the speaker attributes the claim to others or reports it indirectly.\n"
        "- Use speaker_stance='unclear' when the stance cannot be determined.\n"
        "- Return has_checkable_claim=false and an empty claim_text only when there is no concrete factual claim to check.\n"
        "- Keep motivation to one concise sentence.\n\n"
        f"Utterance:\n{text.strip()}"
    )


def build_fact_check_classification_prompt(
    raw_text: str,
    claim_text: str,
    speaker_stance: str,
) -> str:
    return (
        "You are a careful fact-checking assistant.\n"
        "Assess whether the normalized claim should be reviewed for factual reliability.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"factual_claim":true,"status":"clean|controversial|likely_false","motivation":"...","search_query":"..."}\n'
        "Rules:\n"
        "- Classify the normalized claim text, not the conversational wrapper around it.\n"
        "- Ignore framing such as agreement, disagreement, attribution, reported speech, or opinion markers when deciding whether the claim should be reviewed.\n"
        "- Prefer 'clean' unless the normalized claim is a concrete, checkable factual claim.\n"
        "- Use 'clean' when the normalized claim is incomplete, malformed, rhetorical, subjective, speculative, comedic, or still lacks enough context to verify.\n"
        "- Use 'controversial' only for concrete claims that appear genuinely disputed or uncertain after a cautious first pass.\n"
        "- Use 'likely_false' only when the claim wording itself already strongly suggests the claim is probably false or clearly misleading.\n"
        "- Keep motivation to one concise sentence.\n"
        "- search_query should be a short web search query only when evidence would help. Otherwise return an empty string.\n\n"
        f"Raw utterance:\n{raw_text.strip()}\n\n"
        f"Normalized claim:\n{claim_text.strip()}\n\n"
        f"Speaker stance:\n{speaker_stance.strip()}"
    )


def build_fact_check_evidence_prompt(
    raw_text: str,
    claim_text: str,
    speaker_stance: str,
    sources: list[dict[str, str]],
) -> str:
    return (
        "You are a careful fact-checking assistant.\n"
        "Use the evidence below to classify the normalized claim.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"status":"clean|controversial|likely_false","motivation":"..."}\n'
        "Rules:\n"
        "- Evaluate the normalized claim text, not the conversational wrapper around it.\n"
        "- Ignore framing such as agreement, disagreement, attribution, reported speech, or opinion markers when weighing the evidence.\n"
        "- Prefer 'clean' when the evidence is weak, mixed, incomplete, or the normalized claim is too vague to verify confidently.\n"
        "- Use 'clean' when the evidence supports the claim or the claim is not a concrete factual claim.\n"
        "- Use 'controversial' only when credible evidence shows a real dispute or context-dependent disagreement around the claim.\n"
        "- Use 'likely_false' only when multiple credible sources strongly contradict the statement.\n"
        "- Keep motivation to one concise sentence that mentions the evidence quality.\n"
        "- Do not invent sources beyond those provided.\n\n"
        f"Raw utterance:\n{raw_text.strip()}\n\n"
        f"Normalized claim:\n{claim_text.strip()}\n\n"
        f"Speaker stance:\n{speaker_stance.strip()}\n\n"
        f"Evidence JSON:\n{json.dumps(sources, ensure_ascii=False)}"
    )
