"""Incident group service managing multi-claim linking, lifecycle, and append-only member revisions (P6 Task 9, Decision D12)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.schema import hash_free_text
from app.audit.writer import write_audit_log
from app.db.base import utc_now
from app.db.models import Claim, IncidentGroup, IncidentGroupMember, User
from app.domain.enums import IncidentGroupStatus, UserRole
from app.domain.state_machine import FINAL_STATES
from app.security.rbac import PermissionDeniedError, check_claim_access

MAX_GROUP_SIZE = 10
MAX_DATE_SPAN_DAYS = 365


class IncidentGroupError(ValueError):
    """Raised when incident group operations or invariants fail."""

    pass


class IncidentGroupService:
    """Orchestration service for incident group lifecycle and append-only revisions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def propose_group(
        self,
        actor: User,
        claim_ids: list[uuid.UUID],
        reason: str,
    ) -> IncidentGroup:
        """Staff or investigators propose an incident group linking 2+ claims of the same customer."""
        if actor.role not in (
            UserRole.STAFF,
            UserRole.ASSESSOR,
            UserRole.INVESTIGATOR,
            UserRole.MANAGER,
            UserRole.ADMIN,
        ):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' is not authorized to propose incident groups"
            )

        if len(claim_ids) < 2:
            raise IncidentGroupError("An incident group must contain at least 2 claims")
        if len(claim_ids) > MAX_GROUP_SIZE:
            raise IncidentGroupError(
                f"Incident group exceeds maximum size of {MAX_GROUP_SIZE} claims"
            )

        clean_reason = reason.strip()
        if len(clean_reason) < 10:
            raise IncidentGroupError("Proposal reason must be at least 10 characters long")

        # Fetch claims and ensure object-level existence
        stmt = select(Claim).where(Claim.id.in_(claim_ids))
        claims = list(self.db.scalars(stmt).all())
        if len(claims) != len(set(claim_ids)):
            raise IncidentGroupError("One or more claims not found")

        # Constraint 1: All members must belong to the exact same customer
        customer_ids = {c.customer_id for c in claims}
        if len(customer_ids) != 1:
            raise IncidentGroupError(
                "All member claims in an incident group must belong to the same customer"
            )
        customer_id = customer_ids.pop()

        # Constraint 2: Membership freezes when a claim reaches a final state
        for c in claims:
            if c.status in FINAL_STATES:
                raise IncidentGroupError(
                    f"Claim '{c.claim_ref}' is in final status '{c.status.value}' and cannot join an incident group"
                )

        # Constraint 3: Date span limit (max 365 days between earliest and latest incident date)
        incident_dates = [c.incident_date for c in claims]
        date_span = (max(incident_dates) - min(incident_dates)).days
        if date_span > MAX_DATE_SPAN_DAYS:
            raise IncidentGroupError(
                f"Incident date span of {date_span} days exceeds the maximum allowed limit of {MAX_DATE_SPAN_DAYS} days"
            )

        # Constraint 4: One current active membership per claim
        active_member_stmt = select(IncidentGroupMember).where(
            IncidentGroupMember.claim_id.in_(claim_ids),
            IncidentGroupMember.is_active == True,  # noqa: E712
        )
        existing_active = list(self.db.scalars(active_member_stmt).all())
        if existing_active:
            raise IncidentGroupError(
                "One or more claims already have an active incident group membership"
            )

        group_id = uuid.uuid4()
        group_ref = f"IG-{utc_now().year}-{uuid.uuid4().hex[:8].upper()}"

        group = IncidentGroup(
            id=group_id,
            group_ref=group_ref,
            customer_id=customer_id,
            status=IncidentGroupStatus.PROPOSED,
            proposed_by_id=actor.id,
            confirmed_by_id=None,
            confirmed_at=None,
            proposal_reason=clean_reason,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(group)

        # Add initial revision 1 for each claim
        for c in claims:
            member = IncidentGroupMember(
                id=uuid.uuid4(),
                incident_group_id=group_id,
                claim_id=c.id,
                customer_id=customer_id,
                rev=1,
                prev_rev=None,
                is_active=True,
                created_by_id=actor.id,
                remarks="Initial group proposal",
                created_at=utc_now(),
            )
            self.db.add(member)

        # Multi-chain audit logging
        write_audit_log(
            db=self.db,
            action="INCIDENT_GROUP_PROPOSED",
            entity_type="IncidentGroup",
            entity_id=str(group_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=None,
            event_data={
                "group_ref": group_ref,
                "customer_id": str(customer_id),
                "member_claim_ids": [str(c.id) for c in claims],
                "reason_sha256": hash_free_text(clean_reason),
            },
        )
        for c in claims:
            write_audit_log(
                db=self.db,
                action="CLAIM_INCIDENT_GROUP_PROPOSED",
                entity_type="Claim",
                entity_id=str(c.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=c.id,
                event_data={"group_id": str(group_id), "group_ref": group_ref},
            )

        self.db.commit()
        self.db.refresh(group)
        return group

    def confirm_group(
        self,
        actor: User,
        group_id: uuid.UUID,
        remarks: str,
    ) -> IncidentGroup:
        """Assessor or manager confirms incident group with mechanical four-eyes separation (proposer != confirmer)."""
        if actor.role not in (UserRole.ASSESSOR, UserRole.MANAGER):
            raise PermissionDeniedError(
                f"Role '{actor.role.value}' is not authorized to confirm incident groups"
            )

        group = self.db.get(IncidentGroup, group_id)
        if not group:
            raise IncidentGroupError(f"Incident group '{group_id}' not found")

        if group.status != IncidentGroupStatus.PROPOSED:
            raise IncidentGroupError(
                f"Incident group is in status '{group.status.value}', cannot confirm"
            )

        # Four-eyes constraint: confirmer cannot be proposer
        if group.proposed_by_id == actor.id:
            raise PermissionDeniedError(
                "Four-eyes violation: proposer cannot confirm their own incident group"
            )

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 10:
            raise IncidentGroupError("Confirmation remarks must be at least 10 characters long")

        group.status = IncidentGroupStatus.CONFIRMED
        group.confirmed_by_id = actor.id
        group.confirmed_at = utc_now()
        group.updated_at = utc_now()

        # Update member claims with incident_group_id
        active_members_stmt = select(IncidentGroupMember).where(
            IncidentGroupMember.incident_group_id == group_id,
            IncidentGroupMember.is_active == True,  # noqa: E712
        )
        active_members = list(self.db.scalars(active_members_stmt).all())
        member_claim_ids = [m.claim_id for m in active_members]

        claims = list(self.db.scalars(select(Claim).where(Claim.id.in_(member_claim_ids))).all())
        for c in claims:
            c.incident_group_id = group.id

        # Multi-chain audit logging
        write_audit_log(
            db=self.db,
            action="INCIDENT_GROUP_CONFIRMED",
            entity_type="IncidentGroup",
            entity_id=str(group_id),
            actor_role=actor.role.value,
            actor_id=actor.id,
            claim_id=None,
            event_data={
                "group_ref": group.group_ref,
                "confirmed_by_id": str(actor.id),
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )
        for c in claims:
            write_audit_log(
                db=self.db,
                action="CLAIM_INCIDENT_GROUP_LINKED",
                entity_type="Claim",
                entity_id=str(c.id),
                actor_role=actor.role.value,
                actor_id=actor.id,
                claim_id=c.id,
                event_data={"group_id": str(group.id), "group_ref": group.group_ref},
            )

        self.db.commit()
        self.db.refresh(group)
        return group

    def unlink_claim(
        self,
        manager: User,
        group_id: uuid.UUID,
        claim_id: uuid.UUID,
        reason: str,
        remarks: str,
    ) -> IncidentGroup:
        """Manager-only unlinking of a claim from an incident group with append-only revisions (P6 Task 9)."""
        if manager.role != UserRole.MANAGER:
            raise PermissionDeniedError(
                "Only managers are authorized to unlink claims from incident groups"
            )

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 20:
            raise IncidentGroupError("Unlink remarks must be at least 20 characters long")

        group = self.db.get(IncidentGroup, group_id)
        if not group:
            raise IncidentGroupError(f"Incident group '{group_id}' not found")

        claim = self.db.get(Claim, claim_id)
        if not claim:
            raise IncidentGroupError(f"Claim '{claim_id}' not found")
        check_claim_access(manager, claim, action="MUTATE")

        if claim.status in FINAL_STATES:
            raise IncidentGroupError("Cannot unlink claim in final status; membership is frozen")

        # Find latest active membership record
        member_stmt = (
            select(IncidentGroupMember)
            .where(
                IncidentGroupMember.incident_group_id == group_id,
                IncidentGroupMember.claim_id == claim_id,
                IncidentGroupMember.is_active == True,  # noqa: E712
            )
            .order_by(IncidentGroupMember.rev.desc())
        )
        prev_member = self.db.scalars(member_stmt).first()
        if not prev_member:
            raise IncidentGroupError("Claim is not an active member of this incident group")

        # Append-only revision: mark prev as not active, insert rev+1 with is_active=False
        prev_member.is_active = False

        new_member = IncidentGroupMember(
            id=uuid.uuid4(),
            incident_group_id=group_id,
            claim_id=claim_id,
            customer_id=group.customer_id,
            rev=prev_member.rev + 1,
            prev_rev=prev_member.rev,
            is_active=False,
            created_by_id=manager.id,
            remarks=f"Unlinked by manager: {clean_remarks}",
            created_at=utc_now(),
        )
        self.db.add(new_member)
        claim.incident_group_id = None

        write_audit_log(
            db=self.db,
            action="INCIDENT_GROUP_CLAIM_UNLINKED",
            entity_type="IncidentGroup",
            entity_id=str(group_id),
            actor_role=manager.role.value,
            actor_id=manager.id,
            claim_id=claim.id,
            event_data={
                "group_ref": group.group_ref,
                "claim_id": str(claim_id),
                "reason": reason,
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )

        self.db.commit()
        self.db.refresh(group)
        return group

    def merge_groups(
        self,
        manager: User,
        target_group_id: uuid.UUID,
        source_group_id: uuid.UUID,
        reason: str,
        remarks: str,
    ) -> IncidentGroup:
        """Manager-only merging of two incident groups belonging to the same customer."""
        if manager.role != UserRole.MANAGER:
            raise PermissionDeniedError("Only managers are authorized to merge incident groups")

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 20:
            raise IncidentGroupError("Merge remarks must be at least 20 characters long")

        target_group = self.db.get(IncidentGroup, target_group_id)
        source_group = self.db.get(IncidentGroup, source_group_id)
        if not target_group or not source_group:
            raise IncidentGroupError("Target or source incident group not found")

        if target_group.customer_id != source_group.customer_id:
            raise IncidentGroupError(
                "Cannot merge incident groups belonging to different customers"
            )

        # Fetch active members of source
        source_members_stmt = select(IncidentGroupMember).where(
            IncidentGroupMember.incident_group_id == source_group_id,
            IncidentGroupMember.is_active == True,  # noqa: E712
        )
        source_members = list(self.db.scalars(source_members_stmt).all())

        # Deactivate in source, add to target
        for m in source_members:
            m.is_active = False
            new_m = IncidentGroupMember(
                id=uuid.uuid4(),
                incident_group_id=target_group_id,
                claim_id=m.claim_id,
                customer_id=target_group.customer_id,
                rev=1,
                prev_rev=None,
                is_active=True,
                created_by_id=manager.id,
                remarks=f"Merged from {source_group.group_ref}: {clean_remarks}",
                created_at=utc_now(),
            )
            self.db.add(new_m)

            claim = self.db.get(Claim, m.claim_id)
            if claim:
                claim.incident_group_id = target_group_id

        source_group.status = IncidentGroupStatus.DISSOLVED
        source_group.updated_at = utc_now()
        target_group.updated_at = utc_now()

        write_audit_log(
            db=self.db,
            action="INCIDENT_GROUPS_MERGED",
            entity_type="IncidentGroup",
            entity_id=str(target_group_id),
            actor_role=manager.role.value,
            actor_id=manager.id,
            claim_id=None,
            event_data={
                "target_group_ref": target_group.group_ref,
                "source_group_ref": source_group.group_ref,
                "merged_claim_count": len(source_members),
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )

        self.db.commit()
        self.db.refresh(target_group)
        return target_group

    def split_group(
        self,
        manager: User,
        source_group_id: uuid.UUID,
        claim_ids_to_split: list[uuid.UUID],
        reason: str,
        remarks: str,
    ) -> tuple[IncidentGroup, IncidentGroup]:
        """Manager-only splitting of an incident group."""
        if manager.role != UserRole.MANAGER:
            raise PermissionDeniedError("Only managers are authorized to split incident groups")

        clean_remarks = remarks.strip()
        if len(clean_remarks) < 20:
            raise IncidentGroupError("Split remarks must be at least 20 characters long")

        source_group = self.db.get(IncidentGroup, source_group_id)
        if not source_group:
            raise IncidentGroupError(f"Incident group '{source_group_id}' not found")

        if len(claim_ids_to_split) < 1:
            raise IncidentGroupError("Must specify at least 1 claim to split")

        # Deactivate specified claims in source group
        new_group_id = uuid.uuid4()
        new_group_ref = f"IG-{utc_now().year}-{uuid.uuid4().hex[:8].upper()}"
        new_group = IncidentGroup(
            id=new_group_id,
            group_ref=new_group_ref,
            customer_id=source_group.customer_id,
            status=IncidentGroupStatus.CONFIRMED,
            proposed_by_id=manager.id,
            confirmed_by_id=manager.id,
            confirmed_at=utc_now(),
            proposal_reason=f"Split from {source_group.group_ref}: {reason}",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(new_group)

        for cid in claim_ids_to_split:
            active_m_stmt = select(IncidentGroupMember).where(
                IncidentGroupMember.incident_group_id == source_group_id,
                IncidentGroupMember.claim_id == cid,
                IncidentGroupMember.is_active == True,  # noqa: E712
            )
            m = self.db.scalars(active_m_stmt).first()
            if m:
                m.is_active = False
                new_m = IncidentGroupMember(
                    id=uuid.uuid4(),
                    incident_group_id=new_group_id,
                    claim_id=cid,
                    customer_id=source_group.customer_id,
                    rev=1,
                    prev_rev=None,
                    is_active=True,
                    created_by_id=manager.id,
                    remarks=f"Split from {source_group.group_ref}: {clean_remarks}",
                    created_at=utc_now(),
                )
                self.db.add(new_m)

                claim = self.db.get(Claim, cid)
                if claim:
                    claim.incident_group_id = new_group_id

        write_audit_log(
            db=self.db,
            action="INCIDENT_GROUP_SPLIT",
            entity_type="IncidentGroup",
            entity_id=str(source_group_id),
            actor_role=manager.role.value,
            actor_id=manager.id,
            claim_id=None,
            event_data={
                "source_group_ref": source_group.group_ref,
                "new_group_ref": new_group_ref,
                "split_claims_count": len(claim_ids_to_split),
                "remarks_sha256": hash_free_text(clean_remarks),
            },
        )

        self.db.commit()
        self.db.refresh(source_group)
        self.db.refresh(new_group)
        return source_group, new_group
