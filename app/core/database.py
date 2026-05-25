"""
database.py
===========
Configuracion de SQLAlchemy async para el Centro de Mensajes.

Expone:
  - engine            AsyncEngine global del proceso.
  - SessionLocal      sessionmaker para crear AsyncSession por request.
  - get_db()          Dependencia FastAPI: yield AsyncSession, cierra al final.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


# Engine global - una sola pool por proceso. echo=True solo en dev.
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=(settings.env == "dev"),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependencia FastAPI para inyectar una AsyncSession por request.

    Uso:
        @router.get("/foo")
        async def foo(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
