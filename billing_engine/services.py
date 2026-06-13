import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone

from billing_engine.models import (
    CustomUser, VPNPlan, PricingTier, Transaction,
    ProxySubscription, XuiInbound, SubscriptionConfigMapping, XuiServer,
)
from billing_engine.xui_client import XuiAPIClient, XuiAPIException

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024 ** 3


class InsufficientFundsError(Exception):
    """Raised when wallet balance is lower than the purchase cost."""
    pass


class NoAvailableServerError(Exception):
    """Raised when no active inbound has capacity for a new client."""
    pass


def _pick_inbound() -> XuiInbound:
    """
    Return the least-loaded available inbound.
    Raises NoAvailableServerError if nothing is available.
    """
    inbounds = (
        XuiInbound.objects
        .filter(is_available_for_purchase=True, server__is_active=True)
        .select_related('server')
    )
    if not inbounds.exists():
        raise NoAvailableServerError(
            'No active inbounds are available for purchase. '
            'Ask the admin to add a server or enable an inbound.'
        )
    best = None
    best_count = None
    for ib in inbounds:
        count = SubscriptionConfigMapping.objects.filter(inbound=ib).count()
        if count >= ib.server.max_client_capacity:
            continue
        if best is None or count < best_count:
            best = ib
            best_count = count
    if best is None:
        raise NoAvailableServerError('All available inbounds have reached their client capacity.')
    return best


def _build_subscription_url(server: XuiServer, client_uuid: str) -> str:
    protocol = 'https' if server.use_ssl else 'http'
    host = server.get_host()
    base_path = server.get_base_path()
    return f"{protocol}://{host}:{server.api_port}{base_path}sub/{client_uuid}"


def process_purchase(
    buyer: CustomUser,
    plan: Optional[VPNPlan] = None,
    custom_gb: Optional[int] = None,
    custom_days: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full subscription purchase flow:
      1. Validate inputs & resolve pricing
      2. Pick least-loaded available inbound
      3. Deduct wallet + create DB records (atomic)
      4. Call 3x-ui addClient API (outside atomic)
    """
    # 1. Input normalisation
    if plan is not None:
        if custom_gb is not None or custom_days is not None:
            raise ValueError("Provide either a VPNPlan OR custom_gb/custom_days, not both.")
        total_gb = plan.total_gb
        duration_days = plan.duration_days
    else:
        if custom_gb is None or custom_days is None:
            raise ValueError("Must provide a VPNPlan or both custom_gb AND custom_days.")
        if custom_gb <= 0 or custom_days <= 0:
            raise ValueError("custom_gb and custom_days must be positive integers.")
        total_gb = custom_gb
        duration_days = custom_days

    # 2. Billing account & pricing tier
    if buyer.role == CustomUser.Role.END_USER and buyer.parent_manager is not None:
        billing_account = buyer.parent_manager
        pricing_target = buyer.parent_manager
        is_wholesale = True
    else:
        billing_account = buyer
        pricing_target = buyer
        is_wholesale = False

    tier = (
        PricingTier.objects.filter(specific_user=pricing_target).first()
        or PricingTier.objects.filter(
            target_role=pricing_target.role, specific_user__isnull=True
        ).first()
    )
    if not tier:
        raise ValueError(
            f"No PricingTier configured for role '{pricing_target.role}'. "
            "Ask the admin to set one up."
        )

    total_cost = (
        (Decimal(str(total_gb)) * tier.price_per_gb)
        + (Decimal(str(duration_days)) * tier.price_per_day)
    ).quantize(Decimal('0.01'))

    # 3. Pick inbound (read-only, before lock)
    inbound = _pick_inbound()
    server = inbound.server

    # 4. Atomic billing + DB provisioning
    client_uuid = str(uuid.uuid4())
    payment_ref = f"TXN{uuid.uuid4().hex.upper()[:12]}"
    expires_at = timezone.now() + timedelta(days=duration_days)
    sub_url = _build_subscription_url(server, client_uuid)
    xui_email = f"{buyer.username.split('@')[0]}_{client_uuid[:8]}@vpn.local"
    total_gb_bytes = total_gb * BYTES_PER_GB
    expiry_ms = int(expires_at.timestamp() * 1000)

    with transaction.atomic():
        locked_account = CustomUser.objects.select_for_update().get(pk=billing_account.pk)
        if locked_account.wallet_balance < total_cost:
            raise InsufficientFundsError(
                f"Insufficient balance. Have {locked_account.wallet_balance}, need {total_cost}."
            )
        locked_account.wallet_balance -= total_cost
        locked_account.save(update_fields=['wallet_balance'])

        ledger_entry = Transaction(
            user=buyer,
            type=Transaction.TypeChoices.PLAN_PURCHASE,
            amount=total_cost,
            payment_ref_code=payment_ref,
            status=Transaction.StatusChoices.APPROVED,
            screenshot='',
        )
        ledger_entry.save()

        sub = ProxySubscription.objects.create(
            user=buyer,
            xui_client_uuid=client_uuid,
            subscription_url=sub_url,
            total_allocated_gb=total_gb,
            used_gb=Decimal('0.00'),
            expires_at=expires_at,
            is_active=True,
        )

        SubscriptionConfigMapping.objects.create(
            subscription=sub,
            inbound=inbound,
            xui_client_email=xui_email,
        )

    logger.info('Purchase complete: buyer=%s ref=%s sub=%s', buyer.pk, payment_ref, sub.pk)

    # 5. Provision on 3x-ui (outside atomic — panel failure won't roll back billing)
    try:
        XuiAPIClient(server).add_client(
            inbound_id=inbound.xui_inbound_id,
            client_uuid=client_uuid,
            email=xui_email,
            total_gb=total_gb_bytes,
            expiry_time_ms=expiry_ms,
        )
        logger.info('3x-ui client added: uuid=%s inbound=%s', client_uuid, inbound.xui_inbound_id)
    except XuiAPIException as e:
        logger.error('Failed to add client to 3x-ui for sub %s: %s', sub.pk, e)

    return {
        'status': 'SUCCESS',
        'debited_account_id': str(locked_account.id),
        'is_wholesale': is_wholesale,
        'amount_charged': total_cost,
        'payment_ref_code': payment_ref,
        'ledger_transaction_id': str(ledger_entry.id),
        'subscription_id': str(sub.id),
        'subscription_url': sub_url,
        'client_uuid': client_uuid,
    }
