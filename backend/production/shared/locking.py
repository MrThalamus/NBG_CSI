"""Global mutation lock shared by REST, ACK and MQTT paths."""

from django.db import transaction

from production.models import ProcessingMutex

MUTEX_NAME = "production"


def acquire_mutation_lock():
    """
    Lock the seeded ProcessingMutex row for the rest of the current transaction.

    Lock order everywhere: this mutex first, then any challenge row. Re-entrant
    within one transaction (PostgreSQL ignores a repeat lock by the same owner).
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("acquire_mutation_lock() must be called inside transaction.atomic()")
    ProcessingMutex.objects.select_for_update().get(name=MUTEX_NAME)
