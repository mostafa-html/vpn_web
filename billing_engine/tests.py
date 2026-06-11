from django.test import TestCase
from django.db.utils import IntegrityError
from django.contrib.auth import get_user_model
from decimal import Decimal
import uuid

from billing_engine.models import XuiServer, XuiInbound, PricingTier

User = get_user_model()


class CustomUserTests(TestCase):
    """
    Validates user initialization, role hierarchies, and financial decimal accuracy.
    """

    def setUp(self):
        # Establish the multi-tenant chain
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
        """Verify that user IDs are generated as valid UUIDs instead of sequential integers."""
        user = User.objects.create_user(
            username="end_user_test",
            email="client@proxy.net",
            password="securepassword123"
        )
        self.assertIsInstance(user.id, uuid.UUID)
        self.assertNotEqual(str(user.id), "1")

    def test_default_user_role(self):
        """Ensure that unassigned roles default strictly to END_USER."""
        casual_user = User.objects.create_user(
            username="casual_user",
            password="securepassword123"
        )
        self.assertEqual(casual_user.role, User.Role.END_USER)

    def test_multi_tenant_hierarchy(self):
        """Validate the self-referential parent-manager multi-tenancy isolation line."""
        end_user = User.objects.create_user(
            username="end_consumer",
            password="securepassword123",
            role=User.Role.END_USER,
            parent_manager=self.sub_admin
        )
        
        # Verify downward lookup chains
        self.assertEqual(end_user.parent_manager, self.sub_admin)
        self.assertEqual(self.sub_admin.parent_manager, self.master_admin)
        
        # Verify upward reverse relationships (related_name='managed_users')
        self.assertIn(end_user, self.sub_admin.managed_users.all())
        self.assertIn(self.sub_admin, self.master_admin.managed_users.all())

    def test_wallet_balance_decimal_precision(self):
        """Ensure monetary additions preserve exact decimal math and avoid float errors."""
        user = User.objects.create_user(username="wallet_holder", password="password")
        user.wallet_balance = Decimal("10.00")
        user.save()

        # Simulate precise increments (e.g., fractional service fees)
        user.wallet_balance += Decimal("0.05")
        user.save()

        refetched_user = User.objects.get(id=user.id)
        self.assertEqual(refetched_user.wallet_balance, Decimal("10.05"))


class XuiServerTests(TestCase):
    """
    Validates XuiServer registration metrics, UUID fields, and networking formats.
    """

    def test_server_provisioning_and_networking(self):
        """Verify server properties, UUID fields, and IP structures function correctly."""
        server = XuiServer.objects.create(
            name="Frankfurt_Edge_01",
            ip_address="192.168.1.100",
            api_port=8080,
            admin_username="xui_root",
            admin_password="unencrypted_temporary_password",
            max_client_capacity=500
        )
        self.assertIsInstance(server.id, uuid.UUID)
        self.assertTrue(server.is_active)


class XuiInboundTests(TestCase):
    """
    Tests proxy configurations, protocol assignments, and structural uniqueness rules.
    """

    def setUp(self):
        self.server = XuiServer.objects.create(
            name="Tokyo_Edge",
            ip_address="2001:db8::1",  # IPv6 test compliance
            api_port=8081,
            admin_username="tokyo_admin",
            admin_password="password",
            max_client_capacity=200
        )

    def test_json_stream_settings_storage(self):
        """Validate that fluid JSON fields successfully map data payloads."""
        settings_payload = {
            "network": "grpc",
            "security": "reality",
            "realitySettings": {"show": False, "dest": "yahoo.com:443"}
        }
        inbound = XuiInbound.objects.create(
            server=self.server,
            xui_inbound_id=1,
            protocol=XuiInbound.Protocol.VLESS,
            stream_settings=settings_payload
        )
        
        refetched_inbound = XuiInbound.objects.get(id=inbound.id)
        self.assertEqual(refetched_inbound.stream_settings["network"], "grpc")
        self.assertFalse(refetched_inbound.stream_settings["realitySettings"]["show"])

    def test_composite_unique_inbound_per_server_constraint(self):
        """Enforce that the same 3x-ui internal ID cannot be cloned on the same node."""
        XuiInbound.objects.create(
            server=self.server,
            xui_inbound_id=42,
            protocol=XuiInbound.Protocol.TROJAN
        )

        # Attempting to assign the exact same panel ID (42) to the same server must trigger an IntegrityError
        with self.assertRaises(IntegrityError):
            XuiInbound.objects.create(
                server=self.server,
                xui_inbound_id=42,
                protocol=XuiInbound.Protocol.VMESS
            )


class PricingTierTests(TestCase):
    """
    Validates tariff parsing rules across tiered multi-tenant structures.
    """

    def setUp(self):
        self.client_user = User.objects.create_user(
            username="vip_client", 
            password="password", 
            role=User.Role.END_USER
        )

    def test_global_vs_override_pricing_evaluation(self):
        """Verify granular rate properties and pricing tier definitions look up cleanly."""
        # Define a global pricing baseline for End Users
        global_tier = PricingTier.objects.create(
            target_role=PricingTier.TargetRole.END_USER,
            price_per_gb=Decimal("0.1500"),
            price_per_day=Decimal("0.0500")
        )

        # Define an isolated custom contract override for a specific user
        custom_tier = PricingTier.objects.create(
            target_role=PricingTier.TargetRole.END_USER,
            specific_user=self.client_user,
            price_per_gb=Decimal("0.1000"),  # VIP discounted rate
            price_per_day=Decimal("0.0300")
        )

        # Assert correct field storage down to 4 decimal points
        self.assertNil = self.assertIsNone(global_tier.specific_user)
        self.assertEqual(custom_tier.specific_user, self.client_user)
        self.assertEqual(custom_tier.price_per_gb, Decimal("0.1000"))