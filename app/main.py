from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.openapi import API_DESCRIPTION, API_VERSION, OPENAPI_TAGS, configure_openapi
from app.api.routes import router
from app.core.config import get_settings
from app.db.session import init_db
from app.events.consumer import run_consumer
from app.observability import setup_observability

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    stop = asyncio.Event()
    consumer_task = asyncio.create_task(run_consumer(stop), name="finance-nats-consumer")
    try:
        yield
    finally:
        stop.set()
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=settings.service_name,
    version=API_VERSION,
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
configure_openapi(app)
setup_observability(app)
