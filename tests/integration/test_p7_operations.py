"""Integration tests for Phase P7 operations, imports, scheduler, and security hooks."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models import (
    Claim,
    Coverage,
    Customer,
    HistoryCoverage,
    ImportReject,
    JobRun,
    Policy,
    User,
)
from app.domain.enums import (
    ClaimChannel,
    ClaimStatus,
    JobStatus,
    LimitScope,
    PolicyStatus,
    ProductType,
    UserRole,
)
from app.schemas.master_data import (
    ClaimImportCreate,
    CustomerCreate,
    MasterDataBatchImport,
    PolicyCreate,
)
from app.security.antivirus import ScanStatus, scan_file
from app.services.claim_service import ClaimService, ClaimValidationError
from app.services.import_service import MasterDataImportService
from scripts.retention_report import generate_retention_report
from scripts.scheduler import check_missed_runs, run_job


@pytest.fixture
def test_actor(db_session: Session) -> User:
    actor = User(
        id=uuid.uuid4(),
        email=f"admin_ops_{uuid.uuid4().hex[:6]}@test.com",
        password_hash="argon2id$test$hash",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()
    return actor


@pytest.fixture
def existing_customer(db_session: Session) -> Customer:
    cust = Customer(
        id=uuid.uuid4(),
        customer_no="CUST-P7-001",
        full_name="Original Name",
        dob=date(1990, 5, 15),
        email="original@test.com",
        phone="9876543210",
        address="123 Street",
        state_region="Maharashtra",
        source_system="CORE_INSURANCE",
        external_ref="EXT-CUST-100",
    )
    db_session.add(cust)
    db_session.commit()
    return cust


@pytest.fixture
def existing_policy(db_session: Session, existing_customer: Customer) -> Policy:
    pol = Policy(
        id=uuid.uuid4(),
        policy_no="POL-P7-001",
        customer_id=existing_customer.id,
        product_type=ProductType.HEALTH,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        premium=Decimal("15000.00"),
        sum_insured=Decimal("500000.00"),
        status=PolicyStatus.ACTIVE,
        source_system="CORE_INSURANCE",
        external_ref="EXT-POL-100",
    )
    db_session.add(pol)
    db_session.commit()

    cov = Coverage(
        id=uuid.uuid4(),
        policy_id=pol.id,
        claim_category="INPATIENT",
        limit_scope=LimitScope.PER_POLICY_PERIOD,
        limit_amount=Decimal("300000.00"),
        consumed_amount=Decimal("0.00"),
        deductible=Decimal("0.00"),
        copay_percent=Decimal("0.00"),
        waiting_period_days=0,
        is_excluded=False,
        limit_exceeded_behavior="CAP",
    )
    db_session.add(cov)
    db_session.commit()
    return pol


# ==============================================================================
# Task 3: Versioned Imports & Idempotency Tests
# ==============================================================================


def test_import_json_idempotent_upsert_on_external_ref(
    db_session: Session,
    test_actor: User,
    existing_customer: Customer,
    existing_policy: Policy,
) -> None:
    """Test priority lookup on (source_system, external_ref) with idempotent update."""
    service = MasterDataImportService(db=db_session, actor_id=test_actor.id, actor_role="ADMIN")

    # Update customer using existing source_system and external_ref
    payload = MasterDataBatchImport(
        customers=[
            CustomerCreate(
                customer_no="CUST-NEW-CODE",  # Changed customer_no
                full_name="Updated By External Ref",
                dob=date(1990, 5, 15),
                email="updated@test.com",
                phone="9876543210",
                address="456 New Street",
                state_region="Maharashtra",
                source_system="CORE_INSURANCE",
                external_ref="EXT-CUST-100",  # Matches existing customer
            )
        ],
        policies=[],
        required_documents=[],
        claims=[],
    )

    summary = service.import_json(payload, dry_run=False)
    assert summary.customers_processed == 1
    assert summary.items_updated == 1
    assert summary.items_created == 0

    # Verify existing customer row was updated without creating duplicate
    db_session.refresh(existing_customer)
    assert existing_customer.full_name == "Updated By External Ref"
    assert existing_customer.address == "456 New Street"

    # Re-running same batch is completely idempotent
    summary2 = service.import_json(payload, dry_run=False)
    assert summary2.items_updated == 1
    assert summary2.items_created == 0


def test_import_rejects_missing_coverage_terms(
    db_session: Session,
    test_actor: User,
    existing_customer: Customer,
) -> None:
    """Test that a policy missing mandatory coverage terms is rejected into import_rejects table."""
    service = MasterDataImportService(db=db_session, actor_id=test_actor.id, actor_role="ADMIN")

    payload = MasterDataBatchImport(
        customers=[],
        policies=[
            PolicyCreate(
                policy_no="POL-NO-COV-001",
                customer_id=existing_customer.id,
                product_type=ProductType.VEHICLE,
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                premium=Decimal("12000.00"),
                sum_insured=Decimal("400000.00"),
                status=PolicyStatus.ACTIVE,
                source_system="EXTERNAL_BROKER",
                external_ref="EXT-POL-NO-COV",
                coverages=[],  # Empty coverages
            )
        ],
        required_documents=[],
        claims=[],
    )

    summary = service.import_json(payload, dry_run=False)
    assert summary.policies_processed == 1
    assert summary.rejects_created == 1
    assert summary.items_created == 0

    # Verify reject was recorded in import_rejects table
    reject = db_session.scalars(
        select(ImportReject).where(ImportReject.external_ref == "EXT-POL-NO-COV")
    ).first()
    assert reject is not None
    assert reject.entity_type == "policy"
    assert "missing mandatory coverage terms" in reject.reason


def test_import_claims_updates_history_coverage_loaded_from(
    db_session: Session,
    test_actor: User,
    existing_customer: Customer,
    existing_policy: Policy,
) -> None:
    """Test that importing historical claims records history_coverage.loaded_from."""
    service = MasterDataImportService(db=db_session, actor_id=test_actor.id, actor_role="ADMIN")

    claim_incident_date = date(2023, 4, 10)
    payload = MasterDataBatchImport(
        customers=[],
        policies=[],
        required_documents=[],
        claims=[
            ClaimImportCreate(
                claim_ref="CLM-HIST-001",
                policy_no=existing_policy.policy_no,
                incident_date=claim_incident_date,
                claim_category="INPATIENT",
                requested_amount=Decimal("45000.00"),
                description="Historical hospitalization claim",
                channel=ClaimChannel.CUSTOMER,
                source_system="LEGACY_SYSTEM",
                external_ref="LEGACY-CLM-100",
                historical_status=ClaimStatus.APPROVED,
            )
        ],
    )

    summary = service.import_json(payload, dry_run=False)
    assert summary.claims_processed == 1
    assert summary.items_created == 1
    assert summary.rejects_created == 0

    # Verify claim was created with historical status
    claim = db_session.scalars(select(Claim).where(Claim.claim_ref == "CLM-HIST-001")).first()
    assert claim is not None
    assert claim.status == ClaimStatus.APPROVED

    # Verify history_coverage was updated with loaded_from
    hc = db_session.scalars(
        select(HistoryCoverage).where(HistoryCoverage.product_type == ProductType.HEALTH.value)
    ).first()
    assert hc is not None
    assert hc.loaded_from == claim_incident_date


def test_import_claim_rejected_for_invalid_coverage_category(
    db_session: Session,
    test_actor: User,
    existing_policy: Policy,
) -> None:
    """Test that a claim specifying an uncovered category is recorded in import_rejects."""
    service = MasterDataImportService(db=db_session, actor_id=test_actor.id, actor_role="ADMIN")

    payload = MasterDataBatchImport(
        customers=[],
        policies=[],
        required_documents=[],
        claims=[
            ClaimImportCreate(
                claim_ref="CLM-UNCOVERED-001",
                policy_no=existing_policy.policy_no,
                incident_date=date(2025, 3, 1),
                claim_category="MATERNITY",  # Policy only covers INPATIENT
                requested_amount=Decimal("50000.00"),
                description="Maternity claim on policy without maternity coverage",
                source_system="PORTAL",
                external_ref="EXT-CLM-UNCOVERED",
            )
        ],
    )

    summary = service.import_json(payload, dry_run=False)
    assert summary.claims_processed == 1
    assert summary.items_created == 0
    assert summary.rejects_created == 1

    # Verify reject was recorded
    reject = db_session.scalars(
        select(ImportReject).where(ImportReject.external_ref == "EXT-CLM-UNCOVERED")
    ).first()
    assert reject is not None
    assert "no coverage term for category 'MATERNITY'" in reject.reason


# ==============================================================================
# Task 4: Antivirus Hook Tests
# ==============================================================================


def test_antivirus_hook_inactive_by_default() -> None:
    """Verify that antivirus scan reports INACTIVE by default until configured."""
    result = scan_file(content=b"Sample PDF document bytes", file_name="bill.pdf")
    assert result.status == ScanStatus.INACTIVE
    assert result.is_clean is True
    assert "inactive" in result.details.lower()


def test_antivirus_hook_rejects_eicar_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    test_actor: User,
    existing_policy: Policy,
) -> None:
    """Verify that when scanner is active, infected file upload is blocked."""
    from app.config import settings

    monkeypatch.setattr(settings, "ANTIVIRUS_ENABLED", True)

    claim_service = ClaimService(db=db_session)

    claim = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-AV-TEST-001",
        policy_id=existing_policy.id,
        customer_id=existing_policy.customer_id,
        claim_category="INPATIENT",
        incident_date=date(2025, 2, 1),
        incident_location="Hospital",
        incident_description="Test claim",
        requested_amount=Decimal("20000.00"),
        registered_at=utc_now(),
        registered_on=date.today(),
        registered_tz="Asia/Kolkata",
        registered_by=test_actor.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.REGISTERED,
    )
    db_session.add(claim)
    db_session.commit()

    eicar_bytes = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

    with pytest.raises(ClaimValidationError) as exc_info:
        claim_service.upload_document(
            claim_id=claim.id,
            doc_code="HOSPITAL_BILL",
            file_name="malicious.pdf",
            content=eicar_bytes,
            mime_type="application/pdf",
            actor=test_actor,
        )
    assert "Malware signature detected" in str(exc_info.value)


# ==============================================================================
# Task 5: Retention Report & Legal Hold Tests
# ==============================================================================


def test_retention_report_respects_legal_hold(
    db_session: Session,
    test_actor: User,
    existing_policy: Policy,
) -> None:
    """Verify retention report identifies expired final claims but protects legal_hold claims."""
    # 1. Claim eligible for retention (closed 11 years ago, no legal hold)
    claim_eligible = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-RET-ELIGIBLE",
        policy_id=existing_policy.id,
        customer_id=existing_policy.customer_id,
        claim_category="INPATIENT",
        incident_date=date(2014, 1, 1),
        incident_location="Hospital",
        incident_description="Old claim",
        requested_amount=Decimal("10000.00"),
        registered_at=datetime(2014, 1, 2, tzinfo=UTC),
        registered_on=date(2014, 1, 2),
        registered_tz="Asia/Kolkata",
        registered_by=test_actor.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.APPROVED,
        legal_hold=False,
    )

    # 2. Claim closed 11 years ago, but on LEGAL HOLD
    claim_held = Claim(
        id=uuid.uuid4(),
        claim_ref="CLM-RET-HELD",
        policy_id=existing_policy.id,
        customer_id=existing_policy.customer_id,
        claim_category="INPATIENT",
        incident_date=date(2014, 1, 1),
        incident_location="Hospital",
        incident_description="Held claim",
        requested_amount=Decimal("15000.00"),
        registered_at=datetime(2014, 1, 2, tzinfo=UTC),
        registered_on=date(2014, 1, 2),
        registered_tz="Asia/Kolkata",
        registered_by=test_actor.id,
        channel=ClaimChannel.CUSTOMER,
        status=ClaimStatus.APPROVED,
        legal_hold=True,  # Legal hold flag active
    )

    db_session.add_all([claim_eligible, claim_held])
    db_session.commit()

    report = generate_retention_report(db_session)
    claim_summary = report.retention_classes["claims"]

    assert claim_summary.held_by_legal_hold_count >= 1
    assert report.claims_on_legal_hold_count >= 1

    # Verify non-destructive guarantee: rows remain untouched
    assert db_session.get(Claim, claim_eligible.id) is not None
    assert db_session.get(Claim, claim_held.id) is not None


# ==============================================================================
# Task 6: Scheduler Jobs & Claim Status Invariant Tests
# ==============================================================================


def test_scheduler_jobs_recorded_in_job_runs(db_session: Session) -> None:
    """Test that executing a scheduler job creates a JobRun record."""
    job_run = run_job("retention_report", db_session)
    assert job_run.job_name == "retention_report"
    assert job_run.status == JobStatus.SUCCESS.value
    assert job_run.completed_at is not None

    # Check persistence
    persisted = db_session.get(JobRun, job_run.id)
    assert persisted is not None
    assert persisted.status == JobStatus.SUCCESS.value


def test_scheduler_jobs_never_mutate_claim_status(
    db_session: Session,
    test_actor: User,
    existing_policy: Policy,
) -> None:
    """Test and assert the invariant: No scheduler job mutates any claim status."""
    claims = [
        Claim(
            id=uuid.uuid4(),
            claim_ref=f"CLM-SCHED-{i}",
            policy_id=existing_policy.id,
            customer_id=existing_policy.customer_id,
            claim_category="INPATIENT",
            incident_date=date(2025, 1, 1),
            incident_location="Hospital",
            incident_description=f"Claim {i}",
            requested_amount=Decimal("10000.00"),
            registered_at=utc_now(),
            registered_on=date.today(),
            registered_tz="Asia/Kolkata",
            registered_by=test_actor.id,
            channel=ClaimChannel.CUSTOMER,
            status=status,
        )
        for i, status in enumerate(
            [ClaimStatus.REGISTERED, ClaimStatus.VALIDATED, ClaimStatus.APPROVED]
        )
    ]
    db_session.add_all(claims)
    db_session.commit()

    pre_statuses = {c.id: c.status for c in claims}

    # Execute multiple operational scheduler jobs
    run_job("reconciliation", db_session)
    run_job("cleanup_sessions", db_session)
    run_job("cleanup_login_attempts", db_session)
    run_job("retention_report", db_session)

    # Verify claim statuses are 100% unchanged
    for c in claims:
        db_session.refresh(c)
        assert c.status == pre_statuses[c.id], f"Claim {c.claim_ref} status changed!"


def test_scheduler_check_missed_runs(db_session: Session) -> None:
    """Test missed run detection and alert generation."""
    alerts = check_missed_runs(db_session)
    assert len(alerts) > 0
    # Every missed run should record a JobRun entry with status MISSED
    missed_records = db_session.scalars(
        select(JobRun).where(JobRun.status == JobStatus.MISSED.value)
    ).all()
    assert len(missed_records) == len(alerts)
