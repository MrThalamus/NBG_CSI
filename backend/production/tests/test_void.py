"""VOID processing, delayed references and deterministic pending resolution."""

from django.db import IntegrityError, transaction
from django.test import TestCase

from production.models import AppliedReversal, ProductionEvent, SubmissionAttempt
from production.tests.helpers import ApiMixin, count, summary_dict, void


class VoidTests(ApiMixin, TestCase):
    def test_t03_void_before_target_count(self):
        result = self.submit(void("V-1", "C-7"))[0]
        self.assertEqual(result["status"], "PENDING_REFERENCE")
        self.assertEqual(self.summary(), summary_dict(0, 0, 0, 1, 0, 0))

        self.assertEqual(self.statuses(count("C-7", 7)), ["ACCEPTED"])
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 0, 0))
        stored_void = ProductionEvent.objects.get(event_id="V-1")
        self.assertEqual(stored_void.processing_status, "ACCEPTED")
        self.assertIsNotNone(stored_void.acknowledged_at)
        reversal = AppliedReversal.objects.get()
        self.assertEqual((reversal.target.event_id, reversal.void.event_id, reversal.quantity), ("C-7", "V-1", 7))
        # The first submission result remains historical evidence.
        self.assertEqual(SubmissionAttempt.objects.get(event_id="V-1").outcome, "PENDING_REFERENCE")

    def test_t11_immediate_void_then_second_void(self):
        self.submit(count("C-1", 5))
        first = self.submit(void("V-1", "C-1"))[0]
        self.assertEqual(first["status"], "ACCEPTED")
        second = self.submit(void("V-2", "C-1"))[0]
        self.assertEqual((second["status"], second["error_code"]), ("REJECTED", "ALREADY_REVERSED"))
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 0, 0))
        # Replaying the winning VOID is a duplicate, never a second reversal.
        self.assertEqual(self.statuses(void("V-1", "C-1")), ["DUPLICATE"])
        self.assertEqual(self.summary()["net_total"], 0)
        self.assertEqual(AppliedReversal.objects.count(), 1)

    def test_void_never_uses_its_own_quantity(self):
        self.submit(count("C-1", 5))
        self.assertEqual(self.submit(void("V-1", "C-1", quantity=99))[0]["status"], "REJECTED")
        self.assertEqual(self.summary()["net_total"], 5)

    def test_t12_wrong_source_self_target_and_void_target(self):
        self.submit([count("C-1", 5, source_id="LINE-01"), count("C-2", 3), void("V-1", "C-2")])
        cases = [
            (void("V-X", "C-1", source_id="LINE-02"), "SOURCE_MISMATCH"),
            (void("V-S", "V-S"), "SELF_REFERENCE"),
            (void("V-V", "V-1"), "TARGET_NOT_COUNT"),
        ]
        for payload, code in cases:
            result = self.submit(payload)[0]
            self.assertEqual((result["status"], result["error_code"]), ("REJECTED", code))
            self.assertFalse(ProductionEvent.objects.filter(event_id=payload["event_id"]).exists())
        self.assertEqual(self.summary(), summary_dict(5, 3, 2, 0, 0, 0))

    def test_t13_several_pending_voids_first_valid_wins(self):
        self.submit([void("V-A", "C-1"), void("V-B", "C-1"), void("V-C", "C-1")])
        self.assertEqual(self.summary()["unresolved"], 3)
        self.submit(count("C-1", 9))
        states = dict(ProductionEvent.objects.filter(type="VOID").values_list("event_id", "processing_status"))
        self.assertEqual(states, {"V-A": "ACCEPTED", "V-B": "REJECTED", "V-C": "REJECTED"})
        self.assertEqual(ProductionEvent.objects.get(event_id="V-B").failure_code, "ALREADY_REVERSED")
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 0, 0))

    def test_t14_invalid_earlier_candidate_does_not_block_valid_later(self):
        self.submit([void("V-WRONG", "C-1", source_id="LINE-02"), void("V-RIGHT", "C-1", source_id="LINE-01")])
        self.submit(count("C-1", 4, source_id="LINE-01"))
        wrong = ProductionEvent.objects.get(event_id="V-WRONG")
        self.assertEqual((wrong.processing_status, wrong.failure_code), ("REJECTED", "SOURCE_MISMATCH"))
        self.assertEqual(ProductionEvent.objects.get(event_id="V-RIGHT").processing_status, "ACCEPTED")
        self.assertEqual(self.summary()["net_total"], 0)

    def test_t15_arriving_target_is_a_void(self):
        self.submit(void("V-WAIT", "X-1"))
        self.submit(count("C-9", 2))
        self.assertEqual(self.statuses(void("X-1", "C-9")), ["ACCEPTED"])
        waiting = ProductionEvent.objects.get(event_id="V-WAIT")
        self.assertEqual((waiting.processing_status, waiting.failure_code), ("REJECTED", "TARGET_NOT_COUNT"))
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 0, 0))

    def test_arriving_pending_void_invalidates_references_to_it(self):
        self.submit(void("V-WAIT", "X-1"))
        self.assertEqual(self.statuses(void("X-1", "MISSING")), ["PENDING_REFERENCE"])
        self.assertEqual(ProductionEvent.objects.get(event_id="V-WAIT").processing_status, "REJECTED")

    def test_pending_void_reserves_its_id(self):
        self.submit(void("V-1", "C-1"))
        self.assertEqual(self.statuses(void("V-1", "C-1")), ["DUPLICATE"])
        self.assertEqual(self.statuses(count("V-1", 3)), ["CONFLICT"])
        # A duplicate of a pending VOID stays a duplicate after it is rejected.
        self.submit(count("C-1", 3, source_id="LINE-02"))
        self.assertEqual(ProductionEvent.objects.get(event_id="V-1").processing_status, "REJECTED")
        self.assertEqual(self.statuses(void("V-1", "C-1")), ["DUPLICATE"])

    def test_replayed_count_does_not_resolve_again(self):
        self.submit([count("C-1", 5), void("V-1", "C-1")])
        self.submit(void("V-2", "C-1"))  # rejected immediately: already reversed
        self.assertEqual(self.statuses(count("C-1", 5)), ["DUPLICATE"])
        self.assertEqual(AppliedReversal.objects.count(), 1)
        self.assertEqual(self.summary()["net_total"], 0)


