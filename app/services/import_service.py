"""Master data and claims import service supporting versioned CSV and JSON (P7 Task 3).

Features:
- Versioned CSV/JSON schema validation
- Dry-run capability
- Idempotent upserts on UNIQUE(source_system, external_ref) with fallback to natural key
- Rejection of missing/invalid coverage terms into import_rejects table
- History claim imports update history_coverage.loaded_from
- Chunked transaction processing with batch audit event
"""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.base import utc_now
from app.db.models import (
    Claim,
    Coverage,
    Customer,
    HistoryCoverage,
    ImportReject,
    Policy,
    RequiredDocumentType,
)
from app.domain.enums import ClaimChannel, ClaimStatus, LimitScope, PolicyStatus, ProductType
from app.schemas.master_data import (
    CustomerCreate,
    ImportSummary,
    MasterDataBatchImport,
)


class MasterDataImportService:
    """Service to import master data and claims from JSON payloads or CSV text."""

    def __init__(self, db: Session, actor_id: uuid.UUID, actor_role: str) -> None:
        self.db = db
        self.actor_id = actor_id
        self.actor_role = actor_role

    def import_json(
        self,
        payload: MasterDataBatchImport,
        dry_run: bool = False,
        chunk_size: int = 50,
    ) -> ImportSummary:
        """Process batch JSON import with schema validation, idempotent upserts, and rejects."""
        batch_id = uuid.uuid4()
        summary = ImportSummary(
            dry_run=dry_run,
            source_format="json",
            batch_id=batch_id,
            customers_processed=0,
            policies_processed=0,
            coverages_processed=0,
            claims_processed=0,
            items_created=0,
            items_updated=0,
            rejects_created=0,
            errors=[],
        )

        customer_id_map: dict[str, uuid.UUID] = {}

        # 1. Process Customers in chunks
        for idx, cust_data in enumerate(payload.customers):
            summary.customers_processed += 1
            try:
                existing: Customer | None = None
                # Priority 1: UNIQUE(source_system, external_ref)
                if cust_data.source_system and cust_data.external_ref:
                    existing = self.db.scalars(
                        select(Customer).where(
                            Customer.source_system == cust_data.source_system,
                            Customer.external_ref == cust_data.external_ref,
                        )
                    ).first()

                # Priority 2: Fallback to customer_no
                if not existing:
                    existing = self.db.scalars(
                        select(Customer).where(Customer.customer_no == cust_data.customer_no)
                    ).first()

                if existing:
                    customer_id_map[cust_data.customer_no] = existing.id
                    if not dry_run:
                        existing.full_name = cust_data.full_name
                        existing.dob = cust_data.dob
                        existing.email = cust_data.email
                        existing.phone = cust_data.phone
                        existing.address = cust_data.address
                        existing.state_region = cust_data.state_region
                        if cust_data.source_system:
                            existing.source_system = cust_data.source_system
                        if cust_data.external_ref:
                            existing.external_ref = cust_data.external_ref
                    summary.items_updated += 1
                else:
                    new_id = uuid.uuid4()
                    customer_id_map[cust_data.customer_no] = new_id
                    if not dry_run:
                        customer = Customer(
                            id=new_id,
                            customer_no=cust_data.customer_no,
                            full_name=cust_data.full_name,
                            dob=cust_data.dob,
                            email=cust_data.email,
                            phone=cust_data.phone,
                            address=cust_data.address,
                            state_region=cust_data.state_region,
                            source_system=cust_data.source_system,
                            external_ref=cust_data.external_ref,
                        )
                        self.db.add(customer)
                    summary.items_created += 1

                if not dry_run and (idx + 1) % chunk_size == 0:
                    self.db.flush()
            except Exception as exc:
                summary.errors.append(f"Customer #{idx} ({cust_data.customer_no}): {exc}")

        if not dry_run:
            self.db.flush()

        # 2. Process Policies
        for idx, pol_data in enumerate(payload.policies):
            summary.policies_processed += 1
            try:
                cust_id = pol_data.customer_id
                target_customer = self.db.get(Customer, cust_id)
                if not target_customer:
                    summary.errors.append(
                        f"Policy #{idx} ({pol_data.policy_no}): Customer ID '{cust_id}' not found"
                    )
                    continue

                # Coverage term presence check: reject if policy has no coverages defined
                if not pol_data.coverages:
                    if not dry_run:
                        reject = ImportReject(
                            id=uuid.uuid4(),
                            batch_id=batch_id,
                            entity_type="policy",
                            external_ref=pol_data.external_ref or pol_data.policy_no,
                            payload=pol_data.model_dump(mode="json"),
                            reason=f"Policy '{pol_data.policy_no}' rejected: missing mandatory coverage terms",
                            created_at=utc_now(),
                        )
                        self.db.add(reject)
                    summary.rejects_created += 1
                    continue

                existing_pol: Policy | None = None
                # Priority 1: UNIQUE(source_system, external_ref)
                if pol_data.source_system and pol_data.external_ref:
                    existing_pol = self.db.scalars(
                        select(Policy).where(
                            Policy.source_system == pol_data.source_system,
                            Policy.external_ref == pol_data.external_ref,
                        )
                    ).first()

                # Priority 2: Fallback to policy_no
                if not existing_pol:
                    existing_pol = self.db.scalars(
                        select(Policy).where(Policy.policy_no == pol_data.policy_no)
                    ).first()

                if existing_pol:
                    pol_id = existing_pol.id
                    if not dry_run:
                        existing_pol.customer_id = cust_id
                        existing_pol.product_type = pol_data.product_type
                        existing_pol.start_date = pol_data.start_date
                        existing_pol.end_date = pol_data.end_date
                        existing_pol.premium = pol_data.premium
                        existing_pol.sum_insured = pol_data.sum_insured
                        existing_pol.status = pol_data.status
                        if pol_data.source_system:
                            existing_pol.source_system = pol_data.source_system
                        if pol_data.external_ref:
                            existing_pol.external_ref = pol_data.external_ref
                    summary.items_updated += 1
                else:
                    pol_id = uuid.uuid4()
                    if not dry_run:
                        new_pol = Policy(
                            id=pol_id,
                            policy_no=pol_data.policy_no,
                            customer_id=cust_id,
                            product_type=pol_data.product_type,
                            start_date=pol_data.start_date,
                            end_date=pol_data.end_date,
                            premium=pol_data.premium,
                            sum_insured=pol_data.sum_insured,
                            status=pol_data.status,
                            source_system=pol_data.source_system,
                            external_ref=pol_data.external_ref,
                        )
                        self.db.add(new_pol)
                    summary.items_created += 1

                # Process nested coverages
                for cov_data in pol_data.coverages:
                    summary.coverages_processed += 1
                    if not dry_run:
                        cov = self.db.scalars(
                            select(Coverage).where(
                                Coverage.policy_id == pol_id,
                                Coverage.claim_category == cov_data.claim_category,
                            )
                        ).first()
                        if cov:
                            cov.limit_scope = cov_data.limit_scope
                            cov.limit_amount = cov_data.limit_amount
                            cov.deductible = cov_data.deductible
                            cov.copay_percent = cov_data.copay_percent
                            cov.waiting_period_days = cov_data.waiting_period_days
                            cov.conditions = cov_data.conditions
                            cov.is_excluded = cov_data.is_excluded
                            cov.limit_exceeded_behavior = cov_data.limit_exceeded_behavior
                            summary.items_updated += 1
                        else:
                            new_cov = Coverage(
                                id=uuid.uuid4(),
                                policy_id=pol_id,
                                claim_category=cov_data.claim_category,
                                limit_scope=cov_data.limit_scope,
                                limit_amount=cov_data.limit_amount,
                                consumed_amount=Decimal("0.00"),
                                deductible=cov_data.deductible,
                                copay_percent=cov_data.copay_percent,
                                waiting_period_days=cov_data.waiting_period_days,
                                conditions=cov_data.conditions,
                                is_excluded=cov_data.is_excluded,
                                limit_exceeded_behavior=cov_data.limit_exceeded_behavior,
                            )
                            self.db.add(new_cov)
                            summary.items_created += 1

                if not dry_run and (idx + 1) % chunk_size == 0:
                    self.db.flush()
            except Exception as exc:
                summary.errors.append(f"Policy #{idx} ({pol_data.policy_no}): {exc}")

        # 3. Process Required Documents
        for idx, doc_data in enumerate(payload.required_documents):
            try:
                stmt = select(RequiredDocumentType).where(
                    RequiredDocumentType.product_type == doc_data.product_type,
                    RequiredDocumentType.claim_category == doc_data.claim_category,
                    RequiredDocumentType.doc_code == doc_data.doc_code,
                )
                existing_doc = self.db.scalars(stmt).first()
                if existing_doc:
                    if not dry_run:
                        existing_doc.name = doc_data.name
                        existing_doc.is_mandatory = doc_data.is_mandatory
                    summary.items_updated += 1
                else:
                    if not dry_run:
                        new_doc = RequiredDocumentType(
                            id=uuid.uuid4(),
                            product_type=doc_data.product_type,
                            claim_category=doc_data.claim_category,
                            doc_code=doc_data.doc_code,
                            name=doc_data.name,
                            is_mandatory=doc_data.is_mandatory,
                        )
                        self.db.add(new_doc)
                    summary.items_created += 1
            except Exception as exc:
                summary.errors.append(f"DocumentType #{idx} ({doc_data.doc_code}): {exc}")

        # 4. Process Claims
        for idx, claim_data in enumerate(payload.claims):
            summary.claims_processed += 1
            try:
                # Look up policy
                policy = self.db.scalars(
                    select(Policy).where(Policy.policy_no == claim_data.policy_no)
                ).first()
                if not policy:
                    if not dry_run:
                        reject = ImportReject(
                            id=uuid.uuid4(),
                            batch_id=batch_id,
                            entity_type="claim",
                            external_ref=claim_data.external_ref or claim_data.claim_ref,
                            payload=claim_data.model_dump(mode="json"),
                            reason=f"Referenced policy '{claim_data.policy_no}' not found",
                            created_at=utc_now(),
                        )
                        self.db.add(reject)
                    summary.rejects_created += 1
                    continue

                # Coverage term check: policy must have coverage for claim_category
                cov = self.db.scalars(
                    select(Coverage).where(
                        Coverage.policy_id == policy.id,
                        Coverage.claim_category == claim_data.claim_category,
                    )
                ).first()
                if not cov:
                    if not dry_run:
                        reject = ImportReject(
                            id=uuid.uuid4(),
                            batch_id=batch_id,
                            entity_type="claim",
                            external_ref=claim_data.external_ref or claim_data.claim_ref,
                            payload=claim_data.model_dump(mode="json"),
                            reason=f"Policy '{policy.policy_no}' has no coverage term for category '{claim_data.claim_category}'",
                            created_at=utc_now(),
                        )
                        self.db.add(reject)
                    summary.rejects_created += 1
                    continue

                # Idempotent upsert check on claim
                existing_claim: Claim | None = None
                # Priority 1: UNIQUE(source_system, external_ref)
                if claim_data.source_system and claim_data.external_ref:
                    existing_claim = self.db.scalars(
                        select(Claim).where(
                            Claim.source_system == claim_data.source_system,
                            Claim.external_ref == claim_data.external_ref,
                        )
                    ).first()

                # Priority 2: Fallback to claim_ref
                if not existing_claim:
                    existing_claim = self.db.scalars(
                        select(Claim).where(Claim.claim_ref == claim_data.claim_ref)
                    ).first()

                target_status = claim_data.historical_status or ClaimStatus.REGISTERED

                if existing_claim:
                    if not dry_run:
                        existing_claim.requested_amount = claim_data.requested_amount
                        existing_claim.incident_description = claim_data.description
                        if claim_data.source_system:
                            existing_claim.source_system = claim_data.source_system
                        if claim_data.external_ref:
                            existing_claim.external_ref = claim_data.external_ref
                    summary.items_updated += 1
                else:
                    if not dry_run:
                        new_claim = Claim(
                            id=uuid.uuid4(),
                            claim_ref=claim_data.claim_ref,
                            policy_id=policy.id,
                            customer_id=policy.customer_id,
                            claim_category=claim_data.claim_category,
                            incident_date=claim_data.incident_date,
                            incident_location="Imported location",
                            incident_description=claim_data.description,
                            requested_amount=claim_data.requested_amount,
                            registered_at=utc_now(),
                            registered_on=utc_now().date(),
                            registered_tz="Asia/Kolkata",
                            registered_by=self.actor_id,
                            channel=claim_data.channel,
                            status=target_status,
                            status_version=1,
                            legal_hold=False,
                            source_system=claim_data.source_system,
                            external_ref=claim_data.external_ref,
                        )
                        self.db.add(new_claim)
                    summary.items_created += 1

                # Update history coverage loaded_from for the product type
                if not dry_run:
                    prod_val = policy.product_type.value
                    hc = self.db.scalars(
                        select(HistoryCoverage).where(HistoryCoverage.product_type == prod_val)
                    ).first()
                    if hc:
                        if claim_data.incident_date < hc.loaded_from:
                            hc.loaded_from = claim_data.incident_date
                    else:
                        new_hc = HistoryCoverage(
                            id=uuid.uuid4(),
                            product_type=prod_val,
                            loaded_from=claim_data.incident_date,
                            set_by_id=self.actor_id,
                            evidence_reference=f"Import batch {batch_id}",
                        )
                        self.db.add(new_hc)

                if not dry_run and (idx + 1) % chunk_size == 0:
                    self.db.flush()
            except Exception as exc:
                summary.errors.append(f"Claim #{idx} ({claim_data.claim_ref}): {exc}")

        if not dry_run:
            self.db.flush()
            write_audit_log(
                db=self.db,
                action="IMPORT_MASTER_DATA",
                entity_type="MasterData",
                entity_id=str(batch_id),
                actor_role=self.actor_role,
                actor_id=self.actor_id,
                event_data={
                    "batch_id": str(batch_id),
                    "source_format": "json",
                    "customers_processed": summary.customers_processed,
                    "policies_processed": summary.policies_processed,
                    "coverages_processed": summary.coverages_processed,
                    "claims_processed": summary.claims_processed,
                    "items_created": summary.items_created,
                    "items_updated": summary.items_updated,
                    "rejects_created": summary.rejects_created,
                    "errors_count": len(summary.errors),
                },
            )

        return summary

    def import_csv(
        self,
        entity_type: str,
        csv_content: str,
        dry_run: bool = False,
        chunk_size: int = 50,
    ) -> ImportSummary:
        """Process CSV import for customers, policies, coverages, required_documents, or claims."""
        batch_id = uuid.uuid4()
        summary = ImportSummary(
            dry_run=dry_run,
            source_format="csv",
            batch_id=batch_id,
            customers_processed=0,
            policies_processed=0,
            coverages_processed=0,
            claims_processed=0,
            items_created=0,
            items_updated=0,
            rejects_created=0,
            errors=[],
        )

        reader = csv.DictReader(io.StringIO(csv_content.strip()))
        if not reader.fieldnames:
            summary.errors.append("Empty CSV or missing header row")
            return summary

        entity_type_lower = entity_type.strip().lower()

        if entity_type_lower in ("customers", "customer"):
            for row_idx, row in enumerate(reader, start=1):
                summary.customers_processed += 1
                try:
                    payload = CustomerCreate(
                        customer_no=row["customer_no"].strip(),
                        full_name=row["full_name"].strip(),
                        dob=date.fromisoformat(row["dob"].strip()),
                        email=row["email"].strip(),
                        phone=row["phone"].strip(),
                        address=row["address"].strip(),
                        state_region=row["state_region"].strip(),
                        source_system=row.get("source_system", "").strip() or None,
                        external_ref=row.get("external_ref", "").strip() or None,
                    )
                    existing: Customer | None = None
                    if payload.source_system and payload.external_ref:
                        existing = self.db.scalars(
                            select(Customer).where(
                                Customer.source_system == payload.source_system,
                                Customer.external_ref == payload.external_ref,
                            )
                        ).first()
                    if not existing:
                        existing = self.db.scalars(
                            select(Customer).where(Customer.customer_no == payload.customer_no)
                        ).first()

                    if existing:
                        if not dry_run:
                            existing.full_name = payload.full_name
                            existing.dob = payload.dob
                            existing.email = payload.email
                            existing.phone = payload.phone
                            existing.address = payload.address
                            existing.state_region = payload.state_region
                            if payload.source_system:
                                existing.source_system = payload.source_system
                            if payload.external_ref:
                                existing.external_ref = payload.external_ref
                        summary.items_updated += 1
                    else:
                        if not dry_run:
                            new_cust = Customer(
                                id=uuid.uuid4(),
                                customer_no=payload.customer_no,
                                full_name=payload.full_name,
                                dob=payload.dob,
                                email=payload.email,
                                phone=payload.phone,
                                address=payload.address,
                                state_region=payload.state_region,
                                source_system=payload.source_system,
                                external_ref=payload.external_ref,
                            )
                            self.db.add(new_cust)
                        summary.items_created += 1

                    if not dry_run and row_idx % chunk_size == 0:
                        self.db.flush()
                except (ValidationError, KeyError, ValueError) as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower in ("policies", "policy"):
            for row_idx, row in enumerate(reader, start=1):
                summary.policies_processed += 1
                try:
                    customer_no = row.get("customer_no", "").strip()
                    customer = self.db.scalars(
                        select(Customer).where(Customer.customer_no == customer_no)
                    ).first()
                    if not customer:
                        summary.errors.append(
                            f"Row {row_idx}: Customer with customer_no '{customer_no}' not found"
                        )
                        continue

                    policy_no = row["policy_no"].strip()
                    product_type = ProductType(row["product_type"].strip().upper())
                    start_date = date.fromisoformat(row["start_date"].strip())
                    end_date = date.fromisoformat(row["end_date"].strip())
                    premium = Decimal(row["premium"].strip())
                    sum_insured = Decimal(row["sum_insured"].strip())
                    status_val = PolicyStatus(row.get("status", "ACTIVE").strip().upper())
                    source_system = row.get("source_system", "").strip() or None
                    external_ref = row.get("external_ref", "").strip() or None

                    existing_pol: Policy | None = None
                    if source_system and external_ref:
                        existing_pol = self.db.scalars(
                            select(Policy).where(
                                Policy.source_system == source_system,
                                Policy.external_ref == external_ref,
                            )
                        ).first()
                    if not existing_pol:
                        existing_pol = self.db.scalars(
                            select(Policy).where(Policy.policy_no == policy_no)
                        ).first()

                    if existing_pol:
                        if not dry_run:
                            existing_pol.customer_id = customer.id
                            existing_pol.product_type = product_type
                            existing_pol.start_date = start_date
                            existing_pol.end_date = end_date
                            existing_pol.premium = premium
                            existing_pol.sum_insured = sum_insured
                            existing_pol.status = status_val
                            if source_system:
                                existing_pol.source_system = source_system
                            if external_ref:
                                existing_pol.external_ref = external_ref
                        summary.items_updated += 1
                    else:
                        if not dry_run:
                            new_pol = Policy(
                                id=uuid.uuid4(),
                                policy_no=policy_no,
                                customer_id=customer.id,
                                product_type=product_type,
                                start_date=start_date,
                                end_date=end_date,
                                premium=premium,
                                sum_insured=sum_insured,
                                status=status_val,
                                source_system=source_system,
                                external_ref=external_ref,
                            )
                            self.db.add(new_pol)
                        summary.items_created += 1

                    if not dry_run and row_idx % chunk_size == 0:
                        self.db.flush()
                except Exception as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower in ("coverages", "coverage"):
            for row_idx, row in enumerate(reader, start=1):
                summary.coverages_processed += 1
                try:
                    policy_no = row["policy_no"].strip()
                    policy = self.db.scalars(
                        select(Policy).where(Policy.policy_no == policy_no)
                    ).first()
                    if not policy:
                        summary.errors.append(
                            f"Row {row_idx}: Policy with policy_no '{policy_no}' not found"
                        )
                        continue

                    category = row["claim_category"].strip()
                    limit_scope = LimitScope(
                        row.get("limit_scope", "PER_POLICY_PERIOD").strip().upper()
                    )
                    limit_amount = Decimal(row["limit_amount"].strip())
                    deductible = Decimal(row.get("deductible", "0.00").strip() or "0.00")
                    copay = Decimal(row.get("copay_percent", "0.00").strip() or "0.00")
                    waiting_period = int(row.get("waiting_period_days", "0").strip() or "0")
                    is_excluded = row.get("is_excluded", "false").strip().lower() in (
                        "true",
                        "1",
                        "yes",
                    )
                    limit_exceeded_behavior = (
                        row.get("limit_exceeded_behavior", "CAP").strip().upper()
                    )
                    if limit_exceeded_behavior not in ("CAP", "REJECT"):
                        raise ValueError(
                            f"Invalid limit_exceeded_behavior: '{limit_exceeded_behavior}'. Must be 'CAP' or 'REJECT'."
                        )

                    cov = self.db.scalars(
                        select(Coverage).where(
                            Coverage.policy_id == policy.id,
                            Coverage.claim_category == category,
                        )
                    ).first()
                    if cov:
                        if not dry_run:
                            cov.limit_scope = limit_scope
                            cov.limit_amount = limit_amount
                            cov.deductible = deductible
                            cov.copay_percent = copay
                            cov.waiting_period_days = waiting_period
                            cov.is_excluded = is_excluded
                            cov.limit_exceeded_behavior = limit_exceeded_behavior
                        summary.items_updated += 1
                    else:
                        if not dry_run:
                            new_cov = Coverage(
                                id=uuid.uuid4(),
                                policy_id=policy.id,
                                claim_category=category,
                                limit_scope=limit_scope,
                                limit_amount=limit_amount,
                                consumed_amount=Decimal("0.00"),
                                deductible=deductible,
                                copay_percent=copay,
                                waiting_period_days=waiting_period,
                                is_excluded=is_excluded,
                                limit_exceeded_behavior=limit_exceeded_behavior,
                            )
                            self.db.add(new_cov)
                        summary.items_created += 1

                    if not dry_run and row_idx % chunk_size == 0:
                        self.db.flush()
                except Exception as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower in ("claims", "claim"):
            for row_idx, row in enumerate(reader, start=1):
                summary.claims_processed += 1
                try:
                    policy_no = row["policy_no"].strip()
                    policy = self.db.scalars(
                        select(Policy).where(Policy.policy_no == policy_no)
                    ).first()
                    if not policy:
                        if not dry_run:
                            reject = ImportReject(
                                id=uuid.uuid4(),
                                batch_id=batch_id,
                                entity_type="claim",
                                external_ref=row.get("external_ref", "").strip() or None,
                                payload=dict(row),
                                reason=f"Referenced policy '{policy_no}' not found",
                                created_at=utc_now(),
                            )
                            self.db.add(reject)
                        summary.rejects_created += 1
                        continue

                    category = row["claim_category"].strip()
                    cov = self.db.scalars(
                        select(Coverage).where(
                            Coverage.policy_id == policy.id,
                            Coverage.claim_category == category,
                        )
                    ).first()
                    if not cov:
                        if not dry_run:
                            reject = ImportReject(
                                id=uuid.uuid4(),
                                batch_id=batch_id,
                                entity_type="claim",
                                external_ref=row.get("external_ref", "").strip() or None,
                                payload=dict(row),
                                reason=f"Policy '{policy.policy_no}' has no coverage term for category '{category}'",
                                created_at=utc_now(),
                            )
                            self.db.add(reject)
                        summary.rejects_created += 1
                        continue

                    claim_ref = (row.get("claim_ref") or row.get("claim_no", "")).strip()
                    incident_date = date.fromisoformat(row["incident_date"].strip())
                    requested_amount = Decimal(row["requested_amount"].strip())
                    description = row.get("description", "Imported claim").strip()
                    source_system = row.get("source_system", "").strip() or None
                    external_ref = row.get("external_ref", "").strip() or None
                    raw_status = row.get("historical_status", "").strip().upper()
                    target_status = (
                        ClaimStatus(raw_status) if raw_status else ClaimStatus.REGISTERED
                    )

                    existing_claim: Claim | None = None
                    if source_system and external_ref:
                        existing_claim = self.db.scalars(
                            select(Claim).where(
                                Claim.source_system == source_system,
                                Claim.external_ref == external_ref,
                            )
                        ).first()
                    if not existing_claim:
                        existing_claim = self.db.scalars(
                            select(Claim).where(Claim.claim_ref == claim_ref)
                        ).first()

                    if existing_claim:
                        if not dry_run:
                            existing_claim.requested_amount = requested_amount
                            existing_claim.incident_description = description
                            if source_system:
                                existing_claim.source_system = source_system
                            if external_ref:
                                existing_claim.external_ref = external_ref
                        summary.items_updated += 1
                    else:
                        if not dry_run:
                            new_claim = Claim(
                                id=uuid.uuid4(),
                                claim_ref=claim_ref,
                                policy_id=policy.id,
                                customer_id=policy.customer_id,
                                claim_category=category,
                                incident_date=incident_date,
                                incident_location="Imported location",
                                incident_description=description,
                                requested_amount=requested_amount,
                                registered_at=utc_now(),
                                registered_on=utc_now().date(),
                                registered_tz="Asia/Kolkata",
                                registered_by=self.actor_id,
                                channel=(
                                    ClaimChannel(row.get("channel", "CUSTOMER").strip().upper())
                                    if row.get("channel", "").strip().upper()
                                    in ("CUSTOMER", "STAFF")
                                    else ClaimChannel.CUSTOMER
                                ),
                                status=target_status,
                                status_version=1,
                                legal_hold=False,
                                source_system=source_system,
                                external_ref=external_ref,
                            )
                            self.db.add(new_claim)
                        summary.items_created += 1

                    if not dry_run:
                        prod_val = policy.product_type.value
                        hc = self.db.scalars(
                            select(HistoryCoverage).where(HistoryCoverage.product_type == prod_val)
                        ).first()
                        if hc:
                            if incident_date < hc.loaded_from:
                                hc.loaded_from = incident_date
                        else:
                            new_hc = HistoryCoverage(
                                id=uuid.uuid4(),
                                product_type=prod_val,
                                loaded_from=incident_date,
                                set_by_id=self.actor_id,
                                evidence_reference=f"Import batch {batch_id}",
                            )
                            self.db.add(new_hc)

                    if not dry_run and row_idx % chunk_size == 0:
                        self.db.flush()
                except Exception as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower in ("required_documents", "required_document_types"):
            for row_idx, row in enumerate(reader, start=1):
                try:
                    product_type = ProductType(row["product_type"].strip().upper())
                    category = row.get("claim_category", "").strip() or None
                    doc_code = row["doc_code"].strip()
                    name = row["name"].strip()
                    is_mandatory = row.get("is_mandatory", "true").strip().lower() in (
                        "true",
                        "1",
                        "yes",
                    )

                    stmt = select(RequiredDocumentType).where(
                        RequiredDocumentType.product_type == product_type,
                        RequiredDocumentType.claim_category == category,
                        RequiredDocumentType.doc_code == doc_code,
                    )
                    existing_doc = self.db.scalars(stmt).first()
                    if existing_doc:
                        if not dry_run:
                            existing_doc.name = name
                            existing_doc.is_mandatory = is_mandatory
                        summary.items_updated += 1
                    else:
                        if not dry_run:
                            new_doc = RequiredDocumentType(
                                id=uuid.uuid4(),
                                product_type=product_type,
                                claim_category=category,
                                doc_code=doc_code,
                                name=name,
                                is_mandatory=is_mandatory,
                            )
                            self.db.add(new_doc)
                        summary.items_created += 1
                except Exception as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")
        else:
            summary.errors.append(f"Unsupported entity type for CSV import: '{entity_type}'")

        if not dry_run:
            self.db.flush()
            write_audit_log(
                db=self.db,
                action="IMPORT_MASTER_DATA",
                entity_type="MasterData",
                entity_id=str(batch_id),
                actor_role=self.actor_role,
                actor_id=self.actor_id,
                event_data={
                    "batch_id": str(batch_id),
                    "source_format": "csv",
                    "entity_type": entity_type,
                    "customers_processed": summary.customers_processed,
                    "policies_processed": summary.policies_processed,
                    "coverages_processed": summary.coverages_processed,
                    "claims_processed": summary.claims_processed,
                    "items_created": summary.items_created,
                    "items_updated": summary.items_updated,
                    "rejects_created": summary.rejects_created,
                    "errors_count": len(summary.errors),
                },
            )

        return summary
