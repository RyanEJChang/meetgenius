from flask import Blueprint

# Create a Blueprint for the MeetGenius module
meetgenius_bp = Blueprint(
    'meetgenius',
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/meetgenius/static'
)

# Import routes and models to make them accessible
from .main import routes
from .api import routes
from .utils import template_filters
