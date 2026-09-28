from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase 

from app.config import settings

class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False, bind=engine)

from typing import Iterator

def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session