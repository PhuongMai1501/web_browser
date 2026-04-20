"""
scenarios/hooks/ — chứa các module Python register hook vào HOOK_REGISTRY.

Import package này = trigger side-effect của tất cả hook module bên dưới.
Generic runner / startup nên import scenarios.hooks để chắc chắn các hook
đã register trước khi validate scenario spec.
"""

from . import chang_login_hooks  # noqa: F401
