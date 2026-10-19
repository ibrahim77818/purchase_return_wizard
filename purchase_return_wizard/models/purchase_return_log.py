# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Short labels: they are shown as badges in a dense wizard table.
SCENARIOS = [
    ("not_received", "Not Received"),
    ("received_not_billed", "Not Billed"),
    ("partially_billed", "Partially Billed"),
    ("billed", "Billed"),
]

_WRITE_CTX = "prw_allow_log_write"


class PurchaseReturnLog(models.Model):
    """Immutable audit trail of every return processed through the wizard."""

    _name = "purchase.return.log"
    _description = "Purchase Return Log"
    _order = "date desc, id desc"

    name = fields.Char(readonly=True, copy=False, default="/")
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    user_id = fields.Many2one(
        "res.users", string="Processed By", default=lambda self: self.env.user, readonly=True)
    purchase_id = fields.Many2one(
        "purchase.order", string="Purchase Order", required=True,
        ondelete="restrict", index=True, readonly=True)
    partner_id = fields.Many2one(related="purchase_id.partner_id", store=True)
    company_id = fields.Many2one(related="purchase_id.company_id", store=True)
    currency_id = fields.Many2one(related="purchase_id.currency_id")
    routing = fields.Selection(
        [("return_only", "Return Transfer Only"),
         ("return_credit", "Return Transfer + Credit Note")],
        readonly=True, required=True)
    origin_picking_id = fields.Many2one(
        "stock.picking", string="Started From Receipt", readonly=True,
        ondelete="set null", index=True,
        help="Set when the return was launched from a receipt instead of the PO.")
    return_picking_ids = fields.Many2many(
        "stock.picking", "purchase_return_log_picking_rel", "log_id", "picking_id",
        string="Return Transfers", readonly=True)
    refund_id = fields.Many2one(
        "account.move", string="Vendor Credit Note", readonly=True, ondelete="set null")
    refund_state = fields.Selection(related="refund_id.state", string="Credit Note Status")
    note = fields.Text("Return Reason", readonly=True)
    line_ids = fields.One2many("purchase.return.log.line", "log_id", readonly=True)
    amount_credited = fields.Monetary(
        compute="_compute_amount_credited", store=True, currency_field="currency_id")

    @api.depends("line_ids.amount_credited")
    def _compute_amount_credited(self):
        for log in self:
            log.amount_credited = sum(log.line_ids.mapped("amount_credited"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "/") == "/":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code(
                    "purchase.return.log") or "/"
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get(_WRITE_CTX):
            raise UserError(_("Purchase return logs are immutable."))
        return super().write(vals)

    def unlink(self):
        if not self.env.is_superuser():
            raise UserError(_("Purchase return logs cannot be deleted."))
        return super().unlink()


class PurchaseReturnLogLine(models.Model):
    _name = "purchase.return.log.line"
    _description = "Purchase Return Log Line"

    log_id = fields.Many2one(
        "purchase.return.log", required=True, ondelete="cascade", index=True)
    purchase_line_id = fields.Many2one(
        "purchase.order.line", string="PO Line", ondelete="set null", readonly=True)
    product_id = fields.Many2one("product.product", readonly=True)
    product_uom_id = fields.Many2one("uom.uom", string="UoM", readonly=True)
    scenario = fields.Selection(SCENARIOS, string="Status", readonly=True)
    qty_returned = fields.Float(digits="Product Unit of Measure", readonly=True)
    qty_credited = fields.Float(digits="Product Unit of Measure", readonly=True)
    price_unit = fields.Float(digits="Product Price", readonly=True)
    currency_id = fields.Many2one(related="log_id.currency_id")
    amount_credited = fields.Monetary(readonly=True, currency_field="currency_id")

    def write(self, vals):
        if not self.env.context.get(_WRITE_CTX):
            raise UserError(_("Purchase return logs are immutable."))
        return super().write(vals)
