from werkzeug.exceptions import ServiceUnavailable

from odoo import models
from odoo.http import Response, request

from . import maintenance

# the update's own progress screen keeps working, and the files it needs
EXEMPT = ("/web/dataset/call_kw/mtbg.github.batch", "/web/assets/", "/web/static/", "/web/webclient/", "/websocket")
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="refresh" content="30"><title>Updating · Actualizando</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;color:#1f5f7a}
div{text-align:center;padding:24px}h1{font-size:1.6rem;margin:0 0 8px}p{color:#555;margin:4px}</style></head>
<body><div><h1>Updating, back in a minute</h1><p>Actualizando, volvemos en un minuto.</p></div></body></html>"""


class UpdatingError(ServiceUnavailable):
    """Maintenance: its own page (served as is, not as a website error page, which has no 503 template)."""

    def __init__(self):
        super().__init__(response=Response(PAGE, status=503, headers=[("Retry-After", "60")],
                                           content_type="text/html; charset=utf-8"))


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _pre_dispatch(cls, rule, args):
        """While an update upgrades this database (maintenance), pages answer 503 and try again by themselves."""
        if maintenance.active(request.db) and not request.httprequest.path.startswith(EXEMPT):
            raise UpdatingError()
        return super()._pre_dispatch(rule, args)

    @classmethod
    def _handle_error(cls, exception):
        if isinstance(exception, UpdatingError):
            return exception.response
        return super()._handle_error(exception)
