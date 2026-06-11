# billing_engine/tasks.py
import logging
import uuid
from django.db import transaction
from django.conf import settings
from celery import shared_task
from billing_engine.models import (
    ProxySubscription, XuiInbound, SubscriptionConfigMapping, CustomUser, Transaction
)
from billing_engine.xui_client import XuiAPIClient, XuiAPIException

logger = logging.getLogger(__name__)

@shared_task
def provision_proxy_pipeline(subscription_id: int, selected_inbound_ids: list) -> bool:
    """
    Asynchronously provisions xray proxy credentials across multiple 3x-ui edge nodes.
    Implements a programmatic Saga-style compensation rollback if any remote node deployment fails.
    """
    try:
        subscription = ProxySubscription.objects.select_related('user').get(pk=subscription_id)
    except ProxySubscription.DoesNotExist:
        logger.critical(f"Provisioning aborted. ProxySubscription ID {subscription_id} not found.")
        return False

    buyer = subscription.user
    domain = settings.ALLOWED_HOSTS[0] if (hasattr(settings, 'ALLOWED_HOSTS') and settings.ALLOWED_HOSTS) else 'ledger.local'
    
    try:
        # Core Provisioning Loop
        for inbound_id in selected_inbound_ids:
            try:
                inbound = XuiInbound.objects.select_related('server').get(pk=inbound_id)
            except XuiInbound.DoesNotExist:
                raise XuiAPIException(f"Configuration fault: XuiInbound ID {inbound_id} missing from database.")

            client = XuiAPIClient(inbound.server)
            email = f"user_{buyer.id}_{inbound.id}@{domain}"

            logger.info(f"Deploying client token to server '{inbound.server.name}' for inbound port ID {inbound.xui_inbound_id}")
            
            # Fire outbound network call (encapsulates internal retry policies)
            client.add_client(
                inbound_id=inbound.xui_inbound_id,
                client_uuid=subscription.xui_client_uuid,
                email=email
            )

            # Junction Mapping Preservation (persisted instantly on HTTP success)
            SubscriptionConfigMapping.objects.create(
                subscription=subscription,
                inbound=inbound,
                xui_client_email=email
            )

        # Step 2 Compliance: Activate the pool once all nodes have verified deployment
        subscription.is_active = True
        subscription.save(update_fields=['is_active'])
        logger.info(f"Successfully provisioned all protocols for Subscription {subscription.id}.")
        return True

    except XuiAPIException as exc:
        logger.critical(
            f"Distributed Provisioning Failure on Subscription {subscription_id}: {exc}. "
            f"Halting deployment and executing fail-safe rollback compensation."
        )
        
        # 1. State Reversal
        subscription.is_active = False
        subscription.save(update_fields=['is_active'])

        # 2. Financial Refund Routing Calculation
        # Locate the original charge to avoid hardcoded pricing drifts
        latest_purchase = Transaction.objects.filter(
            user=buyer,
            type=Transaction.TypeChoices.PLAN_PURCHASE,
            status=Transaction.StatusChoices.APPROVED
        ).first()

        if latest_purchase:
            refund_amount = latest_purchase.amount
        else:
            logger.error(f"Rollback Anomaly: No matching purchase ledger found for user {buyer.id}. Refund halted.")
            refund_amount = 0

        # Evaluate wholesale vs retail billing routing context
        if buyer.role == CustomUser.Role.END_USER and buyer.parent_manager is not None:
            refund_target = buyer.parent_manager
            logger.warning(f"Wholesale refund route engaged. Crediting SubAdmin: {refund_target.username}")
        else:
            refund_target = buyer
            logger.warning(f"Retail refund route engaged. Crediting User: {refund_target.username}")

        # 3. Atomic Protection & Audit Logging
        if refund_amount > 0:
            with transaction.atomic():
                # Enforce row-level mutex lock to avoid balance race conditions
                locked_account = CustomUser.objects.select_for_update().get(pk=refund_target.pk)
                locked_account.wallet_balance += refund_amount
                locked_account.save(update_fields=['wallet_balance'])

                # Generate clean alphanumeric reference code matching models.py validator constraints
                refund_ref = f"REFUND{uuid.uuid4().hex.upper()[:12]}"
                
                Transaction.objects.create(
                    user=buyer,
                    type=Transaction.TypeChoices.WALLET_TOPUP,
                    amount=refund_amount,
                    payment_ref_code=refund_ref,
                    status=Transaction.StatusChoices.APPROVED,
                    screenshot="protected_media/transactions/automated_refund.png"
                )
                
            logger.critical(
                f"[MASTER ALERT] Financial fallback complete. Refunded {refund_amount} to account "
                f"'{locked_account.username}'. Reference Code: {refund_ref}."
            )
        
        return False