# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.tools import float_round


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    def _prw_get_receipt_moves(self, picking=None):
        """Done vendor receipt moves of this line, newest first (LIFO).

        Returns are allocated against the most recent receipts first, which
        matches how goods are usually still physically on hand.

        :param picking: optional receipt; when given, only the moves of that
            receipt are considered (wizard launched from a receipt).
        """
        self.ensure_one()
        moves = self.move_ids.filtered(lambda m: m._prw_is_vendor_receipt())
        if picking:
            moves = moves.filtered(lambda m: m.picking_id == picking)
        return moves.sorted(key=lambda m: (m.date, m.id), reverse=True)

    def _prw_get_return_metrics(self, picking=None):
        """Batch-compute everything the wizard needs, in the PO LINE UoM.

        Billing and received figures are always PO-line wide (that is the level
        at which Odoo bills), while the *physically returnable* quantity is
        restricted to ``picking`` when one is given.

        :return: {pol_id: {
            qty_ordered, qty_received (net of refunded returns),
            qty_billed   (posted bills - posted refunds - draft refunds),
            draft_bill_qty (draft in_invoice qty, for a UI warning),
            qty_returnable (min of ordered / received / physically returnable),
            price_unit   (weighted average billed unit price, PO currency),
            tax_ids      (taxes of the latest posted bill line, else PO taxes),
        }}
        """
        result = {}
        today = fields.Date.context_today(self)
        for line in self:
            uom = line.product_uom
            rounding = uom.rounding
            billed = draft_bill = 0.0
            billed_subtotal = billed_qty_for_price = 0.0
            last_taxes = self.env["account.tax"]

            for aml in line.invoice_lines.sorted("id"):
                if aml.display_type != "product":
                    continue
                move = aml.move_id
                qty = aml.quantity
                if aml.product_uom_id and aml.product_uom_id != uom:
                    qty = aml.product_uom_id._compute_quantity(qty, uom)

                if move.move_type == "in_invoice":
                    if move.state == "posted":
                        billed += qty
                        subtotal = aml.price_subtotal
                        if move.currency_id != line.currency_id:
                            subtotal = move.currency_id._convert(
                                subtotal, line.currency_id, line.company_id,
                                move.invoice_date or move.date or today)
                        billed_subtotal += subtotal
                        billed_qty_for_price += qty
                        last_taxes = aml.tax_ids
                    elif move.state == "draft":
                        draft_bill += qty
                elif move.move_type == "in_refund" and move.state in ("posted", "draft"):
                    # draft refunds are counted too: prevents crediting twice
                    billed -= qty

            scope_moves = line._prw_get_receipt_moves(picking=picking)
            product_uom = line.product_id.uom_id
            physically_returnable = product_uom._compute_quantity(
                sum(m._prw_get_returnable_qty() for m in scope_moves),
                uom, rounding_method="HALF-UP")
            qty_in_receipt = sum(
                m.product_uom._compute_quantity(m.quantity, uom, rounding_method="HALF-UP")
                for m in scope_moves) if picking else 0.0

            if billed_qty_for_price:
                price_unit = billed_subtotal / billed_qty_for_price
            elif line.product_qty:
                price_unit = line.price_subtotal / line.product_qty
            else:
                price_unit = line.price_unit

            received = line.qty_received
            result[line.id] = {
                "qty_ordered": line.product_qty,
                "qty_received": float_round(received, precision_rounding=rounding),
                "qty_in_receipt": float_round(qty_in_receipt, precision_rounding=rounding),
                "qty_billed": float_round(billed, precision_rounding=rounding),
                "draft_bill_qty": float_round(draft_bill, precision_rounding=rounding),
                "qty_returnable": float_round(
                    max(min(line.product_qty, received, physically_returnable), 0.0),
                    precision_rounding=rounding),
                "price_unit": price_unit,
                "tax_ids": last_taxes or line.taxes_id,
            }
        return result
