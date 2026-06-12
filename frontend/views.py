import json
import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction as db_transaction
from django.utils import timezone
from django.db.models import Q

from billing_engine.models import (
    CustomUser, VPNPlan, Transaction, ProxySubscription,
    XuiServer, XuiInbound, PricingTier, SubscriptionConfigMapping
)
from billing_engine.services import process_purchase, InsufficientFundsError
from billing_engine.xui_client import XuiAPIClient, XuiAPIException
from .forms import (
    LoginForm, RegisterForm, WalletTopUpForm, PlanPurchaseForm,
    CustomPurchaseForm, CreateManagedUserForm, VPNPlanForm,
    XuiServerForm, TransactionReviewForm, PricingTierForm
)
from .decorators import require_role

logger = logging.getLogger(__name__)


def _parse_field(value):
    """Return value as dict regardless of whether it came as a dict or JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _role_redirect(user):
    if user.role == CustomUser.Role.MASTER_ADMIN:
        return 'frontend:master_dashboard'
    elif user.role == CustomUser.Role.SUB_ADMIN:
        return 'frontend:subadmin_dashboard'
    return 'frontend:dashboard'


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def index(request):
    if request.user.is_authenticated:
        return redirect(_role_redirect(request.user))
    return redirect('frontend:login')


def login_view(request):
    if request.user.is_authenticated:
        return redirect(_role_redirect(request.user))
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.get_user()
        login(request, user)
        messages.success(request, f'Welcome back, {user.username}!')
        return redirect(_role_redirect(user))
    return render(request, 'frontend/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, 'You have been logged out.')
    return redirect('frontend:login')


def register_view(request):
    if request.user.is_authenticated:
        return redirect(_role_redirect(request.user))
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save(commit=False)
        user.role = CustomUser.Role.END_USER
        user.save()
        login(request, user)
        messages.success(request, 'Account created! Welcome to vShop.')
        return redirect('frontend:dashboard')
    return render(request, 'frontend/register.html', {'form': form})


# ---------------------------------------------------------------------------
# END USER
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    if request.user.role == CustomUser.Role.MASTER_ADMIN:
        return redirect('frontend:master_dashboard')
    if request.user.role == CustomUser.Role.SUB_ADMIN:
        return redirect('frontend:subadmin_dashboard')
    user = request.user
    active_subs = ProxySubscription.objects.filter(user=user, is_active=True).count()
    recent_txns = Transaction.objects.filter(user=user).order_by('-created_at')[:5]
    return render(request, 'frontend/dashboard.html', {
        'active_subs': active_subs,
        'recent_txns': recent_txns,
    })


@login_required
def plans(request):
    plans_qs = VPNPlan.objects.filter(is_visible=True).order_by('total_gb')
    user = request.user
    pricing_target = user.parent_manager if (user.role == CustomUser.Role.END_USER and user.parent_manager) else user
    tier = (
        PricingTier.objects.filter(specific_user=pricing_target).first()
        or PricingTier.objects.filter(target_role=pricing_target.role, specific_user__isnull=True).first()
    )
    return render(request, 'frontend/plans.html', {'plans': plans_qs, 'tier': tier})


@login_required
def buy_plan(request, plan_id):
    plan = get_object_or_404(VPNPlan, id=plan_id, is_visible=True)
    if request.method == 'POST':
        try:
            result = process_purchase(buyer=request.user, plan=plan)
            messages.success(request, f'Plan purchased! Reference: {result["payment_ref_code"]}')
        except InsufficientFundsError:
            messages.error(request, 'Insufficient wallet balance.')
        except ValueError as e:
            messages.error(request, str(e))
        except Exception as e:
            logger.error('Plan purchase error: %s', e)
            messages.error(request, 'An unexpected error occurred.')
        return redirect('frontend:subscriptions')
    return render(request, 'frontend/buy_confirm.html', {'plan': plan})


@login_required
def custom_plan(request):
    form = CustomPurchaseForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            result = process_purchase(
                buyer=request.user,
                custom_gb=form.cleaned_data['custom_gb'],
                custom_days=form.cleaned_data['custom_days'],
            )
            messages.success(request, 'Custom plan purchased!')
            return redirect('frontend:subscriptions')
        except InsufficientFundsError:
            messages.error(request, 'Insufficient wallet balance.')
        except ValueError as e:
            messages.error(request, str(e))
    return render(request, 'frontend/custom_plan.html', {'form': form})


@login_required
def subscriptions(request):
    user = request.user
    active = ProxySubscription.objects.filter(user=user, is_active=True).order_by('-created_at')
    expired = ProxySubscription.objects.filter(user=user, is_active=False).order_by('-created_at')
    return render(request, 'frontend/subscriptions.html', {'active_subs': active, 'expired_subs': expired})


@login_required
def subscription_detail(request, sub_id):
    sub = get_object_or_404(ProxySubscription, id=sub_id, user=request.user)
    usage_pct = 0
    if sub.total_allocated_gb > 0:
        usage_pct = min(int((float(sub.used_gb) / sub.total_allocated_gb) * 100), 100)
    now = timezone.now()
    remaining_days = max((sub.expires_at - now).days, 0) if sub.expires_at > now else 0
    return render(request, 'frontend/subscription_detail.html', {
        'sub': sub, 'usage_pct': usage_pct, 'remaining_days': remaining_days,
    })


@login_required
def wallet(request):
    form = WalletTopUpForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        topup = form.save(commit=False)
        topup.user = request.user
        topup.type = Transaction.TypeChoices.WALLET_TOPUP
        topup.status = Transaction.StatusChoices.PENDING
        topup.payment_ref_code = topup.payment_ref_code.upper()
        topup.save()
        messages.success(request, 'Top-up request submitted!')
        return redirect('frontend:wallet')
    txns = Transaction.objects.filter(user=request.user, type=Transaction.TypeChoices.WALLET_TOPUP).order_by('-created_at')[:10]
    return render(request, 'frontend/wallet.html', {'form': form, 'txns': txns})


@login_required
def transactions(request):
    txns = Transaction.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'frontend/transactions.html', {'txns': txns})


# ---------------------------------------------------------------------------
# SUB ADMIN
# ---------------------------------------------------------------------------

@require_role('SUB_ADMIN', 'MASTER_ADMIN')
def subadmin_dashboard(request):
    if request.user.role == CustomUser.Role.MASTER_ADMIN:
        return redirect('frontend:master_dashboard')
    managed_users = CustomUser.objects.filter(parent_manager=request.user)
    return render(request, 'frontend/subadmin_dashboard.html', {
        'managed_count': managed_users.count(),
        'active_subs': ProxySubscription.objects.filter(user__in=managed_users, is_active=True).count(),
    })


@require_role('SUB_ADMIN', 'MASTER_ADMIN')
def subadmin_users(request):
    if request.user.role == CustomUser.Role.MASTER_ADMIN:
        return redirect('frontend:master_users')
    managed = CustomUser.objects.filter(parent_manager=request.user).order_by('username')
    return render(request, 'frontend/subadmin_users.html', {'managed_users': managed})


@require_role('SUB_ADMIN', 'MASTER_ADMIN')
def subadmin_create_user(request):
    form = CreateManagedUserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        new_user = CustomUser.objects.create_user(
            username=form.cleaned_data['username'],
            email=form.cleaned_data.get('email', ''),
            password=form.cleaned_data['password'],
            role=CustomUser.Role.END_USER,
            parent_manager=request.user,
        )
        messages.success(request, f'User {new_user.username} created.')
        return redirect('frontend:subadmin_users')
    return render(request, 'frontend/subadmin_create_user.html', {'form': form})


# ---------------------------------------------------------------------------
# MASTER ADMIN — helpers
# ---------------------------------------------------------------------------

def _sync_server_inbounds_and_clients(server):
    inbound_count = 0
    client_count = 0
    errors = []

    try:
        result = XuiAPIClient(server).get_inbounds()
    except XuiAPIException as e:
        errors.append(str(e))
        return inbound_count, client_count, errors

    import_user, _ = CustomUser.objects.get_or_create(
        username='__imported__',
        defaults={
            'role': CustomUser.Role.END_USER,
            'is_active': False,
            'email': 'imported@system.local',
        }
    )

    managed_uuids = set(ProxySubscription.objects.values_list('xui_client_uuid', flat=True))

    for ib in result.get('obj', []):
        stream = _parse_field(ib.get('streamSettings', {}))

        inbound_obj, _ = XuiInbound.objects.get_or_create(
            server=server,
            xui_inbound_id=ib['id'],
            defaults={
                'protocol': ib.get('protocol', 'VLESS').upper(),
                'stream_settings': stream,
                'is_available_for_purchase': True,
            }
        )
        inbound_count += 1

        # settings is a dict in v3.3.1, may be a JSON string in older versions
        settings_obj = _parse_field(ib.get('settings', {}))

        for cli in settings_obj.get('clients', []):
            uuid = cli.get('id', '')
            email = cli.get('email', '')
            if not uuid or uuid in managed_uuids:
                continue
            try:
                sub = ProxySubscription.objects.create(
                    user=import_user,
                    xui_client_uuid=uuid,
                    subscription_url='',
                    total_allocated_gb=0,
                    expires_at=timezone.now(),
                    is_active=cli.get('enable', True),
                )
                SubscriptionConfigMapping.objects.get_or_create(
                    subscription=sub,
                    inbound=inbound_obj,
                    defaults={'xui_client_email': email},
                )
                managed_uuids.add(uuid)
                client_count += 1
            except Exception as e:
                errors.append(f'Client {uuid[:8]}: {e}')
                logger.error('Import client error: %s', e)

    return inbound_count, client_count, errors


# ---------------------------------------------------------------------------
# MASTER ADMIN
# ---------------------------------------------------------------------------

@require_role('MASTER_ADMIN')
def master_dashboard(request):
    return render(request, 'frontend/master_dashboard.html', {
        'total_users': CustomUser.objects.exclude(username='__imported__').count(),
        'active_subs': ProxySubscription.objects.filter(is_active=True).count(),
        'pending_txns': Transaction.objects.filter(status=Transaction.StatusChoices.PENDING).count(),
        'total_servers': XuiServer.objects.count(),
        'active_servers': XuiServer.objects.filter(is_active=True).count(),
    })


@require_role('MASTER_ADMIN')
def master_users(request):
    role_filter = request.GET.get('role', '')
    search = request.GET.get('q', '').strip()
    users = CustomUser.objects.exclude(username='__imported__').order_by('username')
    if role_filter:
        users = users.filter(role=role_filter)
    if search:
        users = users.filter(
            Q(username__icontains=search) |
            Q(email__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)
        )
    return render(request, 'frontend/master_users.html', {
        'users': users,
        'role_filter': role_filter,
        'role_choices': CustomUser.Role.choices,
        'search': search,
    })


@require_role('MASTER_ADMIN')
def master_servers(request):
    form = XuiServerForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        server = form.save()
        inbound_count, client_count, errors = _sync_server_inbounds_and_clients(server)
        if errors:
            messages.warning(request, f'Server added but sync had errors: {"; ".join(errors[:3])}')
        else:
            messages.success(
                request,
                f'Server "{server.name}" added. '
                f'Synced {inbound_count} inbound(s), imported {client_count} existing client(s).'
            )
        return redirect('frontend:master_servers')
    servers = XuiServer.objects.prefetch_related('inbounds').order_by('name')
    return render(request, 'frontend/master_servers.html', {'form': form, 'servers': servers})


@require_role('MASTER_ADMIN')
def master_server_toggle(request, server_id):
    server = get_object_or_404(XuiServer, id=server_id)
    if request.method == 'POST':
        server.is_active = not server.is_active
        server.save(update_fields=['is_active'])
        state = 'activated' if server.is_active else 'deactivated'
        messages.success(request, f'Server "{server.name}" {state}.')
    return redirect('frontend:master_servers')


@require_role('MASTER_ADMIN')
def master_server_clients(request, server_id):
    server = get_object_or_404(XuiServer, id=server_id)
    clients = []
    error = None

    managed_uuids = {
        m.subscription.xui_client_uuid: m.subscription
        for m in SubscriptionConfigMapping.objects.select_related(
            'subscription', 'subscription__user', 'inbound'
        ).filter(inbound__server=server)
    }

    try:
        result = XuiAPIClient(server).get_inbounds()
        for inbound in result.get('obj', []):
            # Fix: use _parse_field so both dict and JSON string are handled
            settings_obj = _parse_field(inbound.get('settings', {}))
            for cli in settings_obj.get('clients', []):
                uuid = cli.get('id', '')
                clients.append({
                    'inbound_id': inbound.get('id'),
                    'inbound_tag': inbound.get('tag', '?'),
                    'protocol': inbound.get('protocol', '?').upper(),
                    'uuid': uuid,
                    'email': cli.get('email', ''),
                    'enable': cli.get('enable', True),
                    'total_gb': round((inbound.get('up', 0) + inbound.get('down', 0)) / (1024 ** 3), 2),
                    'expiry': cli.get('expiryTime', 0),
                    'subscription': managed_uuids.get(uuid),
                })
    except XuiAPIException as e:
        error = str(e)

    return render(request, 'frontend/master_server_clients.html', {
        'server': server,
        'clients': clients,
        'error': error,
        'managed_count': sum(1 for c in clients if c['subscription']),
        'untracked_count': sum(1 for c in clients if not c['subscription']),
    })


@require_role('MASTER_ADMIN')
def master_inbound_toggle(request, inbound_id):
    inbound = get_object_or_404(XuiInbound, id=inbound_id)
    if request.method == 'POST':
        inbound.is_available_for_purchase = not inbound.is_available_for_purchase
        inbound.save(update_fields=['is_available_for_purchase'])
        state = 'enabled' if inbound.is_available_for_purchase else 'disabled'
        messages.success(request, f'Inbound {inbound} {state}.')
    return redirect('frontend:master_servers')


@require_role('MASTER_ADMIN')
def master_plans(request):
    form = VPNPlanForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Plan created.')
        return redirect('frontend:master_plans')
    return render(request, 'frontend/master_plans.html', {
        'form': form,
        'plans': VPNPlan.objects.all().order_by('-created_at'),
    })


@require_role('MASTER_ADMIN')
def master_plan_delete(request, plan_id):
    plan = get_object_or_404(VPNPlan, id=plan_id)
    if request.method == 'POST':
        plan.is_visible = False
        plan.save(update_fields=['is_visible'])
        messages.success(request, f'Plan "{plan.name}" hidden.')
    return redirect('frontend:master_plans')


@require_role('MASTER_ADMIN')
def master_transactions(request):
    status_filter = request.GET.get('status', 'PENDING')
    txns = Transaction.objects.all().order_by('-created_at')
    if status_filter:
        txns = txns.filter(status=status_filter)
    return render(request, 'frontend/master_transactions.html', {
        'txns': txns,
        'status_filter': status_filter,
        'status_choices': Transaction.StatusChoices.choices,
    })


@require_role('MASTER_ADMIN')
def master_transaction_review(request, tx_id):
    tx = get_object_or_404(Transaction, id=tx_id, status=Transaction.StatusChoices.PENDING)
    form = TransactionReviewForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        new_status = form.cleaned_data['status']
        with db_transaction.atomic():
            if new_status == 'APPROVED' and tx.type == Transaction.TypeChoices.WALLET_TOPUP:
                locked_user = CustomUser.objects.select_for_update().get(pk=tx.user.pk)
                locked_user.wallet_balance += tx.amount
                locked_user.save(update_fields=['wallet_balance'])
            tx.status = new_status
            tx.rejection_reason = form.cleaned_data.get('rejection_reason', '') if new_status == 'REJECTED' else ''
            tx.reviewed_by = request.user
            tx.save()
        messages.success(request, f'Transaction {tx.payment_ref_code} {new_status.lower()}d.')
        return redirect('frontend:master_transactions')
    return render(request, 'frontend/master_transaction_review.html', {'tx': tx, 'form': form})


@require_role('MASTER_ADMIN')
def master_pricing(request):
    form = PricingTierForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Pricing tier created.')
        return redirect('frontend:master_pricing')
    return render(request, 'frontend/master_pricing.html', {
        'form': form,
        'tiers': PricingTier.objects.select_related('specific_user').all(),
    })


@require_role('MASTER_ADMIN')
def master_pricing_delete(request, tier_id):
    tier = get_object_or_404(PricingTier, id=tier_id)
    if request.method == 'POST':
        tier.delete()
        messages.success(request, 'Pricing tier deleted.')
    return redirect('frontend:master_pricing')


@require_role('MASTER_ADMIN')
def master_subscriptions(request):
    subs = ProxySubscription.objects.select_related('user').order_by('-created_at')
    status_filter = request.GET.get('status', '')
    if status_filter == 'active':
        subs = subs.filter(is_active=True)
    elif status_filter == 'expired':
        subs = subs.filter(is_active=False)
    return render(request, 'frontend/master_subscriptions.html', {
        'subs': subs, 'status_filter': status_filter,
    })


@require_role('MASTER_ADMIN')
def master_deprovision(request, sub_id):
    sub = get_object_or_404(ProxySubscription, id=sub_id)
    if request.method == 'POST':
        sub.is_active = False
        sub.save(update_fields=['is_active', 'updated_at'])
        messages.warning(request, 'Subscription deprovisioned.')
    return redirect('frontend:master_subscriptions')
