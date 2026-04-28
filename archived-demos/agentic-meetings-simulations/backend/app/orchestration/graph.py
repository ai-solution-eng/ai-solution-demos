
import structlog
from langgraph.graph import END, START, StateGraph

from app.models.roles import RoleAgent
from app.orchestration.agents import create_role_agent_node
from app.orchestration.state import MeetingState
from app.orchestration.supervisor import supervisor_node

logger = structlog.get_logger(__name__)

def build_meeting_graph(attendees: dict[str, RoleAgent]) -> StateGraph:
    """Builds the supervisor-led StateGraph based on selected attendees."""
    builder = StateGraph(MeetingState)

    # Add supervisor
    builder.add_node("supervisor", supervisor_node)

    # Add agents and edge back to supervisor
    for agent_id in attendees.keys():
        node_func = create_role_agent_node(agent_id)
        builder.add_node(agent_id, node_func)
        builder.add_edge(agent_id, "supervisor")

    builder.add_edge(START, "supervisor")

    # Routing logic from supervisor
    def router(state: MeetingState) -> str:
        next_speaker = state.get("next_speaker", "FINISH")
        if next_speaker == "FINISH":
            return "FINISH"
        if next_speaker not in attendees:
            logger.warning("invalid_router_target", target=next_speaker)
            return "FINISH"
        return next_speaker

    valid_targets = {id: id for id in attendees.keys()}
    valid_targets["FINISH"] = END

    builder.add_conditional_edges(
        "supervisor",
        router,
        valid_targets
    )

    return builder
