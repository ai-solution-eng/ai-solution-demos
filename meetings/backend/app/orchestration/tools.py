import uuid
from typing import Any, Callable

import structlog
from langchain_core.tools import tool

from app.core import database
from app.services.vector_search import semantic_search

logger = structlog.get_logger(__name__)

def create_retrieval_tool(agent_id: str, meeting_id: str, library_access: bool) -> Any:
    @tool
    async def retrieve_documents(
        query: str,
        search_company_library: bool = True,
        search_private_library: bool = True
    ) -> str:
        """Search shared library or private library for facts and evidence to support arguments.

        Returns a list of exact matching excerpts with physical locations.
        You MUST assess relevance to the meeting objective and current conversation before using the results. If the extracted chunks are completely irrelevant, do NOT mention them.
        If relevant, you MUST quote them exactly if you use them.
        """
        scopes = []
        if search_company_library and library_access:
            scopes.append("company")
            scopes.append("meeting")
        if search_private_library:
            scopes.append("agent")

        assert database.async_session_maker is not None
        async with database.async_session_maker() as session:
            try:
                results = await semantic_search(
                    query_text=query,
                    library_scopes=scopes,
                    limit=3,
                    session=session,
                    meeting_id=uuid.UUID(meeting_id) if meeting_id else None,
                    owner_agent_id=uuid.UUID(agent_id) if agent_id else None
                )

                if not results:
                    return "No matching documents found."

                formatted_results = []
                for r in results:
                    location = r['page_number']
                    loc_str = f"p.{location}" if str(location).isdigit() else f"{location}"

                    formatted = (
                        f"Source: {r['document_name']} {loc_str}\n"
                        f"Excerpt: \"{r['text']}\"\n"
                        f"Scope: {r['library_scope']}"
                    )
                    formatted_results.append(formatted)

                return "\n\n---\n\n".join(formatted_results)
            except Exception as e:
                logger.error("tool_retrieval_error", error=str(e))
                return f"Error retrieving documents: {str(e)}"

    return retrieve_documents
