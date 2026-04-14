import operator
from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage


def merge_list(a: list[Any], b: list[Any]) -> list[Any]:
    if not a:
        return b
    if not b:
        return a
    return a + b

class MeetingState(TypedDict):
    meeting_id: str
    brief: str
    agenda: str
    objective: str
    expectations: str
    selected_attendee_ids: list[str]
    turn_limit: int
    current_turn: int

    # Message history
    messages: Annotated[Sequence[BaseMessage], operator.add]

    # Structured history of custom events pushed to websockets
    event_log: Annotated[list[dict[str, Any]], merge_list]

    active_agent_id: str | None
    stop_requested: bool
    terminated: bool
    final_summary: str | None

    # Next routing decision
    next_speaker: str | None
