"""
Payments package
================

Bring-Your-Own PayU payment gateway for SocioChat's three subscription layers:

* End-user buys a SocioChat plan  -> settles to the PLATFORM PayU account.
* Tenant's end-user buys the tenant's plan -> settles to the TENANT's own PayU
  account (the tenant configures its key+salt in Integration settings).
* Tenant buys a white-label LICENSE from SocioChat -> settles to the PLATFORM.

Plans only activate AFTER a payment is verified (hash match + status=success),
never on plan-selection alone, so the paid tiers cannot be bypassed. Free plans
(beta / price 0 / null) skip payment entirely.
"""

from payments.routes import payments_bp  # noqa: F401

__all__ = ["payments_bp"]
