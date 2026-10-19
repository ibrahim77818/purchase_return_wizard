# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    # Restricted to purchase users: inventory-only users have no ACL on the
    # log model and would otherwise get an AccessError when opening a receipt.
    prw_return_log_ids = fields.One2many(
        "purchase.return.log", "origin_picking_id", string="Purchase Returns",
        readonly=True, copy=False, groups="purchase.group_purchase_user")
    prw_return_log_count = fields.Integer(
        compute="_compute_prw_return_log_count", groups="purchase.group_purchase_user")
    prw_can_return = fields.Boolean(
        compute="_compute_prw_can_return",
        help="Technical: shows the Return / Refund button on done PO receipts.")

    @api.depends("prw_return_log_ids")
    def _compute_prw_return_log_count(self):
        for picking in self:
            picking.prw_return_log_count = len(picking.prw_return_log_ids)

    @api.depends("state", "picking_type_code", "move_ids.state",
                 "move_ids.purchase_line_id", "move_ids.location_id",
                 "move_ids.location_dest_id")
    def _compute_prw_can_return(self):
        """Cheap visibility check. The full set of rules (and the user-facing
        explanation) lives in `_prw_check_returnable`, which runs on click."""
        for picking in self:
            picking.prw_can_return = (
                picking.state == "done"
                and picking.picking_type_code == "incoming"
                # sudo: visibility only; never fails on PO-line read rights
                and bool(picking.sudo()._prw_get_po_receipt_moves())
            )

    # ------------------------------------------------------------------ helpers
    def _prw_get_po_receipt_moves(self):
        """Done supplier -> internal moves of this picking linked to a PO line.

        Excludes: returns (internal -> supplier), dropship (supplier ->
        customer), cancelled moves and products added by hand without a PO line.
        """
        self.ensure_one()
        return self.move_ids.filtered(
            lambda m: m.purchase_line_id and m._prw_is_vendor_receipt())

    def _prw_check_returnable(self):
        """Raise a clear UserError for every receipt that cannot be processed.

        :return: the single purchase.order the receipt belongs to
        """
        self.ensure_one()
        if self.state != "done":
            raise UserError(_("Only validated receipts can be returned (%s is %s).",
                              self.name, dict(self._fields["state"].selection)[self.state]))
        if self.picking_type_code != "incoming":
            raise UserError(_(
                "%s is not a vendor receipt. Use the Return / Refund button on the "
                "original receipt or on the purchase order.", self.name))
        moves = self._prw_get_po_receipt_moves()
        if not moves:
            raise UserError(_(
                "%s has no received products linked to a purchase order line "
                "(dropship, return transfer or manual receipt). Use the standard "
                "Return button instead.", self.name))
        orders = moves.purchase_line_id.order_id
        if len(orders) > 1:
            raise UserError(_(
                "Receipt %(picking)s contains products from several purchase orders "
                "(%(orders)s). Process the return from each purchase order instead.",
                picking=self.name, orders=", ".join(orders.mapped("name"))))
        if orders.state not in ("purchase", "done"):
            raise UserError(_("Purchase order %s is not confirmed.", orders.name))
        if orders.company_id != self.company_id:
            raise UserError(_(
                "Receipt %(picking)s and purchase order %(po)s belong to different companies.",
                picking=self.name, po=orders.name))
        return orders

    # ------------------------------------------------------------------ actions
    def action_open_purchase_return_wizard(self):
        self.ensure_one()
        if not self.env.user.has_group("purchase_return_wizard.group_purchase_return"):
            raise AccessError(_("You are not allowed to process purchase returns."))
        order = self._prw_check_returnable()
        wizard = self.env["purchase.return.wizard"].with_company(
            self.company_id).create_from_order(order, picking=self)
        return wizard._action_reopen()

    def action_view_prw_return_logs(self):
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Purchase Returns"),
            "res_model": "purchase.return.log",
            "domain": [("origin_picking_id", "=", self.id)],
            "context": {"create": False},
        }
        if self.prw_return_log_count == 1:
            action.update(view_mode="form", res_id=self.prw_return_log_ids.id)
        else:
            action["view_mode"] = "tree,form"
        return action
