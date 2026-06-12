from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from billing_engine.models import CustomUser, VPNPlan, XuiServer, Transaction, PricingTier


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Username',
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Password',
        })
    )


class RegisterForm(UserCreationForm):
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Email (optional)',
        })
    )

    class Meta:
        model = CustomUser
        fields = ('username', 'email', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        input_cls = 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500'
        self.fields['username'].widget.attrs.update({'class': input_cls, 'placeholder': 'Username'})
        self.fields['password1'].widget.attrs.update({'class': input_cls, 'placeholder': 'Password'})
        self.fields['password2'].widget.attrs.update({'class': input_cls, 'placeholder': 'Confirm Password'})


class WalletTopUpForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ('amount', 'payment_ref_code', 'screenshot')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        input_cls = 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500'
        self.fields['amount'].widget.attrs.update({'class': input_cls, 'placeholder': 'Amount (e.g. 50.00)', 'min': '1'})
        self.fields['payment_ref_code'].widget.attrs.update({'class': input_cls, 'placeholder': 'Payment Reference Code (uppercase alphanumeric)'})
        self.fields['screenshot'].widget.attrs.update({'class': 'w-full text-slate-300 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:bg-indigo-600 file:text-white hover:file:bg-indigo-700'})


class PlanPurchaseForm(forms.Form):
    plan_id = forms.IntegerField(widget=forms.HiddenInput())


class CustomPurchaseForm(forms.Form):
    custom_gb = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Data (GB)',
        })
    )
    custom_days = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Duration (days)',
        })
    )


class CreateManagedUserForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Username',
        })
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Email (optional)',
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Password',
        })
    )


class VPNPlanForm(forms.ModelForm):
    class Meta:
        model = VPNPlan
        fields = ('name', 'total_gb', 'duration_days', 'is_visible')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        input_cls = 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500'
        for field in ('name', 'total_gb', 'duration_days'):
            self.fields[field].widget.attrs.update({'class': input_cls})
        self.fields['is_visible'].widget.attrs.update({'class': 'h-5 w-5 text-indigo-500'})


class XuiServerForm(forms.ModelForm):
    class Meta:
        model = XuiServer
        fields = ('name', 'hostname', 'ip_address', 'api_port', 'base_path',
                  'admin_username', 'admin_password', 'max_client_capacity',
                  'is_active', 'use_ssl')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        input_cls = 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500'
        for f in ('name', 'hostname', 'ip_address', 'api_port', 'base_path',
                  'admin_username', 'admin_password', 'max_client_capacity'):
            self.fields[f].widget.attrs.update({'class': input_cls})
        self.fields['hostname'].widget.attrs['placeholder'] = 'e.g. gg.mx11.ir (optional, uses IP if blank)'
        self.fields['base_path'].widget.attrs['placeholder'] = 'e.g. /4bfAPdC269HYSj1c24/'
        self.fields['is_active'].widget.attrs.update({'class': 'h-5 w-5 text-indigo-500'})
        self.fields['use_ssl'].widget.attrs.update({'class': 'h-5 w-5 text-indigo-500'})
        self.fields['use_ssl'].label = 'Use HTTPS (SSL)'
        self.fields['use_ssl'].help_text = 'Enable if your 3x-ui panel uses HTTPS'


class TransactionReviewForm(forms.Form):
    STATUS_CHOICES = [
        ('APPROVED', 'Approve'),
        ('REJECTED', 'Reject'),
    ]
    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        widget=forms.Select(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
        })
    )
    rejection_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500',
            'placeholder': 'Rejection reason (required if rejecting)',
            'rows': 3,
        })
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('status') == 'REJECTED' and not cleaned.get('rejection_reason'):
            raise forms.ValidationError('A rejection reason is required when rejecting a transaction.')
        return cleaned


class PricingTierForm(forms.ModelForm):
    class Meta:
        model = PricingTier
        fields = ('target_role', 'specific_user', 'price_per_gb', 'price_per_day')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        input_cls = 'w-full bg-slate-800 border border-slate-600 text-white rounded-lg px-4 py-3 focus:outline-none focus:border-indigo-500'
        for f in ('target_role', 'specific_user', 'price_per_gb', 'price_per_day'):
            self.fields[f].widget.attrs.update({'class': input_cls})
        self.fields['specific_user'].queryset = CustomUser.objects.filter(
            role__in=[CustomUser.Role.SUB_ADMIN, CustomUser.Role.END_USER]
        )
        self.fields['specific_user'].required = False
