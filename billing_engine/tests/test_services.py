from decimal import Decimal
import uuid
from django.test import TestCase
from django.contrib.auth import get_user_model
from billing_engine.models import CustomUser, VPNPlan, PricingTier, Transaction
from billing_engine.services import process_purchase, InsufficientFundsError

User = get_user_model()

class BillingServiceLayerTests(TestCase):
    
    def setUp(self):
        # 1. Establish Master Admin Root Identity
        self.master_admin = User.objects.create_user(
            username="master_root",
            email="master@platform.net",
            password="test_password_123",
            role="MASTER_ADMIN"
        )
        
        # 2. Establish Sub Admin Reseller Instance
        self.sub_admin = User.objects.create_user(
            username="sub_admin_reseller",
            email="reseller@platform.net",
            password="test_password_123",
            role="SUB_ADMIN",
            parent_manager=self.master_admin,
            wallet_balance=Decimal("500.00")
        )
        
        # 3. Establish Managed End User
        self.managed_user = User.objects.create_user(
            username="managed_client",
            email="client1@managed.net",
            password="test_password_123",
            role="END_USER",
            parent_manager=self.sub_admin,
            wallet_balance=Decimal("0.00")  # Balance is zero because billing routes to Sub-Admin
        )
        
        # 4. Establish Independent Isolated End User
        self.isolated_user = User.objects.create_user(
            username="isolated_client",
            email="client2@independent.net",
            password="test_password_123",
            role="END_USER",
            parent_manager=None,
            wallet_balance=Decimal("100.00")
        )

        # 5. Build Global Structural Pricing Tiers
        # Retail Global Tier: $0.20 per GB, $0.10 per Day
        self.retail_tier = PricingTier.objects.create(
            target_role="END_USER",
            specific_user=None,
            price_per_gb=Decimal("0.20"),
            price_per_day=Decimal("0.10")
        )
        
        # Wholesale Global Tier: $0.05 per GB, $0.02 per Day
        self.wholesale_tier = PricingTier.objects.create(
            target_role="SUB_ADMIN",
            specific_user=None,
            price_per_gb=Decimal("0.05"),
            price_per_day=Decimal("0.02")
        )
        
        # 6. Establish Test VPN Plans
        self.standard_plan = VPNPlan.objects.create(
            name="Standard 50GB Starter Pack",
            total_gb=50,
            duration_days=30,
            is_visible=True
        )

    def test_successful_purchase_by_isolated_user_debits_correctly(self):
        """Verifies an isolated user drawing from standard pricing parameters debits their own balance."""
        # Expected calculation: (50 GB * $0.20) + (30 Days * $0.10) = $10.00 + $3.00 = $13.00
        expected_cost = Decimal("13.00")
        initial_balance = self.isolated_user.wallet_balance

        payload = process_purchase(buyer=self.isolated_user, plan=self.standard_plan)
        
        # Re-fetch isolated user fresh state from DB
        self.isolated_user.refresh_from_db()
        
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["debited_account_id"], str(self.isolated_user.id))
        self.assertEqual(payload["amount_charged"], expected_cost)
        self.assertFalse(payload["is_wholesale"])
        self.assertEqual(self.isolated_user.wallet_balance, initial_balance - expected_cost)
        
        # Assert database transaction ledger verification
        self.assertTrue(Transaction.objects.filter(payment_ref_code=payload["payment_ref_code"]).exists())

    def test_successful_purchase_by_managed_user_reroutes_to_sub_admin_wholesale(self):
        """Verifies an end user with a parent manager routes billing to the sub-admin account."""
        # Expected wholesale pricing metrics calculation: 
        # (50 GB * $0.05) + (30 Days * $0.02) = $2.50 + $0.60 = $3.10
        expected_wholesale_cost = Decimal("3.10")
        sub_admin_initial_balance = self.sub_admin.wallet_balance
        managed_user_initial_balance = self.managed_user.wallet_balance

        payload = process_purchase(buyer=self.managed_user, plan=self.standard_plan)
        
        # Pull fresh database rows
        self.sub_admin.refresh_from_db()
        self.managed_user.refresh_from_db()

        self.assertEqual(payload["status"], "SUCCESS")
        self.assertTrue(payload["is_wholesale"])
        self.assertEqual(payload["debited_account_id"], str(self.sub_admin.id))
        self.assertEqual(payload["amount_charged"], expected_wholesale_cost)
        
        # Ensure the sub-admin was debited wholesale pricing metrics, while end-user balance remained unaffected
        self.assertEqual(self.sub_admin.wallet_balance, sub_admin_initial_balance - expected_wholesale_cost)
        self.assertEqual(self.managed_user.wallet_balance, managed_user_initial_balance)

    def test_insufficient_funds_raises_exception_and_forces_rollback(self):
        """Verifies that low credit conditions raise InsufficientFundsError and abort balance mutations."""
        # Setup an user with very low credit balance
        poor_user = User.objects.create_user(
            username="poor_client",
            email="poor@client.net",
            password="test_password_123",
            role="END_USER",
            parent_manager=None,
            wallet_balance=Decimal("2.00")
        )
        
        # Cost calculation ($13.00) exceeds wallet balance ($2.00)
        with self.assertRaises(InsufficientFundsError):
            process_purchase(buyer=poor_user, plan=self.standard_plan)

        # Confirm the database state rolled back and the balance was not mutated
        poor_user.refresh_from_db()
        self.assertEqual(poor_user.wallet_balance, Decimal("2.00"))
        
        # Ensure no ledger entries were permanently recorded to disk
        self.assertEqual(Transaction.objects.filter(user=poor_user).count(), 0)

    def test_custom_fluid_parameters_pricing_calculation(self):
        """Verifies that fluid custom measurements are correctly processed when no fixed VPNPlan is passed."""
        # Expected Calculation: (100 GB * $0.20) + (10 Days * $0.10) = $20.00 + $1.00 = $21.00
        expected_cost = Decimal("21.00")
        
        payload = process_purchase(
            buyer=self.isolated_user,
            plan=None,
            custom_gb=100,
            custom_days=10
        )
        
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["amount_charged"], expected_cost)