import json
import re

import structlog
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.orchestration.state import MeetingState
from app.orchestration.tools import create_retrieval_tool
from app.orchestration.prompts import DEFAULT_AGENT_PROMPT

logger = structlog.get_logger(__name__)

def create_role_agent_node(agent_id: str):
    """Creates a LangGraph node function localized for a specific agent."""
    async def agent_node(state: MeetingState, config: RunnableConfig) -> dict:
        attendees = config["configurable"]["attendees"]
        my_role = attendees.get(agent_id)
        if not my_role:
            return {}

        model_settings = config["configurable"]["model_settings"]

        llm_params = {
            "api_key": model_settings.inference_api_key,
            "base_url": model_settings.inference_endpoint,
            "model": model_settings.inference_model_name,
            "temperature": 0.7,
            "timeout": 60
        }

        if getattr(model_settings, "inference_ignore_tls", False):
            from app.core.network import get_http_client, get_sync_http_client
            llm_params["http_client"] = get_sync_http_client(ignore_tls=True)
            llm_params["http_async_client"] = get_http_client(ignore_tls=True)

        llm = ChatOpenAI(**llm_params)

        retrieval_tool = create_retrieval_tool(
            agent_id=str(my_role.id),
            meeting_id=state.get("meeting_id"),
            library_access=my_role.allowed_shared_library_access
        )
        tools = [retrieval_tool]

        raw_agent_prompt = getattr(model_settings, "agent_prompt", None) or DEFAULT_AGENT_PROMPT
        
        # Create attendee list for the agent
        other_attendees = [
            f"- {a.display_name} ({a.title}, {a.department})" 
            for aid, a in attendees.items() if aid != agent_id
        ]
        attendee_list_str = "\n".join(other_attendees) if other_attendees else "No other participants."

        # Replace placeholders: {{DISPLAY_NAME}}, {{TITLE}}, {{DEPARTMENT}}, {{SUMMARY}}, {{TONE}}, {{COLLABORATION_STYLE}}, {{OBJECTIVE}}, {{AGENDA}}, {{ATTENDEE_LIST}}
        sys_prompt = (
            raw_agent_prompt
                .replace("{{DISPLAY_NAME}}", my_role.display_name)
                .replace("{{TITLE}}", my_role.title)
                .replace("{{DEPARTMENT}}", my_role.department)
                .replace("{{SUMMARY}}", my_role.summary)
                .replace("{{TONE}}", ", ".join(my_role.tone) if isinstance(my_role.tone, list) else (my_role.tone or ""))
                .replace("{{COLLABORATION_STYLE}}", my_role.collaboration_style)
                .replace("{{OBJECTIVE}}", state.get('objective', ''))
                .replace("{{AGENDA}}", state.get('agenda', ''))
                .replace("{{ATTENDEE_LIST}}", attendee_list_str)
        )
        historical_messages = [
            msg for msg in state.get("messages", [])
            if isinstance(msg, AIMessage) and msg.content and msg.content.startswith("[")
        ]
        
        agent = create_react_agent(llm, tools, prompt=sys_prompt)
        response = await agent.ainvoke({"messages": historical_messages})

        # Filter new messages appended by this agent's ReAct execution
        new_msgs = response["messages"][len(historical_messages):]

        # Find the last AIMessage that has non-empty content
        ai_messages_with_content = [
            msg for msg in new_msgs
            if isinstance(msg, AIMessage) and msg.content and msg.content.strip()
        ]

        if ai_messages_with_content:
            final_message = ai_messages_with_content[-1]
        else:
            # Fallback if the agent only did tool calls or returned empty content
            # We look for ANY AIMessage first, then fall back to a generic string
            all_ai_messages = [msg for msg in new_msgs if isinstance(msg, AIMessage)]
            if all_ai_messages:
                 final_message = all_ai_messages[-1]
            else:
                 final_message = AIMessage(content="[No speech recorded. Performing background analysis.]")

        # Collect internal reasoning for trust debug mode
        internal_events = []
        parsed_thought = ""

        # Check for <thought> or <thinking> tag in the final message content
        if final_message.content:
            thought_match = re.search(r'<(?:thought|thinking)>(.*?)</(?:thought|thinking)>', final_message.content, re.DOTALL)
            if thought_match:
                parsed_thought = thought_match.group(1).strip()
                # Remove thought from final_message.content
                final_message.content = re.sub(r'<(?:thought|thinking)>.*?</(?:thought|thinking)>', '', final_message.content, flags=re.DOTALL).strip()

        for msg in new_msgs:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                internal_events.append({"type": "tool_call", "tool_calls": msg.tool_calls})
            elif isinstance(msg, ToolMessage):
                internal_events.append({"type": "tool_result", "name": msg.name, "content": msg.content})

        # Combined reasoning: parsed thought + tool logs
        combined_reasoning = ""
        if parsed_thought:
            combined_reasoning += f"AGENT THOUGHT PROCESS:\n{parsed_thought}\n\n"
        
        if internal_events:
            combined_reasoning += "INTERNAL ACTIONS & TOOLS:\n" + json.dumps(internal_events, indent=2)
        
        if not combined_reasoning:
            combined_reasoning = "No internal tools used."

        # Format the final output to inject the agent's name for transcript clarity
        cleaned_content = final_message.content.strip() if final_message.content else ""
        
        # Remove any hallucinated prefixes from the LLM to prevent double-prefixing
        cleaned_content = re.sub(r'^\[.*?\]\s*', '', cleaned_content).strip()

        if not cleaned_content:
             # If it was an AI message with tool calls but no text
             cleaned_content = "(performing internal analysis...)"

        public_content = f"[{my_role.display_name} - {my_role.title}] {cleaned_content}"

        event = {
            "type": "agent_spoke",
            "agent_id": str(my_role.id),
            "content": public_content,
            "private_reasoning": combined_reasoning,
            "raw_content": final_message.content
        }

        return {
            "messages": [AIMessage(content=public_content)],
            "event_log": [event],
            "active_agent_id": str(my_role.id)
        }

    return agent_node
