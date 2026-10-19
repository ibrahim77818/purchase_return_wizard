# -*- coding: utf-8 -*-
"""Return / Refund launched from a PO receipt (stock.picking)."""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import PurchaseReturnCommon


@tagged("post_install", "-at_install")
class TestPickingReturn(PurchaseReturnCommon):

    def _open_from_picking(self, picking):
        action = picking.action_open_purchase_return_wizard()
        self.assertEqual(action["res_model"], "purchase.return.wizard")
        return self.env["purchase.return.wizard"].browse(action["res_id"])

    # ------------------------------------------------------------ visibility
    def test_70_button_visibility_and_guards(self):
        po = self._create_po([(self.bolt, 10, 20)])
        receipt = po.picking_ids
        self.assertFalse(receipt.prw_can_return, "not validated yet")
        with self.assertRaises(UserError):
            receipt.action_open_purchase_return_wizard()

        self._receive(po, {self.bolt: 10})
        self.assertTrue(receipt.prw_can_return)

        wizard = self._open_from_picking(receipt)
        self._wline(wizard, self.bolt).write({"action": "return", "return_qty": 2})
        log = self._confirm(wizard)
        return_picking = log.return_picking_ids
        self.assertEqual(return_picking.picking_type_code, "outgoing")
        self.assertFalse(return_picking.prw_can_return, "a return is not a receipt")
        with self.assertRaises(UserError):
            return_picking.action_open_purchase_return_wizard()

    # ----------------------------------------------------------------- scope
    def test_71_only_lines_of_this_receipt_are_listed(self):
        po = self._create_po([(self.beam, 10, 100), (self.bolt, 10, 20)])
        receipt = self._receive(po, {self.beam: 10})  # bolt goes to a backorder
        wizard = self._open_from_picking(receipt)
        self.assertEqual(wizard.picking_id, receipt)
        self.assertEqual(wizard.line_ids.product_id, self.beam)
        self.assertEqual(self._wline(wizard, self.beam).qty_in_receipt, 10)

    def test_72_allocation_restricted_to_this_receipt(self):
        po = self._create_po([(self.bolt, 10, 20)])
        first = self._receive(po, {self.bolt: 4})
        second = self._receive(po, {self.bolt: 6})

        wizard = self._open_from_picking(first)
        line = self._wline(wizard, self.bolt)
        self.assertEqual(line.qty_received, 10, "PO-wide figure")
        self.assertEqual(line.qty_in_receipt, 4)
        self.assertEqual(line.qty_returnable, 4, "capped to this receipt")
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 5})
        line.write({"action": "return", "return_qty": 3})

        log = self._confirm(wizard)

        ret = log.return_picking_ids
        self.assertEqual(len(ret), 1)
        self.assertEqual(ret.move_ids.origin_returned_move_id.picking_id, first,
                         "never LIFO onto the newer receipt")
        self.assertEqual(self._returned_qty(ret, self.bolt), 3)
        self.assertEqual(log.origin_picking_id, first)
        self.assertEqual(first.prw_return_log_count, 1)
        self.assertEqual(second.prw_return_log_count, 0)
        self.assertEqual(po.return_log_count, 1)
        self.assertEqual(self._pol(po, self.bolt).qty_received, 7)

    # --------------------------------------------------------------- routing
    def test_73_billed_receipt_creates_credit_note(self):
        po = self._create_po([(self.beam, 10, 100)])
        receipt = self._receive(po, {self.beam: 10})
        self._bill(po)
        wizard = self._open_from_picking(receipt)
        self._wline(wizard, self.beam).write({"action": "return", "return_qty": 4})
        log = self._confirm(wizard)
        self.assertEqual(log.routing, "return_credit")
        self.assertEqual(log.refund_id.move_type, "in_refund")
        self.assertEqual(log.refund_id.state, "draft")
        self.assertEqual(log.refund_id.invoice_line_ids.quantity, 4)
        self.assertEqual(log.return_picking_ids.state, "done")

    def test_74_partial_bill_across_receipts_uses_po_level_billing(self):
        """Billing is per PO line: credit follows the PO-wide formula even
        when the return is launched from one receipt."""
        po = self._create_po([(self.bolt, 10, 20)])
        first = self._receive(po, {self.bolt: 4})
        self._receive(po, {self.bolt: 6})
        self._bill(po, qty_by_product={self.bolt: 8})  # R=10, B=8
        wizard = self._open_from_picking(first)
        line = self._wline(wizard, self.bolt)
        line.write({"action": "return", "return_qty": 4})  # Q=4 -> credit 2
        self.assertEqual(line.credit_qty, 2)
        log = self._confirm(wizard)
        self.assertEqual(log.refund_id.invoice_line_ids.quantity, 2)

    # ---------------------------------------------------------- corner cases
    def test_75_fully_returned_receipt_cannot_be_opened(self):
        po = self._create_po([(self.bolt, 10, 20)])
        receipt = self._receive(po, {self.bolt: 10})
        wizard = self._open_wizard(po)  # from the PO
        self._wline(wizard, self.bolt).write({"action": "return", "return_qty": 10})
        self._confirm(wizard)
        with self.assertRaises(UserError):
            receipt.action_open_purchase_return_wizard()

    def test_76_stale_receipt_wizard_is_revalidated(self):
        po = self._create_po([(self.bolt, 10, 20)])
        receipt = self._receive(po, {self.bolt: 10})
        from_receipt = self._open_from_picking(receipt)
        self._wline(from_receipt, self.bolt).write({"action": "return", "return_qty": 6})
        from_receipt.action_review()

        from_po = self._open_wizard(po)
        self._wline(from_po, self.bolt).write({"action": "return", "return_qty": 5})
        self._confirm(from_po)

        with self.assertRaises(ValidationError):
            from_receipt.action_confirm()
        self.assertEqual(self._pol(po, self.bolt).qty_received, 5)

    def test_77_receipt_mixing_two_purchase_orders_is_refused(self):
        po1 = self._create_po([(self.bolt, 5, 20)])
        po2 = self._create_po([(self.panel, 3, 50)])
        receipt = po1.picking_ids
        po2.picking_ids.move_ids.write({"picking_id": receipt.id})
        for move in receipt.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        receipt._action_done()
        self.assertEqual(receipt.state, "done")
        with self.assertRaises(UserError):
            receipt.action_open_purchase_return_wizard()

    def test_78_requires_group(self):
        po = self._create_po([(self.bolt, 10, 20)])
        receipt = self._receive(po, {self.bolt: 10})
        group = self.env.ref("purchase_return_wizard.group_purchase_return")
        user = self.env["res.users"].create({
            "name": "Stock only", "login": "prw_stock_only",
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, self.env.company.ids)],
            "groups_id": [(6, 0, [self.env.ref("stock.group_stock_user").id])],
        })
        self.assertNotIn(group, user.groups_id)
        with self.assertRaises(AccessError):
            receipt.with_user(user).action_open_purchase_return_wizard()
