from contextlib import asynccontextmanager
from decimal import Decimal
import logging
from pathlib import Path
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select, text
from app.config import settings
from app.core.limits import DEFAULT_TENANT_CONCURRENCY
from app.core.observability import (
    HTTP_IN_PROGRESS,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    api_metrics_payload,
)
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import ApiKey, Tenant
from app.api.v1 import api_v1_router
from app.api.admin import admin_router
from app.services.auth_service import generate_api_key_and_secret
from app.services.billing_service import BillingService
from app.services.queue_service import task_queue
from app.core.rate_limit import client_rate_limit_identity, consume_rate_limit

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cargo_service")

STATIC_DIR = Path(__file__).parent / "static"


async def seed_initial_demo_tenant():
    """Creates a default demo tenant on fresh installation for zero-friction testing."""
    async with AsyncSessionLocal() as db:
        stmt = select(Tenant)
        res = await db.execute(stmt)
        existing = res.scalars().first()
        if not existing:
            logger.info("Initializing default demo tenant...")
            tenant = Tenant(
                id="tenant_demo_001",
                name="宜运货代演示企业 (Demo)",
                contact_email="demo@cargoplus.com",
                unit_price=Decimal("0.5000"),
                max_concurrency=DEFAULT_TENANT_CONCURRENCY,
                balance=Decimal("100.0000"),
            )
            db.add(tenant)
            await db.commit()

            raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
            api_key = ApiKey(
                id="key_demo_001",
                tenant_id=tenant.id,
                name="Default Demo Key",
                key_prefix=prefix,
                key_hash=key_hash,
                raw_key=raw_key,
                api_secret=secret,
            )
            db.add(api_key)
            await db.commit()

            await BillingService.recharge_balance(
                db, tenant.id, Decimal("100.0000"), description="系统初始开户充值", operator="SYSTEM"
            )
            logger.info("Default demo tenant created: %s (API key is not written to logs)", tenant.name)


async def load_dynamic_system_config():
    """Loads runtime overrides (such as custom LLM Base URL and API Key) from database."""
    if settings.ENVIRONMENT.lower() == "production":
        # Production runtime configuration comes from deployment secrets and is
        # never replaced by plaintext values stored in the application database.
        return
    try:
        from app.models.system import SystemConfig
        async with AsyncSessionLocal() as db:
            stmt = select(SystemConfig)
            res = await db.execute(stmt)
            configs = {c.key: c.value for c in res.scalars().all()}
            if "LLM_BASE_URL" in configs and configs["LLM_BASE_URL"]:
                settings.LLM_BASE_URL = configs["LLM_BASE_URL"].strip()
            if "LLM_API_KEY" in configs and configs["LLM_API_KEY"]:
                settings.LLM_API_KEY = configs["LLM_API_KEY"].strip()
            if "LLM_MODEL" in configs and configs["LLM_MODEL"]:
                settings.LLM_MODEL = configs["LLM_MODEL"].strip()
            if "LLM_TIMEOUT_SECONDS" in configs and configs["LLM_TIMEOUT_SECONDS"]:
                try:
                    settings.LLM_TIMEOUT_SECONDS = int(configs["LLM_TIMEOUT_SECONDS"])
                except ValueError:
                    pass
            # Multimodal Vision Settings
            if "VISION_LLM_ENABLED" in configs:
                settings.VISION_LLM_ENABLED = configs["VISION_LLM_ENABLED"].lower() in {"1", "true", "yes"}
            if "VISION_LLM_MODEL" in configs and configs["VISION_LLM_MODEL"]:
                settings.VISION_LLM_MODEL = configs["VISION_LLM_MODEL"].strip()
            if "VISION_LLM_TIMEOUT_SECONDS" in configs and configs["VISION_LLM_TIMEOUT_SECONDS"]:
                try:
                    settings.VISION_LLM_TIMEOUT_SECONDS = int(configs["VISION_LLM_TIMEOUT_SECONDS"])
                except ValueError:
                    pass
            if "VISION_MAX_IMAGES_PER_TASK" in configs and configs["VISION_MAX_IMAGES_PER_TASK"]:
                try:
                    settings.VISION_MAX_IMAGES_PER_TASK = int(configs["VISION_MAX_IMAGES_PER_TASK"])
                except ValueError:
                    pass
    except Exception as exc:
        logger.warning("Failed to load dynamic system config: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting CargoPlus Extraction API Service...")
    settings.validate_security_settings()
    await init_db()
    await load_dynamic_system_config()
    from app.services.admin_job_service import AdminJobService
    await AdminJobService.mark_interrupted_jobs()
    if settings.SEED_DEMO_TENANT and settings.ENVIRONMENT.lower() != "production":
        await seed_initial_demo_tenant()
    await task_queue.start()
    
    # Start 90-day storage retention pruning worker
    from app.services.storage_service import StorageService
    import asyncio
    retention_task = asyncio.create_task(StorageService.start_retention_pruning_worker())
    
    yield
    # Shutdown
    logger.info("Stopping CargoPlus Extraction API Service...")
    retention_task.cancel()
    await asyncio.gather(retention_task, return_exceptions=True)
    await task_queue.stop()



is_production = settings.ENVIRONMENT.lower() == "production"

app = FastAPI(
    title="CargoPlus 货代邮件抽取 API 服务",
    description="基于 Skill V3 的工业级货代邮件与单证结构化抽取平台，具备多模态解析、多租户按次扣费 (0.50元/次)、异步削峰队列与 Webhook 回调推送。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if is_production else "/docs",
    redoc_url=None if is_production else "/redoc",
    openapi_url=None if is_production else "/openapi.json",
)

