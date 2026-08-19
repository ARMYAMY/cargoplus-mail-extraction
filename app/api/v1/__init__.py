from fastapi import APIRouter
from app.api.v1.extract import router as extract_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.tenants import router as tenants_router
from app.api.v1.billing import router as billing_router
from app.api.v1.auth import router as auth_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(extract_router, tags=["Extraction"])
api_v1_router.include_router(tasks_router, tags=["Tasks"])
api_v1_router.include_router(tenants_router, tags=["Tenants"])
api_v1_router.include_router(billing_router, tags=["Billing"])
api_v1_router.include_router(auth_router, tags=["Authentication & Registration"])

