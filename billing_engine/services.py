import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional
from django.db import transaction
from billing_engine.models import CustomUser, VPNPlan, PricingTier, Transaction

logger = logging.getLogger(__name__)

class InsufficientFundsError(Exception):
    """Raised when an account balance is lower than the computed transaction cost."""
    pass


def process_purchase(
    buyer: CustomUser, 
    plan: Optional[VPNPlan] = None, 
    custom_gb: Optional[int] = None, 
    custom_days: Optional[int] = None
) -> Dict[str, Any]:
    """
    Executes a transactional package purchase sequence. Handles fluid input normalization,
    tiered and wholesale pricing routing, and enforces row-level database mutex locks 
    to prevent double-spend concurrency vulnerabilities.
    """
    # ------------------------------------------------------------------------
    # 1. Input Evaluation & Normalization
    # ------------------------------------------------------------------------
    if plan is not None:
        if custom_gb is not None or custom_days is not None:
            raise ValueError("Ambiguous parameters provided. Provide either a strict VPNPlan OR fluid custom limits.")
        total_gb = plan.total_gb
        duration_days = plan.duration_days
    else:
        if custom_gb is None or custom_days is None:
            raise ValueError("Insufficient parameters. Must provide a valid VPNPlan instance or custom_gb AND custom_days metrics.")
        if custom_gb <= 0 or custom_days <= 0:
            raise ValueError("Fluid custom dimensions must be strictly positive integers.")
        total_gb = custom_gb
        duration_days = custom_days

    # ------------------------------------------------------------------------
    # 2. Wholesale Accounting Routing
    # ------------------------------------------------------------------------
    if buyer.role == CustomUser.Role.END_USER and buyer.parent_manager is not None:
        billing_account = buyer.parent_manager
        pricing_target = buyer.parent_manager
        is_wholesale = True
        logger.info(f"Wholesale routing engaged. EndUser {buyer.id} purchase mapped to SubAdmin {billing_account.id}.")
    else:
        billing_account = buyer
        pricing_target = buyer
        is_wholesale = False

    # ------------------------------------------------------------------------
    # 3. Tiered Cost Calculation via Strict Decimal Mathematics
    # ------------------------------------------------------------------------
    tier = PricingTier.objects.filter(specific_user=pricing_target).first()
    
    if not tier:
        tier = PricingTier.objects.filter(target_role=pricing_target.role, specific_user__isnull=True).first()

    if not tier:
        raise ValueError(f"Operational hazard: No PricingTier layout configured for target entity role: '{pricing_target.role}'.")

    gbs_decimal = Decimal(str(total_gb))
    days_decimal = Decimal(str(duration_days))
    
    # Raw cost can yield 4 decimal places based on PricingTier constraints
    raw_cost = (gbs_decimal * tier.price_per_gb) + (days_decimal * tier.price_per_day)
    
    # FIX 1: Quantize cost to 2 decimal places to match the Transaction model constraints
    total_cost = raw_cost.quantize(Decimal('0.01'))

    # ------------------------------------------------------------------------
    # 4. Concurrency & Database Mutex Lock Blocks
    # ------------------------------------------------------------------------
    with transaction.atomic():
        locked_account = CustomUser.objects.select_for_update().get(pk=billing_account.pk)
        
        if locked_account.wallet_balance < total_cost:
            raise InsufficientFundsError(
                f"Transaction aborted. Account {locked_account.id} balance is {locked_account.wallet_balance}; "
                f"Required allocation cost is {total_cost}."
            )

        locked_account.wallet_balance -= total_cost
        locked_account.save(update_fields=['wallet_balance'])

        # FIX 2: Generate a pure uppercase alphanumeric reference code (no hyphens)
        payment_ref = f"TXN{uuid.uuid4().hex.upper()[:12]}"
        
        # FIX 3: Add explicit system choices and a placeholder screenshot string to pass model clean validation
        ledger_entry = Transaction.objects.create(
            user=buyer,
            type=Transaction.TypeChoices.PLAN_PURCHASE,
            amount=total_cost,
            payment_ref_code=payment_ref,
            status=Transaction.StatusChoices.APPROVED,
            screenshot="protected_media/transactions/automated_plan_purchase.png"
        )

        logger.info(f"Purchase transaction verified. Ref: {payment_ref}. Debited Account: {locked_account.id}.")

    return {
        "status": "SUCCESS",
        "debited_account_id": str(locked_account.id),
        "is_wholesale": is_wholesale,
        "amount_charged": total_cost,
        "payment_ref_code": payment_ref,
        "ledger_transaction_id": str(ledger_entry.id)
    }