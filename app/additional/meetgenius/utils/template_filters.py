from app.additional import blueprint

@blueprint.app_template_filter('format_time')
def format_time(seconds):
    """Converts seconds to MM:SS format."""
    if seconds is None:
        return "00:00"
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes:02d}:{seconds:02d}" 