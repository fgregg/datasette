from datasette import hookimpl, Response
from .utils import add_cors_headers
from .utils.asgi import (
    Base400,
)
from .views.base import DatasetteError
from markupsafe import Markup
import traceback

try:
    import ipdb as pdb
except ImportError:
    import pdb

try:
    import rich
except ImportError:
    rich = None


@hookimpl(trylast=True)
def handle_exception(datasette, request, exception):
    async def inner():
        if datasette.pdb:
            pdb.post_mortem(exception.__traceback__)

        if rich is not None:
            rich.get_console().print_exception(show_locals=True)

        title = None
        if isinstance(exception, Base400):
            status = exception.status
            info = {}
            message = exception.args[0]
        elif isinstance(exception, DatasetteError):
            status = exception.status
            info = exception.error_dict
            message = exception.message
            if exception.message_is_html:
                message = Markup(message)
            title = exception.title
        else:
            status = 500
            info = {}
            message = str(exception)
            traceback.print_exc()
        templates = [f"{status}.html", "error.html"]
        info.update(
            {
                "ok": False,
                "error": message,
                "status": status,
                "title": title,
            }
        )
        headers = {}
        if datasette.cors:
            add_cors_headers(headers)
        if request.path.split("?")[0].endswith(".json"):
            return Response.json(info, status=status, headers=headers)
        else:
            # Render via datasette.render_template so the error page picks up
            # extra_css_urls / extra_js_urls / datasette_version / request /
            # etc. — otherwise stylesheets and scripts configured via
            # extra_css_urls (e.g. a custom theme) are dropped and the page
            # looks unstyled.
            return Response.html(
                await datasette.render_template(
                    templates,
                    info,
                    request=request,
                ),
                status=status,
                headers=headers,
            )

    return inner
