"""Stub routes for pages built in future phases — prevents 404s.

All stub pages have been implemented. This module is kept as a no-op
router for backward compatibility with main.py imports.
"""

from fastapi import APIRouter

router = APIRouter()

# No remaining stub pages — admin.py and search.py are fully implemented.
