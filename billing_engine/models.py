# billing_engine/models.py
import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

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
    ip_address = models.GenericIPAddressField(protocol='both')
    api_port = models.PositiveIntegerField()
    admin_username = models.CharField(max_length=150)
    admin_password = models.CharField(max_length=255, help_text="MUST be encrypted at rest later.")
    max_client_capacity = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "XUI Server"
        verbose_name_plural = "XUI Servers"

    def __str__(self):
        return f"{self.name} ({self.ip_address})"

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