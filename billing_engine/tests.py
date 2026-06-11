# billing_engine/tests.py
from django.test import TestCase
from django.db.utils import IntegrityError
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import uuid

from billing_engine.models import (
    XuiServer, XuiInbound, PricingTier, ProxySubscription, SubscriptionConfigMapping
)

User = get_user_model()


class CustomUserTests(TestCase):
    def setUp(self):
        self.master_admin = User.objects.create_user(
            username="master_root",
            email="master@proxy.net",
            password="securepassword123",
            role=User.Role.MASTER_ADMIN
        )
        self.sub_admin = User.objects.create_user(
            username="sub_dealer_01",
            email="sub01@proxy.net",
            password="securepassword123",
            role=User.Role.SUB_ADMIN,
            parent_manager=self.master_admin
        )

    def test_user_uuid_primary_key(self):
        user = User.objects.create_user(
            username="end_user_test",
            email="client@proxy.net",
            password="securepassword123"
        )
        self.assertIsInstance(user.id, uuid.UUID)

    def test_default_user_role(self):
        casual_user = User.objects.create_user(username="casual_user", password="securepassword123")
        self.assertEqual(casual_user.role, User.Role.END_USER)

    def test_multi_tenant_hierarchy(self):
        end_user = User.objects.create_user(
            username="end_consumer",
            password="securepassword123",
            role=User.Role.END_USER,
            parent_manager=self.sub_admin
        )
        self.assertEqual(end_user.parent_manager, self.sub_admin)
        self.assertIn(end_user, self.sub_admin.managed_users.all())

    def test_wallet_balance_decimal_precision(self):
        user = User.objects.create_user(username="wallet_holder", password="password")
        user.wallet_balance = Decimal("10.00")
        user.save()
        user.wallet_balance += Decimal("0.05")
        user.save()
        refetched_user = User.objects.get(id=user.id)
        self.assertEqual(refetched_user.wallet_balance, Decimal("10.05"))


class XuiServerTests(TestCase):
    def test_server_provisioning_and_networking(self):
        server = XuiServer.objects.create(
            name="Frankfurt_Edge_01",
            ip_address="192.168.1.100",
            api_port=8080,
            admin_username="xui_root",
            admin_password="temporary_password",
            max_client_capacity=500
        )
        self.assertIsInstance(server.id, uuid.UUID)


class XuiInboundTests(TestCase):
    def setUp(self):
        self.server = XuiServer.objects.create(
            name="Tokyo_Edge",
            ip_address="2001:db8::1",
            api_port=8081,
            admin_username="tokyo_admin",
            admin_password="password",
            max_client_capacity=200
        )

    def test_json_stream_settings_storage(self):
        settings_payload = {
            "network": "grpc",
            "security": "reality",
            "realitySettings": {"show": False}
        }
        inbound = XuiInbound.objects.create(
            server=self.server,
            xui_inbound_id=1,
            protocol=XuiInbound.Protocol.VLESS,
            stream_settings=settings_payload
        )
        refetched_inbound = XuiInbound.objects.get(id=inbound.id)
        self.assertEqual(refetched_inbound.stream_settings["network"], "grpc")

    def test_composite_unique_inbound_per_server_constraint(self):
        XuiInbound.objects.create(server=self.server, xui_inbound_id=42, protocol=XuiInbound.Protocol.TROJAN)
        with self.assertRaises(IntegrityError):
            XuiInbound.objects.create(server=self.server, xui_inbound_id=42, protocol=XuiInbound.Protocol.VMESS)


class PricingTierTests(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(username="vip_client", password="password", role=User.Role.END_USER)

    def test_global_vs_override_pricing_evaluation(self):
        global_tier = PricingTier.objects.create(
            target_role=PricingTier.TargetRole.END_USER, price_per_gb=Decimal("0.1500"), price_per_day=Decimal("0.0500")
        )
        custom_tier = PricingTier.objects.create(
            target_role=PricingTier.TargetRole.END_USER, specific_user=self.client_user, price_per_gb=Decimal("0.1000"), price_per_day=Decimal("0.0300")
        )
        self.assertIsNone(global_tier.specific_user)
        self.assertEqual(custom_tier.price_per_gb, Decimal("0.1000"))


class ProxyInfrastructureArchitectureTests(TestCase):
    def setUp(self):
        # Create user
        self.user = User.objects.create_user(
            username="testbackenduser", email="test@ledger.internal", password="SecureSafePassword123!"
        )
        
        # Core infrastructure node required for real Inbounds
        self.edge_server = XuiServer.objects.create(
            name="Core_Edge_Node",
            ip_address="142.250.190.46",
            api_port=2053,
            admin_username="admin",
            admin_password="password",
            max_client_capacity=1000
        )
        
        # Instantiate two valid production-spec inbounds mapped to the edge node
        self.german_vless_inbound = XuiInbound.objects.create(
            server=self.edge_server,
            xui_inbound_id=101,
            protocol=XuiInbound.Protocol.VLESS
        )
        self.singapore_trojan_inbound = XuiInbound.objects.create(
            server=self.edge_server,
            xui_inbound_id=102,
            protocol=XuiInbound.Protocol.TROJAN
        )
        
        # Create user pool
        self.subscription = ProxySubscription.objects.create(
            user=self.user,
            xui_client_uuid=str(uuid.uuid4()),
            subscription_url="https://ledger.internal/sub/v1/stream-token",
            total_allocated_gb=100,
            expires_at=timezone.now() + timedelta(days=30)
        )

    def test_multi_protocol_scaling_mapping_and_unique_email_generation(self):
        """Verify that a single subscription maps cleanly to multiple real inbounds."""
        mapping_de = SubscriptionConfigMapping.objects.create(
            subscription=self.subscription, inbound=self.german_vless_inbound
        )
        mapping_sg = SubscriptionConfigMapping.objects.create(
            subscription=self.subscription, inbound=self.singapore_trojan_inbound
        )

        self.assertEqual(self.subscription.mappings.count(), 2)
        
       # Dynamically resolve the domain exactly how the model's save() method does it
        from django.conf import settings
        resolved_domain = settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'ledger.local'
        
        # Enforce structural integrity of deterministic non-colliding client emails
        expected_de_email = f"user_{self.user.id}_{self.german_vless_inbound.id}@{resolved_domain}"
        expected_sg_email = f"user_{self.user.id}_{self.singapore_trojan_inbound.id}@{resolved_domain}"
        
        self.assertEqual(mapping_de.xui_client_email, expected_de_email)
        self.assertEqual(mapping_sg.xui_client_email, expected_sg_email)
        self.assertNotEqual(mapping_de.xui_client_email, mapping_sg.xui_client_email)

    def test_unique_together_constraint_on_junction_table(self):
        """Verify a subscription cannot be double-mapped to the exact same inbound."""
        SubscriptionConfigMapping.objects.create(
            subscription=self.subscription, inbound=self.german_vless_inbound
        )
        with self.assertRaises(IntegrityError):
            SubscriptionConfigMapping.objects.create(
                subscription=self.subscription, inbound=self.german_vless_inbound
            )