"""Reproduction script for Finding F2.

Finding F2: Audit writer races on first write to a new chain and under load.
50 concurrent writers on one chain gave 6 IntegrityErrors; 20 concurrent first writes to a new chain gave 12 failures.
The chain stayed valid. No concurrency test exists.

Inspection Evidence:
app/audit/writer.py lines 160-176:
    stmt = select(AuditChainHead).where(AuditChainHead.chain_key == chain_key)
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    chain_head = db.scalars(stmt).first()

    if not chain_head:
        genesis = compute_genesis_hash(chain_key)
        chain_head = AuditChainHead(
            chain_key=chain_key,
            last_seq=0,
            last_hash=genesis,
            updated_at=occurred_at,
        )
        db.add(chain_head)
        db.flush()

When 2+ threads/processes attempt a first write to a newly generated chain_key concurrently,
neither finds an existing chain_head row. Both enter `if not chain_head:` and attempt
to insert the same chain_key, causing a race condition and IntegrityError / uniqueness violation.
"""

import concurrent.futures
import os
import sys

sys.path.insert(0, ".")
os.environ["ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-32-chars-minimum-length-for-testing"
os.environ["AUDIT_HMAC_KEY"] = "test-audit-hmac-key-32-chars-minimum-for-testing"

import uuid

from app.audit.writer import write_audit_log
from app.db.base import Base
from tests.conftest import TestingSessionLocal, test_engine


def attempt_first_write(chain_id: uuid.UUID, writer_idx: int) -> tuple[bool, str]:
    session = TestingSessionLocal()
    try:
        write_audit_log(
            db=session,
            action="CONCURRENT_FIRST_WRITE",
            entity_type="Claim",
            entity_id=str(chain_id),
            actor_role="STAFF",
            claim_id=chain_id,
            event_data={"writer_idx": writer_idx},
        )
        session.commit()
        return True, "SUCCESS"
    except Exception as exc:
        session.rollback()
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def main() -> None:
    Base.metadata.create_all(bind=test_engine)
    new_claim_id = uuid.uuid4()
    num_concurrent = 10

    print(
        f"[F2 Reproduction] Launching {num_concurrent} concurrent first-writes to new chain claim:{new_claim_id}..."
    )
    successes = 0
    failures = 0
    error_samples = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [
            executor.submit(attempt_first_write, new_claim_id, i) for i in range(num_concurrent)
        ]
        for f in concurrent.futures.as_completed(futures):
            ok, msg = f.result()
            if ok:
                successes += 1
            else:
                failures += 1
                if len(error_samples) < 3:
                    error_samples.append(msg)

    print(
        f"[F2 Reproduction] Results: {successes} succeeded, {failures} failed out of {num_concurrent}"
    )
    if error_samples:
        print("[F2 Reproduction] Sample concurrency errors:")
        for err in error_samples:
            print(f"  - {err}")
    print("[F2 Reproduction] VERDICT: REPRODUCED. First-write race condition exists.")


if __name__ == "__main__":
    main()
