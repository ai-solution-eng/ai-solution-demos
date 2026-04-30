import asyncio

import structlog
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.orchestration.state import MeetingState

logger = structlog.get_logger(__name__)

class SupervisorDecision(BaseModel):
    next_speaker: str = Field(description="The ID of the next agent to speak, or 'FINISH' to end the meeting.")
    reasoning: str = Field(description="Why this speaker was chosen.")

from typing import Any
from app.orchestration.prompts import DEFAULT_SUPERVISOR_PROMPT

async def supervisor_node(state: MeetingState, config: RunnableConfig) -> dict[str, Any]:
    """Decides the next speaker or if the meeting should finish."""
    current_turn = state.get("current_turn", 0)
    turn_limit = state.get("turn_limit", 50)
    meeting_id = state.get("meeting_id")

    logger.info("supervisor_running", curr_turn=current_turn, limit=turn_limit, meeting_id=meeting_id)

    # 1. Termination Checks
    is_terminated = state.get("stop_requested") or state.get("terminated")
    is_limit_reached = current_turn >= turn_limit

    if is_terminated or is_limit_reached:
        reason = "Meeting terminated by user request." if is_terminated else f"Meeting reached turn limit ({turn_limit})."
        logger.info("supervisor_concluding", reason=reason)
        content = "[Supervisor] Concluded the meeting."
        return {
            "next_speaker": "FINISH",
            "reasoning": reason,
            "messages": [AIMessage(content=content)],
            "event_log": [{
                "type": "supervisor_spoke",
                "agent_id": "supervisor",
                "content": content,
                "reasoning": reason,
                "private_reasoning": reason,
                "is_conclusion": True
            }],
            "final_summary": reason
        }

    # 2. LLM Setup
    model_settings = config["configurable"]["model_settings"]
    attendees = config["configurable"]["attendees"]

    llm_params = {
        "api_key": model_settings.inference_api_key,
        "base_url": model_settings.inference_endpoint,
        "model": model_settings.inference_model_name,
        "temperature": 0.1, # Lower temperature for better structural adherence
        "timeout": 90
    }

    if getattr(model_settings, "inference_ignore_tls", False):
        from app.core.network import get_http_client, get_sync_http_client
        llm_params["http_client"] = get_sync_http_client(ignore_tls=True)
        llm_params["http_async_client"] = get_http_client(ignore_tls=True)

    llm = ChatOpenAI(**llm_params)

    attendee_options = []
    for agent_id, agent in attendees.items():
        attendee_options.append(f"ID: '{agent_id}' | Name: {agent.display_name} | Role: {agent.title} | Department: {agent.department}")

    valid_ids = list(attendees.keys()) + ["FINISH"]

    # 2.5 Dynamic Schema for better model adherence
    # We create a dynamic Literal type for the next_speaker field
    from typing import Literal
    from pydantic import create_model
    
    # Create a dynamic model where next_speaker is restricted to valid_ids
    # This helps models like Gemini/OpenAI stick to the allowed options
    DynamicSupervisorDecision = create_model(
        "SupervisorDecision",
        next_speaker=(str, Field(description=f"Selection from: {', '.join(valid_ids)}")),
        reasoning=(str, Field(description="Detailed explanation for this choice."))
    )

    # 2.6 Prompt Selection & Placeholder replacement
    raw_prompt = getattr(model_settings, "supervisor_prompt", None) or DEFAULT_SUPERVISOR_PROMPT
    
    # Placeholders: {{OBJECTIVE}}, {{AGENDA}}, {{ATTENDEE_LIST}}
    attendee_list_str = chr(10).join(attendee_options)
    
    final_sys_prompt = (
        raw_prompt.replace("{{ATTENDEE LIST}}", attendee_list_str)
                  .replace("{{OBJECTIVE}}", "{objective}")
                  .replace("{{AGENDA}}", "{agenda}")
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", final_sys_prompt),
        ("placeholder", "{messages}"),
    ])

    chain = prompt | llm.with_structured_output(DynamicSupervisorDecision)

    # 3. Execution with Retry
    for attempt in range(3):
        try:
            logger.info("supervisor_invoking_llm", meeting_id=meeting_id, attempt=attempt)
            # Use only public conversational history (chatter) to decide next speaker
            historical_messages = [
                msg for msg in state.get("messages", [])
                if isinstance(msg, AIMessage) and msg.content and msg.content.startswith("[")
            ]

            decision = await chain.ainvoke({
                "objective": state.get("objective", ""),
                "agenda": state.get("agenda", ""),
                "messages": historical_messages
            })

            if decision is None:
                raise ValueError("LLM returned empty or malformed structured output.")

            next_speaker = decision.next_speaker.strip()
            reasoning = decision.reasoning

            # Validation with Fallback matching
            if next_speaker not in valid_ids:
                logger.warning("invalid_id_from_supervisor", received=next_speaker)
                
                # attempt to find by name or role matches if model returned a string instead of ID
                normalized_received = next_speaker.lower()
                found_id = None
                for aid, a in attendees.items():
                    # check display name, title, or if ID contains the received string
                    if (normalized_received in a.display_name.lower() or 
                        normalized_received in a.title.lower() or 
                        normalized_received in aid.lower()):
                        found_id = aid
                        break
                
                if found_id:
                    logger.info("supervisor_id_recovered", original=next_speaker, recovered=found_id)
                    next_speaker = found_id
                else:
                    logger.warning("supervisor_id_unrecoverable_fallback_to_finish")
                    next_speaker = "FINISH"

            is_finish = (next_speaker == "FINISH")
            content = f"[Supervisor] {'Concluded the meeting.' if is_finish else f'Selected next speaker: {next_speaker}'}"

            return {
                "next_speaker": next_speaker,
                "current_turn": current_turn + 1,
                "reasoning": reasoning,
                "messages": [AIMessage(content=content)],
                "event_log": [{
                    "type": "supervisor_spoke",
                    "agent_id": "supervisor",
                    "content": content,
                    "reasoning": reasoning,
                    "private_reasoning": reasoning,
                    "is_conclusion": is_finish
                }],
                "final_summary": reasoning if is_finish else None
            }

        except Exception as e:
            logger.error("supervisor_attempt_failed", error=str(e), attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(0.5) # Quick backoff
                continue

            # FINAL FALLBACK
            logger.error("supervisor_critically_failed_finishing_safe", meeting_id=meeting_id)
            err_msg = f"Supervisor encountered a technical error: {str(e)}. Meeting concluded for safety."
            fallback_content = "[Supervisor] System Error Encountered."
            return {
                "next_speaker": "FINISH",
                "reasoning": err_msg,
                "event_log": [{
                    "type": "supervisor_spoke",
                    "agent_id": "supervisor",
                    "content": fallback_content,
                    "reasoning": err_msg,
                    "private_reasoning": err_msg,
                    "is_conclusion": True
                }],
                "final_summary": err_msg
            }
