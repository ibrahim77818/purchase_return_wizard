# -*- coding: utf-8 -*-
from odoo import models


class StockMove(models.Model):
    _inherit = "stock.move"

    def _prw_is_vendor_receipt(self):
        """Done move that brought goods from a vendor into internal stock.

        Covers original receipts and re-receipts (returns of returns).
        Dropship moves (supplier -> customer) are intentionally excluded.
        """
        self.ensure_one()
        return (
            self.state == "done"
            and self.location_id.usage == "supplier"
            and self.location_dest_id.usage == "internal"
        )

    def _prw_get_returnable_qty(self):
        """Quantity (in the PRODUCT UoM) of this receipt move that can still be
        sent back, i.e. done quantity minus every non-cancelled return already
        created from it (done or still pending)."""
        self.ensure_one()
        product_uom = self.product_id.uom_id
        done_qty = self.product_uom._compute_quantity(
            self.quantity_done, product_uom, rounding_method="HALF-UP")
        returned = 0.0
        for ret in self.returned_move_ids.filtered(lambda m: m.state != "cancel"):
            qty = ret.quantity_done if ret.state == "done" else ret.product_uom_qty
            returned += ret.product_uom._compute_quantity(
                qty, product_uom, rounding_method="HALF-UP")
        return max(done_qty - returned, 0.0)
