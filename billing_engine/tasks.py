# billing_engine/tasks.py
import logging
import uuid
from decimal import Decimal, ROUND_DOWN

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing_engine.models import (
    ProxySubscription,
    SubscriptionConfigMapping,
    XuiServer,
    XuiInbound,
    CustomUser,
    Transaction,
)
from billing_engine.xui_client import XuiAPIClient, XuiAPIException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BYTES_PER_GB = Decimal("1073741824")  # 2^30 — exact binary gigabyte


# ---------------------------------------------------------------------------
# Internal Helpers  (sync engine)
# ---------------------------------------------------------------------------

def _bytes_to_gb(byte_count: int) -> Decimal:
    """Convert a raw byte integer to a Decimal gigabyte value, truncated (never rounded up)."""
    return (Decimal(byte_count) / BYTES_PER_GB).quantize(
        Decimal("0.000000001"), rounding=ROUND_DOWN
    )


def _build_email_index(inbounds_payload: list) -> dict:
    """
    Flatten the nested 3x-ui inbound → clientStats structure into a single
    lookup dictionary keyed by the tracking email string.

    Expected remote JSON shape:
        [
            {
                "id": <xui_inbound_id>,
                "clientStats": [
                    {"email": "user_<uid>_<iid>@domain", "up": <int>, "down": <int>},
                    ...
                ]
            },
            ...
        ]

    Returns: { "user_<uid>_<iid>@domain": {"up": int, "down": int, "xui_inbound_id": int} }
    """
    index = {}
    for inbound in inbounds_payload:
        xui_inbound_id = inbound.get("id")
        for client_stat in inbound.get("clientStats") or []:
            email = client_stat.get("email", "").strip()
            if email:
                index[email] = {
                    "up": int(client_stat.get("up", 0)),
                    "down": int(client_stat.get("down", 0)),
                    "xui_inbound_id": xui_inbound_id,
                }
    return index


def _deprovision_subscription(subscription: ProxySubscription, reason: str) -> None:
    """
    Hard-block a subscription:
      1. Flip is_active = False locally.
      2. Call disable_client() on every mapped edge node.
    Errors on individual remote calls are caught and logged so one bad
    deprovision call does not abort the entire sync sweep.
    """
    if subscription.is_active:
        subscription.is_active = False
        subscription.save(update_fields=["is_active", "updated_at"])
        logger.warning(
            "[DEPROVISIONED] Subscription %s — Reason: %s | User: %s",
            subscription.id,
            reason,
            subscription.user.username,
        )

    mappings = subscription.mappings.select_related("inbound__server").all()

    for mapping in mappings:
        server = mapping.inbound.server
        if not server.is_active:
            logger.debug(
                "Skipping disable call on inactive server %s for subscription %s.",
                server.name,
                subscription.id,
            )
            continue
        try:
            client = XuiAPIClient(server)
            client.disable_client(
                inbound_id=mapping.inbound.xui_inbound_id,
                client_uuid=subscription.xui_client_uuid,
            )
            logger.info(
                "[EDGE BLOCK] Disabled UUID %s on server '%s' inbound %s.",
                subscription.xui_client_uuid,
                server.name,
                mapping.inbound.xui_inbound_id,
            )
        except XuiAPIException as exc:
            # Local flag is already False — log and continue. A reconciliation
            # sweep or admin alert can retry the remote call later.
            logger.error(
                "[EDGE BLOCK FAILED] Could not disable UUID %s on server '%s': %s",
                subscription.xui_client_uuid,
                server.name,
                exc,
            )


