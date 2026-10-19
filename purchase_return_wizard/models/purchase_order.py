# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    return_log_ids = fields.One2many(
        "purchase.return.log", "purchase_id", string="Return Logs",
        readonly=True, copy=False)
    return_log_count = fields.Integer(compute="_compute_return_log_count")

    @api.depends("return_log_ids")
    def _compute_return_log_count(self):
        for order in self:
            order.return_log_count = len(order.return_log_ids)

    def action_open_return_wizard(self):
        self.ensure_one()
        if not self.env.user.has_group("purchase_return_wizard.group_purchase_return"):
            raise AccessError(_("You are not allowed to process purchase returns."))
        wizard = self.env["purchase.return.wizard"].create_from_order(self)
        return wizard._action_reopen()

    def action_view_return_logs(self):
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Returns"),
            "res_model": "purchase.return.log",
            "domain": [("purchase_id", "=", self.id)],
            "context": {"create": False},
        }
        if self.return_log_count == 1:
            action.update(view_mode="form", res_id=self.return_log_ids.id)
        else:
            action["view_mode"] = "list,form"
        return action