if is_production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Refreshed-Token"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Include Routers
app.include_router(api_v1_router)
app.include_router(admin_router)


@app.middleware("http")
async def protect_public_auth_endpoints(request, call_next):
    if not settings.AUTH_RATE_LIMIT_ENABLED or request.method != "POST":
        return await call_next(request)

    limits = {
        "/api/v1/auth/login": (
            "tenant-login",
            settings.TENANT_LOGIN_RATE_LIMIT_ATTEMPTS,
            settings.TENANT_LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        ),
        "/api/v1/auth/admin/login": (
            "admin-login",
            settings.ADMIN_LOGIN_RATE_LIMIT_ATTEMPTS,
            settings.ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        ),
        "/api/v1/auth/register": (
            "register",
            settings.REGISTER_RATE_LIMIT_ATTEMPTS,
            settings.REGISTER_RATE_LIMIT_WINDOW_SECONDS,
        ),
    }
    rule = limits.get(request.url.path)
    if rule is None:
        return await call_next(request)

    bucket, limit, window = rule
    try:
        allowed, retry_after = await consume_rate_limit(
            bucket,
            client_rate_limit_identity(request),
            limit,
            window,
        )
    except Exception:
        logger.exception("Authentication rate-limit backend is unavailable")
        return Response(
            content='{"detail":{"code":50310,"message":"Authentication service is temporarily unavailable"}}',
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": "10"},
        )
    if not allowed:
        return Response(
            content='{"detail":{"code":42910,"message":"Too many authentication attempts"}}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if is_production:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
    if request.url.path.startswith(("/api/", "/admin/")):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path in {"/", "/portal", "/reconciliation", "/login", "/register", "/manual", "/guide"}:
        # HTML 必须每次向服务端确认版本；JS/CSS 通过查询参数做版本隔离。
        # 否则浏览器可能组合“新版 HTML + 旧版 JS”，造成新按钮点击无响应。
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.middleware("http")
async def handle_sliding_session_renewal(request: Request, call_next):
    response = await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        try:
            from app.services.auth_service import verify_access_token, create_access_token
            claims = verify_access_token(token)
            if claims and 200 <= response.status_code < 400:
                now = int(time.time())
                exp = int(claims.get("exp", 0))
                iat = int(claims.get("iat", 0))
                total_ttl = exp - iat if (exp > iat) else settings.AUTH_TOKEN_TTL_SECONDS
                elapsed = now - iat
                # If more than 15% of token lifetime has elapsed, silently renew the token
                if elapsed > (total_ttl * 0.15):
                    refreshed = create_access_token(
                        claims["sub"],
                        role=claims.get("role", "tenant"),
                        expires_in=total_ttl,
                    )
                    response.headers["X-Refreshed-Token"] = refreshed
        except Exception:
            pass
    return response


@app.middleware("http")
async def record_http_metrics(request, call_next):
    if not settings.METRICS_ENABLED or request.url.path == "/metrics":
        return await call_next(request)
    method = request.method
    started = time.perf_counter()
    HTTP_IN_PROGRESS.labels(method=method).inc()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_name = getattr(route, "path", None) or "unmatched"
        duration = time.perf_counter() - started
        HTTP_REQUESTS.labels(method=method, route=route_name, status=str(status_code)).inc()
        HTTP_LATENCY.labels(method=method, route=route_name).observe(duration)
        HTTP_IN_PROGRESS.labels(method=method).dec()


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    if not settings.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    payload, content_type = api_metrics_payload()
    return Response(content=payload, media_type=content_type)


@app.get("/", summary="Web 控制台主页", include_in_schema=False)
async def serve_dashboard():
    index_file = STATIC_DIR / "index.html"
    return FileResponse(str(index_file))


@app.get("/portal", summary="租户对账中心门户", include_in_schema=False)
@app.get("/reconciliation", summary="租户对账中心门户", include_in_schema=False)
async def serve_tenant_portal():
    portal_file = STATIC_DIR / "portal.html"
    return FileResponse(str(portal_file))


@app.get("/login", summary="用户与管理员登录页", include_in_schema=False)
async def serve_login():
    login_file = STATIC_DIR / "login.html"
    return FileResponse(str(login_file))


@app.get("/register", summary="客户自助注册开户页", include_in_schema=False)
async def serve_register():
    register_file = STATIC_DIR / "register.html"
    return FileResponse(str(register_file))


@app.get("/manual", summary="用户接入与集成手册", include_in_schema=False)
@app.get("/guide", summary="用户接入与集成手册", include_in_schema=False)
async def serve_manual():
    manual_file = STATIC_DIR / "manual.html"
    return FileResponse(str(manual_file))




@app.get("/health", summary="健康检查接口")
async def health_check():
    queue_healthy = await task_queue.health()
    return {
        "status": "healthy" if queue_healthy else "degraded",
        "service": "CargoPlus Mail Extraction API",
        "version": "1.0.0",
        "queue_backlog": task_queue.queue_size,
        "queue_healthy": queue_healthy,
    }


@app.get("/health/live", summary="进程存活检查", include_in_schema=False)
async def liveness_check():
    return {"status": "alive"}


@app.get("/health/ready", summary="依赖就绪检查", include_in_schema=False)
async def readiness_check():
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        queue_healthy = await task_queue.health()
    except Exception as exc:
        logger.error("Readiness dependency check failed: %s", exc)
        raise HTTPException(status_code=503, detail="Service dependencies are not ready") from exc
    if not queue_healthy:
        raise HTTPException(status_code=503, detail="Task queue is not ready")
    return {"status": "ready", "database": "ok", "queue": "ok"}
