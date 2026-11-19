"""Master data import service supporting versioned CSV and JSON with dry-run and upserts."""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.writer import write_audit_log
from app.db.models import Coverage, Customer, Policy, RequiredDocumentType
from app.domain.enums import LimitScope, PolicyStatus, ProductType
from app.schemas.master_data import (
    CustomerCreate,
    ImportSummary,
    MasterDataBatchImport,
)


class MasterDataImportService:
    """Service to import master data from JSON payloads or CSV text."""

    def __init__(self, db: Session, actor_id: uuid.UUID, actor_role: str) -> None:
        self.db = db
        self.actor_id = actor_id
        self.actor_role = actor_role

    def import_json(self, payload: MasterDataBatchImport, dry_run: bool = False) -> ImportSummary:
        """Process batch JSON import with schema validation and idempotent upserts."""
        summary = ImportSummary(
            dry_run=dry_run,
            source_format="json",
            customers_processed=0,
            policies_processed=0,
            coverages_processed=0,
            items_created=0,
            items_updated=0,
            errors=[],
        )

        customer_id_map: dict[str, uuid.UUID] = {}

        # 1. Process Customers
        for idx, cust_data in enumerate(payload.customers):
            summary.customers_processed += 1
            try:
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
                        existing.source_system = cust_data.source_system
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
            except Exception as exc:
                summary.errors.append(f"Customer #{idx} ({cust_data.customer_no}): {exc}")

        if not dry_run:
            self.db.flush()

        # 2. Process Policies
        for idx, pol_data in enumerate(payload.policies):
            summary.policies_processed += 1
            try:
                # Resolve customer_id if needed
                cust_id = pol_data.customer_id
                target_customer = self.db.get(Customer, cust_id)
                if not target_customer:
                    summary.errors.append(
                        f"Policy #{idx} ({pol_data.policy_no}): Customer ID '{cust_id}' not found"
                    )
                    continue

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
                        existing_pol.source_system = pol_data.source_system
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
                        # Check existing coverage for category on policy
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
                            )
                            self.db.add(new_cov)
                            summary.items_created += 1
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

        if not dry_run:
            self.db.flush()
            write_audit_log(
                db=self.db,
                action="IMPORT_MASTER_DATA",
                entity_type="MasterData",
                entity_id=str(uuid.uuid4()),
                actor_role=self.actor_role,
                actor_id=self.actor_id,
                event_data={
                    "source_format": "json",
                    "items_created": summary.items_created,
                    "items_updated": summary.items_updated,
                    "errors_count": len(summary.errors),
                },
            )

        return summary

    def import_csv(
        self,
        entity_type: str,
        csv_content: str,
        dry_run: bool = False,
    ) -> ImportSummary:
        """Process CSV import for a specific entity type (customers, policies, coverages, required_documents)."""
        summary = ImportSummary(
            dry_run=dry_run,
            source_format="csv",
            customers_processed=0,
            policies_processed=0,
            coverages_processed=0,
            items_created=0,
            items_updated=0,
            errors=[],
        )

        reader = csv.DictReader(io.StringIO(csv_content.strip()))
        if not reader.fieldnames:
            summary.errors.append("Empty CSV or missing header row")
            return summary

        entity_type_lower = entity_type.strip().lower()

        if entity_type_lower == "customers":
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
                            existing.source_system = payload.source_system
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
                except (ValidationError, KeyError, ValueError) as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower == "policies":
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
                            existing_pol.source_system = (
                                row.get("source_system", "").strip() or None
                            )
                            existing_pol.external_ref = row.get("external_ref", "").strip() or None
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
                                source_system=row.get("source_system", "").strip() or None,
                                external_ref=row.get("external_ref", "").strip() or None,
                            )
                            self.db.add(new_pol)
                        summary.items_created += 1
                except Exception as exc:
                    summary.errors.append(f"Row {row_idx}: {exc}")

        elif entity_type_lower == "coverages":
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
                            )
                            self.db.add(new_cov)
                        summary.items_created += 1
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
                entity_id=str(uuid.uuid4()),
                actor_role=self.actor_role,
                actor_id=self.actor_id,
                event_data={
                    "source_format": "csv",
                    "entity_type": entity_type,
                    "items_created": summary.items_created,
                    "items_updated": summary.items_updated,
                    "errors_count": len(summary.errors),
                },
            )

        return summary