class ConstraintTests(ApiMixin, TestCase):
    def test_database_enforces_one_reversal_per_count_and_unique_ids(self):
        self.submit([count("C-1", 5), void("V-1", "C-1"), count("C-2", 1)])
        c1 = ProductionEvent.objects.get(event_id="C-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AppliedReversal.objects.create(
                target=c1, void=ProductionEvent.objects.get(event_id="C-2"), quantity=5, applied_at=c1.received_at
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductionEvent.objects.create(
                event_id="C-1", source_id="LINE-01", type="COUNT", quantity=1,
                normalized_payload={}, event_time=c1.event_time, received_at=c1.received_at,
                processing_status="ACCEPTED",
            )

    def test_database_enforces_event_shape(self):
        self.submit(count("C-1", 5))
        base = ProductionEvent.objects.get(event_id="C-1")
        bad_rows = [
            {"type": "COUNT", "quantity": None, "processing_status": "ACCEPTED"},
            {"type": "COUNT", "quantity": 0, "processing_status": "ACCEPTED"},
            {"type": "VOID", "quantity": 3, "target_event_id": "C-1", "processing_status": "ACCEPTED"},
            {"type": "VOID", "quantity": None, "target_event_id": None, "processing_status": "ACCEPTED"},
            {"type": "COUNT", "quantity": 1, "processing_status": "PENDING_REFERENCE"},
            {"type": "ADJUST", "quantity": 1, "processing_status": "ACCEPTED"},
        ]
        for index, fields in enumerate(bad_rows):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                ProductionEvent.objects.create(
                    event_id=f"BAD-{index}", source_id="LINE-01", normalized_payload={},
                    event_time=base.event_time, received_at=base.received_at, **fields,
                )
