# Ported from hermes-agent tools/__init__.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Tools package namespace. Kept side-effect free: importing ``tools`` must not
load the tool stack (some subsystems import it while ``hermes_cli.config`` is
still initializing). Import concrete submodules directly."""


def check_file_requirements():
    """File tools only require terminal backend availability."""
    from .terminal_tool import check_terminal_requirements

    return check_terminal_requirements()


__all__ = ["check_file_requirements"]
