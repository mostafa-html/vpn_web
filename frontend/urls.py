from django.urls import path
from . import views

app_name = 'frontend'

urlpatterns = [
    # ── Auth ──────────────────────────────────────────────────────────────────
    path('', views.index, name='index'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('register/', views.register_view, name='register'),
    path('dashboard/', views.dashboard, name='dashboard'),

    # ── End User ──────────────────────────────────────────────────────────
    path('plans/', views.plans, name='plans'),
    path('plans/buy/<int:plan_id>/', views.buy_plan, name='buy_plan'),
    path('plans/custom/', views.custom_plan, name='custom_plan'),
    path('subscriptions/', views.subscriptions, name='subscriptions'),
    path('subscriptions/<int:sub_id>/', views.subscription_detail, name='subscription_detail'),
    path('wallet/', views.wallet, name='wallet'),
    path('transactions/', views.transactions, name='transactions'),

    # ── Sub-Admin ─────────────────────────────────────────────────────────
    path('subadmin/dashboard/', views.subadmin_dashboard, name='subadmin_dashboard'),
    path('subadmin/users/', views.subadmin_users, name='subadmin_users'),
    path('subadmin/users/create/', views.subadmin_create_user, name='subadmin_create_user'),

    # ── Master Admin ───────────────────────────────────────────────────────
    path('master/dashboard/', views.master_dashboard, name='master_dashboard'),
    path('master/users/', views.master_users, name='master_users'),
    path('master/servers/', views.master_servers, name='master_servers'),
    path('master/servers/<uuid:server_id>/toggle/', views.master_server_toggle, name='master_server_toggle'),
    path('master/servers/<uuid:server_id>/clients/', views.master_server_clients, name='master_server_clients'),
    path('master/inbounds/<int:inbound_id>/toggle/', views.master_inbound_toggle, name='master_inbound_toggle'),
    path('master/plans/', views.master_plans, name='master_plans'),
    path('master/plans/<int:plan_id>/delete/', views.master_plan_delete, name='master_plan_delete'),
    path('master/transactions/', views.master_transactions, name='master_transactions'),
    path('master/transactions/<uuid:tx_id>/review/', views.master_transaction_review, name='master_transaction_review'),
    path('master/pricing/', views.master_pricing, name='master_pricing'),
    path('master/pricing/<int:tier_id>/delete/', views.master_pricing_delete, name='master_pricing_delete'),
    path('master/subscriptions/', views.master_subscriptions, name='master_subscriptions'),
    path('master/subscriptions/<int:sub_id>/deprovision/', views.master_deprovision, name='master_deprovision'),
]
