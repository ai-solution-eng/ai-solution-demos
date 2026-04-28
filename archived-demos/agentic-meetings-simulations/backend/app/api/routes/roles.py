from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.domain.response import APIResponse
from app.domain.roles import RoleAgentCreate, RoleAgentResponse, RoleAgentUpdate
from app.models.roles import RoleAgent

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.get("", response_model=APIResponse)
async def list_roles(session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(RoleAgent).order_by(RoleAgent.display_name)
    result = await session.execute(query)
    roles = result.scalars().all()

    return APIResponse(
        status="success",
        data=[RoleAgentResponse.model_validate(r).model_dump() for r in roles]
    )

@router.post("", response_model=APIResponse)
async def create_role(role_in: RoleAgentCreate, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    new_role = RoleAgent(**role_in.model_dump())
    session.add(new_role)
    await session.commit()
    await session.refresh(new_role)

    logger.info("role_created", role_id=str(new_role.id))
    return APIResponse(
        status="success",
        data=RoleAgentResponse.model_validate(new_role).model_dump()
    )

@router.get("/{role_id}", response_model=APIResponse)
async def get_role(role_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(RoleAgent).where(RoleAgent.id == role_id)
    result = await session.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    return APIResponse(
        status="success",
        data=RoleAgentResponse.model_validate(role).model_dump()
    )

@router.put("/{role_id}", response_model=APIResponse)
async def update_role(role_id: UUID, role_in: RoleAgentUpdate, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(RoleAgent).where(RoleAgent.id == role_id)
    result = await session.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    update_data = role_in.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(role, key, value)

    await session.commit()
    await session.refresh(role)
    return APIResponse(
        status="success",
        data=RoleAgentResponse.model_validate(role).model_dump()
    )
