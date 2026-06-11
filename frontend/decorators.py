from functools import wraps
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required


def require_role(*roles):
    """
    Decorator that checks the authenticated user's role.
    Redirects to the appropriate dashboard if not authorized.
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped(request, *args, **kwargs):
            if request.user.role not in roles:
                return redirect('frontend:dashboard')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
