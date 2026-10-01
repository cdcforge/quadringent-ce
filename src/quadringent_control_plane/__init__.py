"""Modèle de lecture du control plane Quadringent.

Ce package ne pilote pas le data plane : il projette uniquement ses constats.
"""

from .model import PipelineProjection, ProjectionError, SourceDescriptor, StageProjection
from .projection import build_overview, project_console_document
from .onboarding import evaluate_onboarding
from .repository import ProjectionRepository, ProjectionSnapshot, parse_source_spec

__all__ = [
    "PipelineProjection",
    "ProjectionRepository",
    "ProjectionSnapshot",
    "ProjectionError",
    "SourceDescriptor",
    "StageProjection",
    "build_overview",
    "project_console_document",
    "parse_source_spec",
    "evaluate_onboarding",
]
