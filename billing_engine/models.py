import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        MASTER_ADMIN = 'MASTER_ADMIN', 'Master Admin'
        SUB_ADMIN = 'SUB_ADMIN', 'Sub Admin'
        END_USER = 'END_USER', 'End User'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.END_USER)
    parent_manager = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='managed_users'
    )
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class XuiServer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    hostname = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Domain name (e.g. gg.mx11.ir). Used instead of IP when set.'
    )
    ip_address = models.GenericIPAddressField(protocol='both')
    api_port = models.PositiveIntegerField()
    base_path = models.CharField(
        max_length=255, blank=True, default='/',
        help_text='3x-ui secret base path, e.g. /4bfAPdC269HYSj1c24/ (include slashes)'
    )
    # API token from Panel Settings -> Security -> API Token (3x-ui v3+)
    api_token = models.CharField(
        max_length=512, blank=True, default='',
        help_text='Bearer token from 3x-ui Panel Settings -> Security -> API Token'
    )
    # Kept for reference / legacy but no longer used for auth
    admin_username = models.CharField(max_length=150, blank=True, default='')
    admin_password = models.CharField(max_length=255, blank=True, default='')
    max_client_capacity = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    use_ssl = models.BooleanField(
        default=False,
        help_text='Enable if your 3x-ui panel runs on HTTPS.'
    )

    class Meta:
        verbose_name = "XUI Server"
        verbose_name_plural = "XUI Servers"

    def get_host(self):
        return self.hostname.strip() if self.hostname.strip() else self.ip_address

    def get_base_path(self):
        path = self.base_path.strip() or '/'
        if not path.startswith('/'):
            path = '/' + path
        if not path.endswith('/'):
            path = path + '/'
        return path

    def __str__(self):
        return f"{self.name} ({self.get_host()})"


class XuiInbound(models.Model):
    class Protocol(models.TextChoices):
        VLESS = 'VLESS', 'VLESS'
        VMESS = 'VMESS', 'VMESS'
        TROJAN = 'TROJAN', 'TROJAN'
        SHADOWSOCKS = 'SHADOWSOCKS', 'SHADOWSOCKS'

    server = models.ForeignKey(XuiServer, on_delete=models.CASCADE, related_name='inbounds')
    xui_inbound_id = models.PositiveIntegerField()
    protocol = models.CharField(max_length=20, choices=Protocol.choices)
    stream_settings = models.JSONField(default=dict, blank=True)
    is_available_for_purchase = models.BooleanField(default=True)

    class Meta:
        verbose_name = "XUI Inbound"
        verbose_name_plural = "XUI Inbounds"
        constraints = [
            models.UniqueConstraint(fields=['server', 'xui_inbound_id'], name='unique_inbound_per_server')
        ]

    def __str__(self):
        return f"{self.protocol} (ID: {self.xui_inbound_id}) on {self.server.name}"


class PricingTier(models.Model):
    class TargetRole(models.TextChoices):
        SUB_ADMIN = 'SUB_ADMIN', 'Sub Admin'
        END_USER = 'END_USER', 'End User'

    target_role = models.CharField(max_length=20, choices=TargetRole.choices)
    specific_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name='custom_rates'
    )
    price_per_gb = models.DecimalField(max_digits=10, decimal_places=4)
    price_per_day = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        verbose_name = "Pricing Tier"
        verbose_name_plural = "Pricing Tiers"

    def __str__(self):
        if self.specific_user:
            return f"Custom Rate for {self.specific_user.username}"
        return f"Global Rate for {self.get_target_role_display()}"


class VPNPlan(models.Model):
    name = models.CharField(max_length=255)
    total_gb = models.PositiveIntegerField()
    duration_days = models.PositiveIntegerField()
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "VPN Plan"
        verbose_name_plural = "VPN Plans"

    def __str__(self):
        return f"{self.name} - {self.total_gb}GB ({self.duration_days} Days)"


class Transaction(models.Model):
    class TypeChoices(models.TextChoices):
        WALLET_TOPUP = "WALLET_TOPUP", "Wallet Top-up"
        PLAN_PURCHASE = "PLAN_PURCHASE", "Plan Purchase"

    class StatusChoices(models.TextChoices):
        PENDING = "PENDING", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions")
    type = models.CharField(max_length=20, choices=TypeChoices.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    screenshot = models.ImageField(upload_to="protected_media/transactions/")
    payment_ref_code = models.CharField(
        max_length=100, unique=True, db_index=True,
        validators=[RegexValidator(regex=r"^[A-Z0-9]+$", message="Uppercase alphanumeric only.")]
    )
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING)
    rejection_reason = models.TextField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reviewed_transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.payment_ref_code:
            self.payment_ref_code = self.payment_ref_code.upper()
        if self.status == self.StatusChoices.REJECTED and not self.rejection_reason:
            raise ValidationError({"rejection_reason": "A reason must be provided if rejected."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Tx {self.payment_ref_code} - {self.user.username} ({self.amount})"


class ProxySubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions")
    xui_client_uuid = models.CharField(max_length=36)
    subscription_url = models.URLField()
    total_allocated_gb = models.PositiveIntegerField()
    used_gb = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    last_known_up_bytes = models.BigIntegerField(default=0)
    last_known_down_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Sub {self.xui_client_uuid[:8]} - User: {self.user.username}"


class SubscriptionConfigMapping(models.Model):
    subscription = models.ForeignKey(ProxySubscription, on_delete=models.CASCADE, related_name="mappings")
    inbound = models.ForeignKey(XuiInbound, on_delete=models.CASCADE, related_name="mapped_subscriptions")
    xui_client_email = models.CharField(max_length=255)

    class Meta:
        unique_together = ("subscription", "inbound")

    def save(self, *args, **kwargs):
        if not self.xui_client_email:
            self.xui_client_email = f"user_{self.subscription.user.id}_{self.inbound.id}@{settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'ledger.local'}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Mapping: Sub {self.subscription.id} <-> Inbound {self.inbound.id}"
