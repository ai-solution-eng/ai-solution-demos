from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.domain.meetings import (
    MeetingTemplateCreate,
    MeetingTemplateResponse,
    MeetingTemplateUpdate,
)
from app.domain.response import APIResponse
from app.models.meetings import MeetingTemplate

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.get("", response_model=APIResponse)
async def list_templates(session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(MeetingTemplate).order_by(MeetingTemplate.name)
    result = await session.execute(query)
    templates = result.scalars().all()

    return APIResponse(
        status="success",
        data=[MeetingTemplateResponse.model_validate(t).model_dump() for t in templates]
    )

@router.post("", response_model=APIResponse)
async def create_template(template_in: MeetingTemplateCreate, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    new_template = MeetingTemplate(**template_in.model_dump(mode='json'))
    session.add(new_template)
    await session.commit()
    await session.refresh(new_template)

    return APIResponse(
        status="success",
        data=MeetingTemplateResponse.model_validate(new_template).model_dump()
    )

@router.get("/{template_id}", response_model=APIResponse)
async def get_template(template_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(MeetingTemplate).where(MeetingTemplate.id == template_id)
    result = await session.execute(query)
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return APIResponse(
        status="success",
        data=MeetingTemplateResponse.model_validate(template).model_dump()
    )

@router.put("/{template_id}", response_model=APIResponse)
async def update_template(template_id: UUID, template_in: MeetingTemplateUpdate, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(MeetingTemplate).where(MeetingTemplate.id == template_id)
    result = await session.execute(query)
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if template.is_builtin:
        raise HTTPException(status_code=403, detail="Built-in templates cannot be edited")

    update_data = template_in.model_dump(mode='json', exclude_unset=True)
    for key, value in update_data.items():
        setattr(template, key, value)

    await session.commit()
    await session.refresh(template)
    return APIResponse(
        status="success",
        data=MeetingTemplateResponse.model_validate(template).model_dump()
    )

@router.delete("/{template_id}", response_model=APIResponse)
async def delete_template(template_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(MeetingTemplate).where(MeetingTemplate.id == template_id)
    result = await session.execute(query)
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if template.is_builtin:
        raise HTTPException(status_code=403, detail="Built-in templates cannot be deleted")

    await session.delete(template)
    await session.commit()

    return APIResponse(status="success", message="Template deleted")