# ---------------------------------------------------------------------------
# Task 1: Telemetry Synchronization Daemon
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_all_edge_traffic(self) -> None:
    """
    Celery Beat daemon: polls all active 3x-ui edge nodes, applies traffic
    delta accounting with reset-protection, and enforces de-provisioning
    on limit or expiry breaches.

    Execution flow per server:
        → Query remote inbound telemetry via XuiAPIClient.get_inbounds()
        → Build email → stats lookup index
        → For each active ProxySubscription mapped to this server:
            1. Locate remote stats via xui_client_email tracker
            2. Calculate Δ (delta) bytes with reboot-protection logic
            3. Increment used_gb, anchor last_known bytes
            4. Evaluate limit/expiry → deprovision if breached
    """
    active_servers = XuiServer.objects.filter(is_active=True).prefetch_related("inbounds")

    if not active_servers.exists():
        logger.info("[SYNC] No active edge servers found. Skipping cycle.")
        return

    for server in active_servers:
        logger.info(
            "[SYNC] Querying telemetry from server '%s' (%s).",
            server.name,
            server.ip_address,
        )

        try:
            xui_client = XuiAPIClient(server)
            inbounds_payload = xui_client.get_inbounds()
        except XuiAPIException as exc:
            logger.error(
                "[SYNC] Failed to fetch inbounds from server '%s': %s. Skipping.",
                server.name,
                exc,
            )
            continue

        remote_stats_index = _build_email_index(inbounds_payload)

        if not remote_stats_index:
            logger.warning(
                "[SYNC] Server '%s' returned an empty client stats payload.", server.name
            )
            continue

        # Fetch all active subscriptions mapped to any inbound on this server.
        # select_related avoids N+1 on user and server lookups.
        mappings = (
            SubscriptionConfigMapping.objects.filter(
                inbound__server=server,
                subscription__is_active=True,
            )
            .select_related("subscription__user", "inbound")
            .distinct()
        )

        # Guard: a subscription spanning multiple inbounds on the same server
        # must only be processed once per cycle to prevent double-counting.
        processed_subscription_ids = set()

        for mapping in mappings:
            subscription = mapping.subscription

            if subscription.id in processed_subscription_ids:
                continue
            processed_subscription_ids.add(subscription.id)

            # -----------------------------------------------------------
            # Step 1: Locate the remote telemetry record via email tracker
            # -----------------------------------------------------------
            remote_stats = remote_stats_index.get(mapping.xui_client_email)

            if remote_stats is None:
                logger.warning(
                    "[SYNC] Tracker email '%s' not found in remote payload for server '%s'. "
                    "Client may have been manually removed from the panel.",
                    mapping.xui_client_email,
                    server.name,
                )
                continue

            incoming_up: int = remote_stats["up"]
            incoming_down: int = remote_stats["down"]
            incoming_absolute: int = incoming_up + incoming_down

            # -----------------------------------------------------------
            # Step 2: Traffic Reset Protection — Delta Calculation
            # -----------------------------------------------------------
            last_known_absolute: int = (
                subscription.last_known_up_bytes + subscription.last_known_down_bytes
            )

            if incoming_absolute >= last_known_absolute:
                # Condition A: Normal incremental flow
                delta_bytes: int = incoming_absolute - last_known_absolute
            else:
                # Condition B: Edge node rebooted — counters rolled back to zero.
                # Treat the entire incoming value as new usage since last anchor.
                delta_bytes = incoming_absolute
                logger.warning(
                    "[RESET DETECTED] Server '%s' counter rollback detected for subscription %s. "
                    "Incoming: %d bytes < Last known: %d bytes. Treating as fresh delta.",
                    server.name,
                    subscription.id,
                    incoming_absolute,
                    last_known_absolute,
                )

            delta_gb: Decimal = _bytes_to_gb(delta_bytes)

            # -----------------------------------------------------------
            # Step 3: Persist incremental usage and update byte anchors
            # -----------------------------------------------------------
            subscription.used_gb = (subscription.used_gb or Decimal("0")) + delta_gb
            subscription.last_known_up_bytes = incoming_up
            subscription.last_known_down_bytes = incoming_down
            subscription.save(
                update_fields=[
                    "used_gb",
                    "last_known_up_bytes",
                    "last_known_down_bytes",
                    "updated_at",
                ]
            )

            logger.debug(
                "[SYNC] Subscription %s | Δ=%s GB | used_gb=%s | up=%d down=%d",
                subscription.id,
                delta_gb,
                subscription.used_gb,
                incoming_up,
                incoming_down,
            )

            # -----------------------------------------------------------
            # Step 4: Automated De-provisioning Enforcement
            # -----------------------------------------------------------
            now = timezone.now()
            limit_breached = subscription.used_gb >= subscription.total_allocated_gb
            time_expired = now > subscription.expires_at

            if limit_breached or time_expired:
                reasons = []
                if limit_breached:
                    reasons.append(
                        f"usage {subscription.used_gb:.4f} GB >= limit {subscription.total_allocated_gb} GB"
                    )
                if time_expired:
                    reasons.append(f"expired at {subscription.expires_at.isoformat()}")

                _deprovision_subscription(subscription, reason=" | ".join(reasons))

    logger.info("[SYNC] Telemetry synchronization cycle complete.")


# ---------------------------------------------------------------------------
# Task 2: Saga-Pattern Provisioning Pipeline
# ---------------------------------------------------------------------------

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

        # Activate the pool once all nodes have verified deployment
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
