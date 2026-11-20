"""Pytest configuration and database fixtures."""

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure test environment settings
os.environ["ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-32-chars-minimum-length-for-testing"
os.environ["AUDIT_HMAC_KEY"] = "test-audit-hmac-key-32-chars-minimum-for-testing"

from app.db.base import Base
from app.db.models import User
from app.db.session import get_db
from app.domain.enums import UserRole
from app.main import app
from app.security.password import hash_password

# Use environment DATABASE_URL if configured for PostgreSQL, otherwise in-memory SQLite
env_db = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
if env_db and "postgresql" in env_db:
    TEST_DATABASE_URL = env_db
    test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
else:
    TEST_DATABASE_URL = "sqlite:///:memory:"
    test_engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Create fresh schema before each test and drop afterwards."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Yield a database session for testing."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Yield a FastAPI TestClient with database session overridden."""

    def override_get_db() -> Generator[Session, None, None]:
        session = TestingSessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_admin(db_session: Session) -> User:
    """Create a sample administrator user."""
    admin = User(
        email="admin@test.com",
        password_hash=hash_password("AdminSecurePassword123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


@pytest.fixture
def sample_customer_user(db_session: Session) -> User:
    """Create a sample customer user."""
    customer = User(
        email="customer@test.com",
        password_hash=hash_password("CustomerSecurePassword123!"),
        role=UserRole.CUSTOMER,
        is_active=True,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return customer
