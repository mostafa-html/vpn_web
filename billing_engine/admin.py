# billing_engine/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, XuiServer, XuiInbound, PricingTier

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'role', 'parent_manager', 'wallet_balance', 'is_staff')
    list_filter = ('role', 'is_staff', 'is_superuser')
    fieldsets = UserAdmin.fieldsets + (
        ('Multi-Tenant Infrastructure', {'fields': ('role', 'parent_manager', 'wallet_balance')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Multi-Tenant Infrastructure', {'fields': ('role', 'parent_manager', 'wallet_balance')}),
    )

@admin.register(XuiServer)
class XuiServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip_address', 'api_port', 'max_client_capacity', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'ip_address')

@admin.register(XuiInbound)
class XuiInboundAdmin(admin.ModelAdmin):
    list_display = ('server', 'xui_inbound_id', 'protocol', 'is_available_for_purchase')
    list_filter = ('protocol', 'is_available_for_purchase', 'server')

@admin.register(PricingTier)
class PricingTierAdmin(admin.ModelAdmin):
    list_display = ('target_role', 'specific_user', 'price_per_gb', 'price_per_day')
    list_filter = ('target_role', 'specific_user')