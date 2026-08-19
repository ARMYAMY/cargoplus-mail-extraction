from fastapi import APIRouter
from app.api.admin.tenants import router as tenants_router
from app.api.admin.recharge import router as recharge_router
from app.api.admin.tasks import router as tasks_router
from app.api.admin.stats import router as stats_router
from app.api.admin.billing import router as billing_router
from app.api.admin.llm_config import router as llm_config_router

admin_router = APIRouter()
admin_router.include_router(tenants_router, tags=["Admin - Tenants"])
admin_router.include_router(recharge_router, tags=["Admin - Billing & Recharge"])
admin_router.include_router(tasks_router, tags=["Admin - Tasks"])
admin_router.include_router(stats_router, tags=["Admin - Statistics"])
admin_router.include_router(billing_router, tags=["Admin - Billing & Transactions"])
admin_router.include_router(llm_config_router, tags=["Admin - LLM Configuration"])

