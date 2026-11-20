from functools import cached_property
import sys
from typing import Protocol
from uuid import uuid4

from keyring.errors import KeyringError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .db import ModelCredentialPending, transaction


class CredentialStore(Protocol):
    def get(self, reference: str) -> str | None: ...
    def put(self, reference: str, value: str) -> None: ...
    def discard(self, reference: str) -> None: ...


CREDENTIAL_FAILURES = (KeyringError, OSError, RuntimeError, ImportError)


class NativeCredentialStore:
    scope = "model"
    username = "model-api-key"

    @cached_property
    def backend(self):
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        else:
            raise KeyringError("No supported system credential store.")
        if Keyring.priority <= 0:
            raise KeyringError("System credential store is unavailable.")
        return Keyring()

    @classmethod
    def service(cls, reference: str) -> str:
        # One service per version also avoids Windows Credential Manager's shared service target.
        return f"智学/{cls.scope}/{reference}"

    def get(self, reference: str) -> str | None:
        return self.backend.get_password(self.service(reference), self.username)

    def put(self, reference: str, value: str) -> None:
        self.backend.set_password(self.service(reference), self.username, value)

    def discard(self, reference: str) -> None:
        if self.get(reference) is not None:
            self.backend.delete_password(self.service(reference), self.username)


def reserve_credential(engine) -> str:
    reference = str(uuid4())
    with transaction(engine) as db:
        db.add(ModelCredentialPending(reference=reference, reserved=True))
    return reference


def retry_credential_cleanup(
    engine, store: CredentialStore, *, startup: bool = False, abandon: str | None = None,
) -> bool:
    """Retire keys detached from connections, including interrupted saves after restarting."""
    try:
        with transaction(engine) as db:
            if abandon:
                reserved = db.get(ModelCredentialPending, abandon)
                if reserved is not None:
                    reserved.reserved = False
            pending = select(ModelCredentialPending)
            if not startup:
                pending = pending.where(ModelCredentialPending.reserved.is_(False))
            failed = False
            for item in db.scalars(pending):
                # The writer must still find its reservation before it can store a key.
                item.reserved = False
                try:
                    store.discard(item.reference)
                except CREDENTIAL_FAILURES:
                    failed = True
                else:
                    db.delete(item)
            return failed
    except SQLAlchemyError:
        # The detached references are already durable and can be retried later.
        return True
