from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from typing import List
from . import database, schemas_admin
from .core import get_logger, get_settings
from .kube import get_all_services, get_custom_object_list

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)

logger = get_logger(__name__)
settings = get_settings()


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/tables", response_model=List[str])
def get_tables():
    inspector = inspect(database.engine)
    return inspector.get_table_names()


@router.get("/tables/{table_name}")
def get_table_records(
    table_name: str, page: int = 1, per_page: int = 10, db: Session = Depends(get_db)
):
    inspector = inspect(database.engine)
    if table_name not in inspector.get_table_names():
        raise HTTPException(status_code=404, detail="Table not found")

    # Calculate offset
    offset = (page - 1) * per_page

    # Get total count
    count_query = text(f"SELECT COUNT(*) FROM {table_name}")
    total = db.execute(count_query).scalar()

    # Get column names to check if 'id' exists
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    has_id = "id" in columns

    # Build query with or without ORDER BY id
    if has_id:
        query = text(
            f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
    else:
        # Use first column for ordering if no id column
        first_col = columns[0] if columns else "*"
        query = text(
            f"SELECT * FROM {table_name} ORDER BY {first_col} LIMIT :limit OFFSET :offset"
        )

    try:
        result = db.execute(query, {"limit": per_page, "offset": offset})
        raw_records = [dict(row._mapping) for row in result]

        # Process records to handle binary data and other non-serializable types
        records = []
        for record in raw_records:
            processed_record = {}
            for key, value in record.items():
                if isinstance(value, bytes):
                    # For binary data, show size instead of content
                    processed_record[key] = f"<binary data: {len(value)} bytes>"
                elif isinstance(value, memoryview):
                    # Handle memoryview (another binary type)
                    processed_record[key] = f"<binary data: {len(value)} bytes>"
                elif value is None:
                    processed_record[key] = None
                else:
                    # Try to convert to string, fallback to repr
                    try:
                        processed_record[key] = (
                            str(value) if not isinstance(value, (dict, list)) else value
                        )
                    except:
                        processed_record[key] = repr(value)
            records.append(processed_record)

        return {
            "records": records,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/endpoints", response_model=List[object])
def get_endpoints():
    llm_endpoints = get_custom_object_list(
        "inferenceservices", "serving.kserve.io", "v1beta1"
    )
    logger.debug(llm_endpoints)
    return llm_endpoints
