import json

from odoo import api, fields, models
from odoo.exceptions import UserError

try:
    from zeep.helpers import serialize_object
except ImportError:
    from odoo.tools.zeep.helpers import serialize_object


class AfipWsConsult(models.TransientModel):
    _name = "l10n_ar_afipws_fe.ws.consult"
    _description = "Consult Invoice Data in ARCA"

    journal_id = fields.Many2one(
        "account.journal",
        required=True,
        domain="[('type', '=', 'sale'), ('afip_ws', '!=', False)]",
    )
    available_document_type_ids = fields.Many2many(
        "l10n_latam.document.type",
        compute="_compute_available_document_type_ids",
    )
    document_type_id = fields.Many2one(
        "l10n_latam.document.type",
        required=True,
    )
    number = fields.Integer(required=True)

    queried = fields.Boolean(readonly=True)
    result = fields.Char(readonly=True)
    amount_total = fields.Float(readonly=True)
    currency_code = fields.Char(readonly=True)
    authorization_code = fields.Char(string="CAE / Authorization Code", readonly=True)
    authorization_due_date = fields.Char(readonly=True)
    invoice_date = fields.Char(readonly=True)
    response_data = fields.Text(readonly=True)

    @api.depends("journal_id")
    def _compute_available_document_type_ids(self):
        document_type_model = self.env["l10n_latam.document.type"]
        for wizard in self:
            wizard.available_document_type_ids = (
                document_type_model.search(
                    wizard.journal_id._get_journal_codes_domain()
                )
                if wizard.journal_id
                else document_type_model
            )

    @api.onchange("journal_id")
    def _onchange_journal_id(self):
        if self.document_type_id not in self.available_document_type_ids:
            self.document_type_id = self.available_document_type_ids[:1]
        self._clear_response()

    @api.onchange("document_type_id", "number")
    def _onchange_query(self):
        self._clear_response()

    def _clear_response(self):
        self.update(
            {
                "queried": False,
                "result": False,
                "amount_total": 0.0,
                "currency_code": False,
                "authorization_code": False,
                "authorization_due_date": False,
                "invoice_date": False,
                "response_data": False,
            }
        )

    @staticmethod
    def _first_value(data, *keys):
        for key in keys:
            if data.get(key) not in (None, False, ""):
                return data[key]
        return False

    @staticmethod
    def _serialize(value):
        return serialize_object(value, dict) if value else False

    def _raise_ws_error(self, error):
        serialized_error = self._serialize(error)
        raise UserError(
            self.env._(
                "ARCA returned an error:\n%(error)s",
                error=json.dumps(
                    serialized_error or str(error),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
            )
        )

    def _consult_wsfe(self, service, auth, request):
        response = service.FECompConsultar(auth, request)
        if response.Errors:
            self._raise_ws_error(response.Errors)
        return response.ResultGet

    def _consult_wsfex(self, service, auth, request):
        response = service.FEXGetCMP(
            auth,
            {
                "Cbte_tipo": request["CbteTipo"],
                "Punto_vta": request["PtoVta"],
                "Cbte_nro": request["CbteNro"],
            },
        )
        if response.FEXErr and response.FEXErr.ErrCode:
            self._raise_ws_error(response.FEXErr)
        return response.FEXResultGet

    def _consult_wsbfe(self, service, auth, request):
        response = service.BFEGetCMP(
            auth,
            {
                "Tipo_cbte": request["CbteTipo"],
                "Punto_vta": request["PtoVta"],
                "Cbte_nro": request["CbteNro"],
            },
        )
        if response.BFEErr and response.BFEErr.ErrCode:
            self._raise_ws_error(response.BFEErr)
        return response.BFEResultGet

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_get_last_number(self):
        self.ensure_one()
        if not self.journal_id or not self.document_type_id:
            raise UserError(self.env._("Select a journal and document type first."))
        self.number = self.journal_id.get_pyafipws_last_invoice(self.document_type_id)
        self._clear_response()
        return self._reopen()

    def action_consult(self):
        self.ensure_one()
        if self.number <= 0:
            raise UserError(self.env._("Enter a valid invoice number."))

        afip_ws = self.journal_id.afip_ws
        if not afip_ws:
            raise UserError(
                self.env._(
                    "No ARCA webservice is configured for journal %(journal)s.",
                    journal=self.journal_id.display_name,
                )
            )

        connection = self.journal_id.company_id.get_connection(afip_ws)
        client, auth = connection._get_client()
        request = {
            "CbteTipo": int(self.document_type_id.code),
            "CbteNro": self.number,
            "PtoVta": self.journal_id.l10n_ar_afip_pos_number,
        }

        consult_method = getattr(self, f"_consult_{afip_ws}", None)
        if not consult_method:
            raise UserError(
                self.env._("ARCA webservice %(ws)s is not supported.", ws=afip_ws)
            )
        response = consult_method(client.service, auth, request)
        data = self._serialize(response)
        if not data:
            raise UserError(self.env._("ARCA did not return invoice data."))

        amount_total = self._first_value(data, "ImpTotal", "Imp_total") or 0.0
        self.write(
            {
                "queried": True,
                "result": self._first_value(data, "Resultado"),
                "amount_total": float(amount_total),
                "currency_code": self._first_value(
                    data, "MonId", "Moneda_Id", "Imp_moneda_Id"
                ),
                "authorization_code": self._first_value(
                    data, "CodAutorizacion", "CAE", "Cae"
                ),
                "authorization_due_date": self._first_value(
                    data, "FchVto", "FchVencCAE", "Fch_venc_Cae"
                ),
                "invoice_date": self._first_value(
                    data, "CbteFch", "FechaCbte", "Fecha_cbte", "Fecha_cbte_orig"
                ),
                "response_data": json.dumps(
                    data, ensure_ascii=False, indent=2, default=str
                ),
            }
        )
        return self._reopen()
