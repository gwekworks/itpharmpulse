"""Shared view helpers: rendering and the admin gate."""
from __future__ import annotations

from django.http import HttpResponseForbidden
from django.shortcuts import render


def render_page(request, template_name: str, ctx_fn=None):
    ctx = ctx_fn(request) if ctx_fn else {}
    return render(request, f"pharmacypulse/pages/{template_name}", ctx)


def admin_required(view):
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.shortcuts import redirect
            return redirect(f"/login?next={request.path}")
        if request.user.role != "admin":
            return HttpResponseForbidden("admin only")
        return view(request, *args, **kwargs)
    return wrapped