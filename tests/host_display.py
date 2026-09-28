"""Construct the host configuration display across supported AstrBot revisions."""

import inspect

from astrbot.dashboard.services.config_service import ConfigDisplayService


def display_service(host):
    if "logo_service" in inspect.signature(ConfigDisplayService).parameters:
        from astrbot.dashboard.services.logo_service import LogoService

        return ConfigDisplayService(host, LogoService(host))
    return ConfigDisplayService(host)
