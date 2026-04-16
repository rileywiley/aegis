"""Web layer — shared Jinja2 templates instance."""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="aegis/web/templates")
