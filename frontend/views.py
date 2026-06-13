import json
import logging
from datetime import datetime, timezone as dt_timezone, timedelta
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
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

BYTES_PER_GB = 1024 ** 3


def _parse_field(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _fmt_bytes(b):
    if b is None:
        return '0 B'
    b = int(b)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(b) < 1024.0:
            return f"{b:.2f} {unit}" if unit not in ('B', 'KB') else f"{b} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def _expiry_display(expiry_ms):
    if not expiry_ms:
        return None, None
    try:
        dt = datetime.fromtimestamp(expiry_ms / 1000, tz=dt_timezone.utc)
        now = datetime.now(tz=dt_timezone.utc)
        days = (dt - now).days
        return dt.strftime('%Y-%m-%d'), days
    except Exception:
        return None, None


def _role_redirect(user):
    if user.role == CustomUser.Role.MASTER_ADMIN:
        return 'frontend:master_dashboard'
    elif user.role == CustomUser.Role.SUB_ADMIN:
        return 'frontend:subadmin_dashboard'
    return 'frontend:dashboard'


def _get_or_create_user_for_email(email):
    if not email:
        return None, False
    email = email.strip().lower()
    user, created = CustomUser.objects.get_or_create(
        username=email,
        defaults={
            'email': email,
            'role': CustomUser.Role.END_USER,
            'is_active': True,
        }
    )
    if created:
        user.set_password(email)
        user.save(update_fields=['password'])
    return user, created


def _get_pricing_for_user(user):
    pricing_target = (
        user.parent_manager
        if (user.role == CustomUser.Role.END_USER and user.parent_manager)
        else user
    )
    tier = (
        PricingTier.objects.filter(specific_user=pricing_target).first()
        or PricingTier.objects.filter(
            target_role=pricing_target.role, specific_user__isnull=True
        ).first()
    )
    if tier:
        return tier.price_per_gb, tier.price_per_day
    return Decimal('0'), Decimal('0')


def _build_subscription_url(server, client_uuid):
    """Build the 3x-ui subscription URL for a client UUID."""
    protocol = 'https' if server.use_ssl else 'http'
    host = server.get_host()
    base_path = server.get_base_path()  # always has trailing slash
    return f"{protocol}://{host}:{server.api_port}{base_path}sub/{client_uuid}"


def _get_live_client_data_for_uuid(server, target_uuid):
    """Fetch live data for a single client UUID from 3x-ui."""
    try:
        result = XuiAPIClient(server).get_inbounds()
    except XuiAPIException as e:
        logger.error('XUI API error: %s', e)
        return None

    aggregated = {
        'used_bytes': 0,
        'total_bytes': 0,
        'expiry_ms': 0,
        'enable': True,
        'email': '',
    }
    found = False

    for inbound in result.get('obj', []):
        client_stats = inbound.get('clientStats') or []
        stats_by_email = {cs.get('email', ''): cs for cs in client_stats}
        settings_obj = _parse_field(inbound.get('settings', {}))
        for cli in settings_obj.get('clients', []):
            if cli.get('id', '') != target_uuid:
                continue
            found = True
            email = cli.get('email', '')
            stats = stats_by_email.get(email, {})
            up = int(stats.get('up', 0))
            down = int(stats.get('down', 0))
            aggregated['used_bytes'] += up + down
            aggregated['email'] = email
            aggregated['enable'] = cli.get('enable', True)
            total_bytes = int(cli.get('totalGB', 0))
            expiry_ms = int(cli.get('expiryTime', 0))
            if total_bytes > aggregated['total_bytes']:
                aggregated['total_bytes'] = total_bytes
            if expiry_ms and (not aggregated['expiry_ms'] or expiry_ms < aggregated['expiry_ms']):
                aggregated['expiry_ms'] = expiry_ms

    return aggregated if found else None


def _push_client_to_xui(sub, new_total_gb_bytes: int = None, new_expiry_ms: int = None):
    """
    Push updated traffic/expiry values for sub's client to every inbound it is
    mapped to on 3x-ui.  Called after DB changes are committed.

    Parameters (both optional — pass only what changed):
        new_total_gb_bytes : new total traffic cap in BYTES  (0 = unlimited)
        new_expiry_ms      : new expiry as Unix milliseconds (0 = never)

    We first fetch the current live values and merge, so we never accidentally
    zero-out a field we didn't intend to change.
    """
    mappings = SubscriptionConfigMapping.objects.select_related('inbound__server').filter(subscription=sub)
    if not mappings.exists():
        logger.warning('_push_client_to_xui: no mappings for sub %s', sub.pk)
        return

    for mapping in mappings:
        server = mapping.inbound.server
        inbound_id = mapping.inbound.xui_inbound_id
        client_email = mapping.xui_client_email
        client_uuid = sub.xui_client_uuid

        # Fetch current live state so we only overwrite what we intend to
        try:
            inbounds_resp = XuiAPIClient(server).get_inbounds()
        except XuiAPIException as e:
            logger.error('Cannot fetch inbounds for server %s: %s', server.name, e)
            continue

        current_total = 0
        current_expiry = 0
        current_enable = True
        for ib in inbounds_resp.get('obj', []):
            if ib.get('id') != inbound_id:
                continue
            for cli in _parse_field(ib.get('settings', {})).get('clients', []):
                if cli.get('id') == client_uuid:
                    current_total = int(cli.get('totalGB', 0))
                    current_expiry = int(cli.get('expiryTime', 0))
                    current_enable = cli.get('enable', True)
                    break

        push_total = new_total_gb_bytes if new_total_gb_bytes is not None else current_total
        push_expiry = new_expiry_ms if new_expiry_ms is not None else current_expiry

        try:
            XuiAPIClient(server).update_client(
                inbound_id=inbound_id,
                client_uuid=client_uuid,
                email=client_email,
                total_gb=push_total,
                expiry_time_ms=push_expiry,
                enable=current_enable,
            )
            logger.info(
                'Pushed to 3x-ui: sub=%s inbound=%s totalGB=%s expiry=%s',
                sub.pk, inbound_id, push_total, push_expiry,
            )
        except XuiAPIException as e:
            logger.error(
                'Failed to push update to 3x-ui for sub %s inbound %s: %s',
                sub.pk, inbound_id, e,
            )


def _create_internal_transaction(user, txn_type, amount, ref_code):
    """Create a system-generated Transaction (no screenshot, auto ref code)."""
    txn = Transaction(
        user=user,
        type=txn_type,
        amount=amount.quantize(Decimal('0.01')),
        payment_ref_code=ref_code,
        status=Transaction.StatusChoices.APPROVED,
        screenshot='',
    )
    txn.save()
    return txn


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
            process_purchase(
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


def _enrich_sub_with_live_data(sub):
    """Attach live 3x-ui data to a ProxySubscription as extra attributes.
    Also backfills DB fields if still at import defaults.
    """
    mapping = SubscriptionConfigMapping.objects.select_related(
        'inbound__server'
    ).filter(subscription=sub).first()

    sub.live_used_gb = float(sub.used_gb)
    sub.live_total_gb = sub.total_allocated_gb
    sub.live_expiry_str = sub.expires_at.strftime('%Y-%m-%d') if sub.expires_at else None
    now = timezone.now()
    sub.live_remaining_days = max((sub.expires_at - now).days, 0) if sub.expires_at and sub.expires_at > now else 0
    sub.live_error = None

    if not mapping:
        return sub

    server = mapping.inbound.server

    if not sub.subscription_url:
        url = _build_subscription_url(server, sub.xui_client_uuid)
        sub.subscription_url = url
        ProxySubscription.objects.filter(pk=sub.pk).update(subscription_url=url)

    live = _get_live_client_data_for_uuid(server, sub.xui_client_uuid)
    if live:
        used_gb = live['used_bytes'] / BYTES_PER_GB
        total_gb = live['total_bytes'] / BYTES_PER_GB if live['total_bytes'] else 0
        sub.live_used_gb = round(used_gb, 3)
        sub.live_total_gb = round(total_gb, 2)

        update_fields = []
        if sub.total_allocated_gb == 0 and total_gb > 0:
            sub.total_allocated_gb = int(total_gb) or 1
            update_fields.append('total_allocated_gb')

        if live['expiry_ms']:
            expiry_dt = datetime.fromtimestamp(live['expiry_ms'] / 1000, tz=dt_timezone.utc)
            expiry_dt_aware = timezone.make_aware(
                expiry_dt.replace(tzinfo=None), timezone.get_current_timezone()
            ) if timezone.is_naive(expiry_dt) else expiry_dt
            sub.live_expiry_str = expiry_dt.strftime('%Y-%m-%d')
            remaining = (expiry_dt - datetime.now(tz=dt_timezone.utc)).days
            sub.live_remaining_days = max(remaining, 0)
            if sub.expires_at <= timezone.now() + timedelta(minutes=1):
                sub.expires_at = expiry_dt_aware
                update_fields.append('expires_at')
        else:
            sub.live_expiry_str = 'No expiry'
            sub.live_remaining_days = 999

        if update_fields:
            try:
                ProxySubscription.objects.filter(pk=sub.pk).update(
                    **{f: getattr(sub, f) for f in update_fields}
                )
            except Exception as e:
                logger.warning('Could not backfill sub %s: %s', sub.pk, e)

    return sub


@login_required
def subscriptions(request):
    user = request.user
    active_qs = list(ProxySubscription.objects.filter(user=user, is_active=True).order_by('-created_at'))
    expired_qs = list(ProxySubscription.objects.filter(user=user, is_active=False).order_by('-created_at'))

    for sub in active_qs + expired_qs:
        _enrich_sub_with_live_data(sub)

    price_per_gb, price_per_day = _get_pricing_for_user(user)
    return render(request, 'frontend/subscriptions.html', {
        'active_subs': active_qs,
        'expired_subs': expired_qs,
        'price_per_gb': price_per_gb,
        'price_per_day': price_per_day,
    })


@login_required
def subscription_detail(request, sub_id):
    sub = get_object_or_404(ProxySubscription, id=sub_id, user=request.user)
    _enrich_sub_with_live_data(sub)

    usage_pct = 0
    if sub.live_total_gb > 0:
        usage_pct = min(int(sub.live_used_gb / sub.live_total_gb * 100), 100)

    return render(request, 'frontend/subscription_detail.html', {
        'sub': sub,
        'usage_pct': usage_pct,
        'remaining_days': sub.live_remaining_days,
        'expiry_str': sub.live_expiry_str,
        'used_gb': sub.live_used_gb,
        'total_gb': sub.live_total_gb,
    })


@login_required
def subscription_topup(request, sub_id):
    if request.method != 'POST':
        return redirect('frontend:subscriptions')

    sub = get_object_or_404(ProxySubscription, id=sub_id, user=request.user)
    action = request.POST.get('action', '')
    price_per_gb, price_per_day = _get_pricing_for_user(request.user)
    ts = int(timezone.now().timestamp())

    xui_push_kwargs = {}  # filled per action, pushed after the atomic block

    with db_transaction.atomic():
        user = CustomUser.objects.select_for_update().get(pk=request.user.pk)

        if action == 'traffic':
            try:
                extra_gb = int(request.POST.get('extra_gb', 0))
            except ValueError:
                extra_gb = 0
            if extra_gb <= 0:
                messages.error(request, 'Enter a valid number of GB.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            if price_per_gb <= 0:
                messages.error(request, 'No pricing configured. Contact support.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            cost = (Decimal(str(extra_gb)) * price_per_gb).quantize(Decimal('0.01'))
            if user.wallet_balance < cost:
                messages.error(request, f'Insufficient balance. Need {cost} IRR, have {user.wallet_balance} IRR.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            user.wallet_balance -= cost
            user.save(update_fields=['wallet_balance'])
            sub.total_allocated_gb += extra_gb
            sub.is_active = True
            sub.save(update_fields=['total_allocated_gb', 'is_active', 'updated_at'])
            _create_internal_transaction(
                user=user,
                txn_type=Transaction.TypeChoices.PLAN_PURCHASE,
                amount=cost,
                ref_code=f'TOPUP{sub.id}T{ts}',
            )
            # Convert new total GB -> bytes for 3x-ui
            xui_push_kwargs['new_total_gb_bytes'] = sub.total_allocated_gb * BYTES_PER_GB
            messages.success(request, f'{extra_gb} GB added. Cost: {cost} IRR.')

        elif action == 'renew':
            try:
                extra_days = int(request.POST.get('extra_days', 0))
            except ValueError:
                extra_days = 0
            if extra_days <= 0:
                messages.error(request, 'Enter a valid number of days.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            if price_per_day <= 0:
                messages.error(request, 'No pricing configured. Contact support.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            cost = (Decimal(str(extra_days)) * price_per_day).quantize(Decimal('0.01'))
            if user.wallet_balance < cost:
                messages.error(request, f'Insufficient balance. Need {cost} IRR, have {user.wallet_balance} IRR.')
                return redirect('frontend:subscription_detail', sub_id=sub_id)
            user.wallet_balance -= cost
            user.save(update_fields=['wallet_balance'])
            base = max(sub.expires_at, timezone.now())
            sub.expires_at = base + timedelta(days=extra_days)
            sub.is_active = True
            sub.save(update_fields=['expires_at', 'is_active', 'updated_at'])
            _create_internal_transaction(
                user=user,
                txn_type=Transaction.TypeChoices.PLAN_PURCHASE,
                amount=cost,
                ref_code=f'RENEW{sub.id}T{ts}',
            )
            # Convert new expiry datetime -> Unix milliseconds for 3x-ui
            xui_push_kwargs['new_expiry_ms'] = int(sub.expires_at.timestamp() * 1000)
            messages.success(request, f'{extra_days} days added. Cost: {cost} IRR.')

        else:
            messages.error(request, 'Invalid action.')

    # Push to 3x-ui OUTSIDE the atomic block so a panel API failure
    # doesn't roll back the billing transaction.
    if xui_push_kwargs:
        try:
            _push_client_to_xui(sub, **xui_push_kwargs)
        except Exception as e:
            logger.error('3x-ui push failed for sub %s: %s', sub.pk, e)
            messages.warning(
                request,
                'Balance deducted and DB updated, but could not reach the VPN panel. '
                'Changes will appear on next sync.'
            )

    return redirect('frontend:subscription_detail', sub_id=sub_id)


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


@login_required
def change_password(request):
    if request.method == 'POST':
        current = request.POST.get('current_password', '')
        new1 = request.POST.get('new_password1', '')
        new2 = request.POST.get('new_password2', '')
        if not request.user.check_password(current):
            messages.error(request, 'Current password is incorrect.')
        elif len(new1) < 6:
            messages.error(request, 'New password must be at least 6 characters.')
        elif new1 != new2:
            messages.error(request, 'New passwords do not match.')
        else:
            request.user.set_password(new1)
            request.user.save()
            update_session_auth_hash(request, request.user)
            messages.success(request, 'Password changed successfully.')
            return redirect(_role_redirect(request.user))
    return render(request, 'frontend/change_password.html')


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
# MASTER ADMIN - helpers
# ---------------------------------------------------------------------------

def _build_live_client_map(server):
    result = {}
    try:
        api_result = XuiAPIClient(server).get_inbounds()
    except XuiAPIException as e:
        logger.error('XUI API error for server %s: %s', server.name, e)
        return result

    for inbound in api_result.get('obj', []):
        tag = inbound.get('tag') or inbound.get('remark') or f"IB-{inbound.get('id')}"
        protocol = inbound.get('protocol', '').upper()
        label = f"{protocol}/{tag}"

        client_stats = inbound.get('clientStats') or []
        stats_by_email = {}
        for cs in client_stats:
            stats_by_email[cs.get('email', '')] = cs

        settings_obj = _parse_field(inbound.get('settings', {}))
        for cli in settings_obj.get('clients', []):
            uuid = cli.get('id', '')
            if not uuid:
                continue
            email = cli.get('email', '')
            stats = stats_by_email.get(email, {})

            up = int(stats.get('up', 0))
            down = int(stats.get('down', 0))
            used = up + down
            total_bytes = int(cli.get('totalGB', 0))
            expiry_ms = int(cli.get('expiryTime', 0))

            if uuid not in result:
                result[uuid] = {
                    'email': email,
                    'enable': cli.get('enable', True),
                    'expiry_ms': expiry_ms,
                    'total_bytes': total_bytes,
                    'up_bytes': up,
                    'down_bytes': down,
                    'used_bytes': used,
                    'inbound_tags': [label],
                }
            else:
                result[uuid]['up_bytes'] += up
                result[uuid]['down_bytes'] += down
                result[uuid]['used_bytes'] += used
                result[uuid]['inbound_tags'].append(label)
                if expiry_ms and (not result[uuid]['expiry_ms'] or expiry_ms < result[uuid]['expiry_ms']):
                    result[uuid]['expiry_ms'] = expiry_ms
                if total_bytes > result[uuid]['total_bytes']:
                    result[uuid]['total_bytes'] = total_bytes

    return result


def _sync_server_inbounds_and_clients(server):
    inbound_count = 0
    client_count = 0
    user_count = 0
    errors = []

    try:
        api_result = XuiAPIClient(server).get_inbounds()
    except XuiAPIException as e:
        errors.append(str(e))
        return inbound_count, client_count, user_count, errors

    import_user, _ = CustomUser.objects.get_or_create(
        username='__imported__',
        defaults={'role': CustomUser.Role.END_USER, 'is_active': False, 'email': 'imported@system.local'}
    )

    managed_uuids = set(ProxySubscription.objects.values_list('xui_client_uuid', flat=True))

    inbound_map = {}
    for ib in api_result.get('obj', []):
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
        inbound_map[ib['id']] = (inbound_obj, ib)
        inbound_count += 1

    uuid_to_data = {}
    for xui_id, (inbound_obj, ib) in inbound_map.items():
        settings_obj = _parse_field(ib.get('settings', {}))
        client_stats = ib.get('clientStats') or []
        stats_by_email = {cs.get('email', ''): cs for cs in client_stats}

        for cli in settings_obj.get('clients', []):
            uuid = cli.get('id', '')
            if not uuid:
                continue
            email = cli.get('email', '').strip().lower()
            stats = stats_by_email.get(email, {})
            up = int(stats.get('up', 0))
            down = int(stats.get('down', 0))
            total_bytes = int(cli.get('totalGB', 0))
            expiry_ms = int(cli.get('expiryTime', 0))

            if uuid not in uuid_to_data:
                uuid_to_data[uuid] = {
                    'cli': cli,
                    'email': email,
                    'inbounds': [],
                    'used_bytes': 0,
                    'total_bytes': total_bytes,
                    'expiry_ms': expiry_ms,
                    'enable': cli.get('enable', True),
                }
            uuid_to_data[uuid]['inbounds'].append(inbound_obj)
            uuid_to_data[uuid]['used_bytes'] += up + down
            if total_bytes > uuid_to_data[uuid]['total_bytes']:
                uuid_to_data[uuid]['total_bytes'] = total_bytes
            if expiry_ms and (not uuid_to_data[uuid]['expiry_ms'] or expiry_ms < uuid_to_data[uuid]['expiry_ms']):
                uuid_to_data[uuid]['expiry_ms'] = expiry_ms

    for uuid, data in uuid_to_data.items():
        if uuid in managed_uuids:
            continue
        email = data['email']

        owner, created = _get_or_create_user_for_email(email)
        if created:
            user_count += 1
        if owner is None:
            owner = import_user

        total_gb = int(data['total_bytes'] / BYTES_PER_GB) if data['total_bytes'] else 0
        used_gb_val = round(data['used_bytes'] / BYTES_PER_GB, 3)
        if data['expiry_ms']:
            expires_at = datetime.fromtimestamp(data['expiry_ms'] / 1000, tz=dt_timezone.utc)
        else:
            expires_at = timezone.now() + timedelta(days=36500)

        sub_url = _build_subscription_url(server, uuid)

        try:
            sub = ProxySubscription.objects.create(
                user=owner,
                xui_client_uuid=uuid,
                subscription_url=sub_url,
                total_allocated_gb=max(total_gb, 0),
                used_gb=used_gb_val,
                expires_at=expires_at,
                is_active=data['enable'],
            )
            for inbound_obj in data['inbounds']:
                SubscriptionConfigMapping.objects.get_or_create(
                    subscription=sub, inbound=inbound_obj,
                    defaults={'xui_client_email': email}
                )
            managed_uuids.add(uuid)
            client_count += 1
        except Exception as e:
            errors.append(f'Client {uuid[:8]}: {e}')
            logger.error('Import client error: %s', e)

    return inbound_count, client_count, user_count, errors


# ---------------------------------------------------------------------------
# MASTER ADMIN
# ---------------------------------------------------------------------------

@require_role('MASTER_ADMIN')
def master_dashboard(request):
    servers = XuiServer.objects.prefetch_related('inbounds').order_by('name')
    return render(request, 'frontend/master_dashboard.html', {
        'total_users': CustomUser.objects.exclude(username='__imported__').count(),
        'active_subs': ProxySubscription.objects.filter(is_active=True).count(),
        'pending_txns': Transaction.objects.filter(status=Transaction.StatusChoices.PENDING).count(),
        'total_servers': servers.count(),
        'active_servers': servers.filter(is_active=True).count(),
        'servers': servers,
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
            Q(username__icontains=search) | Q(email__icontains=search) |
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
        )
    return render(request, 'frontend/master_users.html', {
        'users': users, 'role_filter': role_filter,
        'role_choices': CustomUser.Role.choices, 'search': search,
    })


@require_role('MASTER_ADMIN')
def master_create_user(request):
    if request.method != 'POST':
        return redirect('frontend:master_users')

    email = request.POST.get('email', '').strip().lower()
    role_value = request.POST.get('role', 'END_USER').strip()
    parent_username = request.POST.get('parent_username', '').strip()

    if not email:
        messages.error(request, 'Email is required.')
        return redirect('frontend:master_users')

    if CustomUser.objects.filter(username=email).exists():
        messages.error(request, f'A user with username "{email}" already exists.')
        return redirect('frontend:master_users')

    valid_roles = {r for r, _ in CustomUser.Role.choices}
    if role_value not in valid_roles:
        role_value = CustomUser.Role.END_USER

    parent = None
    if parent_username:
        parent = CustomUser.objects.filter(username=parent_username).first()
        if not parent:
            messages.error(request, f'Parent manager "{parent_username}" not found.')
            return redirect('frontend:master_users')

    new_user = CustomUser.objects.create_user(
        username=email, email=email, password=email,
        role=role_value, parent_manager=parent,
    )
    messages.success(request, f'User "{new_user.username}" created. Default password = email.')
    return redirect('frontend:master_users')


@require_role('MASTER_ADMIN')
def master_servers(request):
    form = XuiServerForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        server = form.save()
        inbound_count, client_count, user_count, errors = _sync_server_inbounds_and_clients(server)
        if errors:
            messages.warning(request, f'Server added but sync had errors: {"; ".join(errors[:3])}')
        else:
            messages.success(
                request,
                f'Server "{server.name}" added. '
                f'Synced {inbound_count} inbound(s), '
                f'imported {client_count} client(s), '
                f'created {user_count} new user account(s).'
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
        messages.success(request, f'Server "{server.name}" {"activated" if server.is_active else "deactivated"}.')
    return redirect('frontend:master_servers')


@require_role('MASTER_ADMIN')
def master_server_clients(request, server_id):
    server = get_object_or_404(XuiServer, id=server_id)
    search = request.GET.get('q', '').strip().lower()
    error = None

    db_map = {}
    for m in SubscriptionConfigMapping.objects.select_related('subscription', 'subscription__user', 'inbound').filter(inbound__server=server):
        uuid = m.subscription.xui_client_uuid
        if uuid not in db_map:
            db_map[uuid] = {'subscription': m.subscription, 'owner': m.subscription.user}

    clients = []
    try:
        live = _build_live_client_map(server)
        for uuid, d in live.items():
            email = d['email']
            db = db_map.get(uuid)
            owner = db['owner'] if db else None
            owner_name = owner.username if owner else None
            display_name = (owner_name if owner_name and owner_name != '__imported__' else None) or email or uuid[:8]

            expiry_str, days_left = _expiry_display(d['expiry_ms'])
            used_gb = d['used_bytes'] / BYTES_PER_GB
            total_gb = d['total_bytes'] / BYTES_PER_GB if d['total_bytes'] else 0
            remaining_gb = max(total_gb - used_gb, 0) if total_gb else None
            pct = min(int(used_gb / total_gb * 100), 100) if total_gb else 0

            entry = {
                'uuid': uuid,
                'email': email,
                'display_name': display_name,
                'enable': d['enable'],
                'inbounds': ', '.join(d['inbound_tags']),
                'inbound_count': len(d['inbound_tags']),
                'up': _fmt_bytes(d['up_bytes']),
                'down': _fmt_bytes(d['down_bytes']),
                'used': _fmt_bytes(d['used_bytes']),
                'used_gb': round(used_gb, 2),
                'total_gb': round(total_gb, 2) if total_gb else 0,
                'remaining': _fmt_bytes(d['total_bytes'] - d['used_bytes']) if d['total_bytes'] else None,
                'remaining_gb': round(remaining_gb, 2) if remaining_gb is not None else None,
                'pct': pct,
                'expiry': expiry_str,
                'days_left': days_left,
                'subscription': db['subscription'] if db else None,
                'owner': owner,
            }

            if search:
                if search not in f"{email} {display_name} {uuid}".lower():
                    continue
            clients.append(entry)
    except Exception as e:
        error = str(e)
        logger.error('master_server_clients error: %s', e)

    clients.sort(key=lambda x: x['display_name'].lower())

    return render(request, 'frontend/master_server_clients.html', {
        'server': server, 'clients': clients, 'error': error, 'search': search,
        'total_count': len(clients),
        'managed_count': sum(1 for c in clients if c['subscription']),
        'untracked_count': sum(1 for c in clients if not c['subscription']),
    })


@require_role('MASTER_ADMIN')
def master_inbound_toggle(request, inbound_id):
    inbound = get_object_or_404(XuiInbound, id=inbound_id)
    if request.method == 'POST':
        inbound.is_available_for_purchase = not inbound.is_available_for_purchase
        inbound.save(update_fields=['is_available_for_purchase'])
        messages.success(request, f'Inbound {inbound} {"enabled" if inbound.is_available_for_purchase else "disabled"}.')
    return redirect('frontend:master_servers')


@require_role('MASTER_ADMIN')
def master_plans(request):
    form = VPNPlanForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Plan created.')
        return redirect('frontend:master_plans')
    return render(request, 'frontend/master_plans.html', {
        'form': form, 'plans': VPNPlan.objects.all().order_by('-created_at'),
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
        'txns': txns, 'status_filter': status_filter,
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
        'form': form, 'tiers': PricingTier.objects.select_related('specific_user').all(),
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
    search = request.GET.get('q', '').strip().lower()
    status_filter = request.GET.get('status', '')
    server_filter = request.GET.get('server', '')

    servers = XuiServer.objects.filter(is_active=True)
    clients = []

    db_map = {}
    for m in SubscriptionConfigMapping.objects.select_related(
        'subscription', 'subscription__user', 'inbound', 'inbound__server'
    ).all():
        uuid = m.subscription.xui_client_uuid
        if uuid not in db_map:
            db_map[uuid] = {'subscription': m.subscription, 'owner': m.subscription.user, 'server': m.inbound.server}

    for server in servers:
        if server_filter and str(server.id) != server_filter:
            continue
        try:
            live = _build_live_client_map(server)
        except Exception as e:
            logger.error('Live map error server %s: %s', server.name, e)
            continue

        for uuid, d in live.items():
            email = d['email']
            db = db_map.get(uuid)
            owner = db['owner'] if db else None
            owner_name = owner.username if owner else None
            display_name = (owner_name if owner_name and owner_name != '__imported__' else None) or email or uuid[:8]

            expiry_str, days_left = _expiry_display(d['expiry_ms'])
            used_bytes = d['used_bytes']
            total_bytes = d['total_bytes']
            used_gb = used_bytes / BYTES_PER_GB
            total_gb = total_bytes / BYTES_PER_GB if total_bytes else 0
            pct = min(int(used_gb / total_gb * 100), 100) if total_gb else 0
            remaining_gb = max(total_gb - used_gb, 0) if total_gb else None

            is_active = d['enable']

            if status_filter == 'active' and not is_active:
                continue
            if status_filter == 'expired' and is_active:
                continue
            if search and search not in f"{email} {display_name} {uuid}".lower():
                continue

            clients.append({
                'uuid': uuid,
                'email': email,
                'display_name': display_name,
                'enable': is_active,
                'server_name': server.name,
                'inbounds': ', '.join(d['inbound_tags']),
                'used': _fmt_bytes(used_bytes),
                'used_gb': round(used_gb, 2),
                'total_gb': round(total_gb, 2) if total_gb else 0,
                'remaining': _fmt_bytes(total_bytes - used_bytes) if total_bytes else None,
                'remaining_gb': round(remaining_gb, 2) if remaining_gb is not None else None,
                'pct': pct,
                'expiry': expiry_str,
                'days_left': days_left,
                'subscription': db['subscription'] if db else None,
                'owner': owner,
            })

    clients.sort(key=lambda x: x['display_name'].lower())

    return render(request, 'frontend/master_subscriptions.html', {
        'clients': clients,
        'total_count': len(clients),
        'servers': XuiServer.objects.all(),
        'search': search,
        'status_filter': status_filter,
        'server_filter': server_filter,
    })


@require_role('MASTER_ADMIN')
def master_deprovision(request, sub_id):
    sub = get_object_or_404(ProxySubscription, id=sub_id)
    if request.method == 'POST':
        sub.is_active = False
        sub.save(update_fields=['is_active', 'updated_at'])
        messages.warning(request, 'Subscription deprovisioned.')
    return redirect('frontend:master_subscriptions')


@require_role('MASTER_ADMIN')
def master_subscription_edit(request, sub_id):
    sub = get_object_or_404(ProxySubscription, id=sub_id)
    if request.method == 'POST':
        try:
            total_gb = int(request.POST.get('total_allocated_gb', sub.total_allocated_gb))
            expires_at_raw = request.POST.get('expires_at', '')
            is_active = request.POST.get('is_active') == '1'
            new_owner_username = request.POST.get('owner_username', '').strip()

            sub.total_allocated_gb = max(total_gb, 0)
            sub.is_active = is_active

            if expires_at_raw:
                from django.utils.dateparse import parse_datetime
                from datetime import datetime as _dt
                parsed = parse_datetime(expires_at_raw)
                if not parsed:
                    try:
                        parsed = _dt.strptime(expires_at_raw, '%Y-%m-%d')
                        parsed = timezone.make_aware(parsed)
                    except ValueError:
                        parsed = None
                if parsed:
                    sub.expires_at = parsed

            if new_owner_username:
                new_owner = CustomUser.objects.filter(username=new_owner_username).first()
                if new_owner:
                    sub.user = new_owner
                else:
                    messages.error(request, f'User "{new_owner_username}" not found.')
                    return redirect('frontend:master_subscriptions')

            sub.save()
            messages.success(request, 'Subscription updated.')
        except (ValueError, TypeError) as e:
            messages.error(request, str(e))
    return redirect('frontend:master_subscriptions')
