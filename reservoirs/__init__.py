# reservoirs/__init__.py
from flask import Blueprint

reservoirs_bp = Blueprint(
    "reservoirs",
    __name__,
    template_folder="../templates"
)

from . import routes  # noqa: F401



