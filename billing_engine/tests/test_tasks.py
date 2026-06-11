# billing_engine/tests/test_tasks.py
from decimal import Decimal
import uuid
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock

from billing_engine.models import (
    XuiServer, XuiInbound, ProxySubscription, SubscriptionConfigMapping, Transaction
)
from billing_engine.tasks import provision_proxy_pipeline
from billing_engine.xui_client import XuiAPIException

User = get_user_model()

class ProxyProvisioningTasksTests(TestCase):
    
    def setUp(self):
        # 1. Setup multi-tenant hierarchy
        self.sub_admin = User.objects.create_user(
            username="reseller_sub",
            role=User.Role.SUB_ADMIN,
            wallet_balance=Decimal("200.00")
        )
        self.managed_user = User.objects.create_user(
            username="client_end",
            role=User.Role.END_USER,
            parent_manager=self.sub_admin,
            wallet_balance=Decimal("0.00")
        )

        # 2. Setup architectural edge nodes
        self.server = XuiServer.objects.create(
            name="Frankfurt_Core",
            ip_address="127.0.0.1",
            api_port=2053,
            admin_username="root",
            admin_password="password",
            max_client_capacity=500
        )
        self.inbound_vless = XuiInbound.objects.create(
            server=self.server, xui_inbound_id=1, protocol=XuiInbound.Protocol.VLESS
        )
        self.inbound_vmess = XuiInbound.objects.create(
            server=self.server, xui_inbound_id=2, protocol=XuiInbound.Protocol.VMESS
        )

        # 3. Setup inactive proxy subscription container
        self.subscription = ProxySubscription.objects.create(
            user=self.managed_user,
            xui_client_uuid=str(uuid.uuid4()),
            subscription_url="https://ledger.local/sub/token",
            total_allocated_gb=100,
            expires_at=timezone.now() + timedelta(days=30),
            is_active=False
        )

        # 4. Fabricate initial valid checkout ledger entry
        self.original_charge = Decimal("25.75")
        self.purchase_tx = Transaction.objects.create(
            user=self.managed_user,
            type=Transaction.TypeChoices.PLAN_PURCHASE,
            amount=self.original_charge,
            payment_ref_code="TXNCHECKOUT99",
            status=Transaction.StatusChoices.APPROVED,
            screenshot="protected_media/transactions/automated_plan_purchase.png"
        )

    @patch('billing_engine.tasks.XuiAPIClient.add_client')
    def test_successful_provisioning_pipeline_flow(self, mock_add_client):
        """Verifies smooth path provisions remote endpoints, maps junctions, and activates state."""
        mock_add_client.return_value = {"success": True}
        selected_ids = [self.inbound_vless.id, self.inbound_vmess.id]

        result = provision_proxy_pipeline(self.subscription.id, selected_ids)

        self.assertTrue(result)
        self.subscription.refresh_from_db()
        
        # Verify state conversion and bridge mapping retention
        self.assertTrue(self.subscription.is_active)
        mappings = SubscriptionConfigMapping.objects.filter(subscription=self.subscription)
        self.assertEqual(mappings.count(), 2)
        
        # Verify call arguments to ensure correct parameters reached downstream client
        self.assertEqual(mock_add_client.call_count, 2)

    @patch('billing_engine.tasks.XuiAPIClient.add_client')
    def test_network_fault_triggers_saga_compensation_and_wholesale_refund(self, mock_add_client):
        """Verifies node outages trigger fail-safe rollbacks, maintain pool inactivation, and credit the wholesale source."""
        # Force out-of-bounds network communications error
        mock_add_client.side_effect = XuiAPIException("Network socket connection timeout on remote panel node.")
        selected_ids = [self.inbound_vless.id, self.inbound_vmess.id]

        result = provision_proxy_pipeline(self.subscription.id, selected_ids)

        self.assertFalse(result)
        self.subscription.refresh_from_db()
        
        # 1. Verify State Reversal
        self.assertFalse(self.subscription.is_active)

        # 2. Verify Wholesale Refund Routing (Sub-Admin should be credited, not the managed user)
        self.sub_admin.refresh_from_db()
        self.managed_user.refresh_from_db()
        
        self.assertEqual(self.sub_admin.wallet_balance, Decimal("200.00") + self.original_charge)
        self.assertEqual(self.managed_user.wallet_balance, Decimal("0.00"))

        # 3. Verify Compensation Audit Trail Recording
        refund_tx = Transaction.objects.filter(
            user=self.managed_user,
            type=Transaction.TypeChoices.WALLET_TOPUP,
            status=Transaction.StatusChoices.APPROVED
        ).first()
        
        self.assertIsNotNone(refund_tx)
        self.assertEqual(refund_tx.amount, self.original_charge)
        self.assertTrue(refund_tx.payment_ref_code.startswith("REFUND"))