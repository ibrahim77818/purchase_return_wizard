# -*- coding: utf-8 -*-
"""Run with:
    odoo-bin -d <db> -i purchase_return_wizard --test-tags /purchase_return_wizard --stop-after-init
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.purchase_return_wizard.wizard.purchase_return_wizard import (
    compute_credit_qty,
)

from .common import PurchaseReturnCommon


@tagged("post_install", "-at_install")
class TestPurchaseReturnWizard(PurchaseReturnCommon):

    # ------------------------------------------------------- pure routing rule
    def test_00_credit_formula(self):
        self.assertEqual(compute_credit_qty(4, 10, 10, 0.01), 4)
        self.assertEqual(compute_credit_qty(4, 10, 6, 0.01), 0)
        self.assertEqual(compute_credit_qty(4, 10, 8, 0.01), 2)
        self.assertEqual(compute_credit_qty(0, 10, 10, 0.01), 0)

    # ------------------------------------------------------------- constraints
    def test_10_cannot_exceed_received(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 5})
        line = self._wline(self._open_wizard(po), self.bolt)
        self.assertEqual(line.qty_received, 5)
        self.assertEqual(line.qty_returnable, 5)
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 6})

    def test_11_cannot_exceed_ordered_on_over_receipt(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 12})
        line = self._wline(self._open_wizard(po), self.bolt)
        self.assertEqual(line.qty_received, 12)
        self.assertEqual(line.qty_returnable, 10)
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 11})

    def test_12_disposition_consistency(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 10})
        line = self._wline(self._open_wizard(po), self.bolt)
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": -1})
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 0})
        with self.assertRaises(ValidationError):
            line.write({"action": "keep", "return_qty": 2})

    def test_13_not_received_line_not_returnable(self):
        po = self._create_po([(self.bolt, 10, 20), (self.panel, 5, 50)])
        self._receive(po, {self.bolt: 10})
        line = self._wline(self._open_wizard(po), self.panel)
        self.assertEqual(line.scenario, "not_received")
        self.assertEqual(line.qty_returnable, 0)
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 1})

    def test_14_nothing_selected(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 10})
        with self.assertRaises(UserError):
            self._open_wizard(po).action_review()

    def test_15_review_step_required_and_back(self):
        po = self._create_po([(self.beam, 10, 100)])
        self._receive(po, {self.beam: 10})
        self._bill(po)
        wizard = self._open_wizard(po)
        self._wline(wizard, self.beam).write({"action": "return", "return_qty": 4})
        with self.assertRaises(UserError):
            wizard.action_confirm()  # cannot skip the review step
        wizard.action_review()
        self.assertEqual(wizard.review_routing, "return_credit")
        self.assertEqual(wizard.review_return_qty, 4)
        self.assertAlmostEqual(wizard.review_credit_amount, 400)
        self.assertEqual(wizard.selected_line_ids.product_id, self.beam)
        wizard.action_back()
        self.assertEqual(wizard.state, "select")
        self.assertFalse(po.return_log_ids)

    def test_16_done_action_links(self):
        po = self._create_po([(self.beam, 10, 100)])
        self._receive(po, {self.beam: 10})
        self._bill(po)
        wizard = self._open_wizard(po)
        self._wline(wizard, self.beam).write({"action": "return", "return_qty": 1})
        wizard.action_review()
        params = wizard.action_confirm()["params"]
        # log + 1 transfer + credit note, one "%s" placeholder per link
        self.assertEqual(len(params["links"]), 3)
        self.assertEqual(params["message"].count("%s"), 3)

    # --------------------------------------------- scenario A: received, unbilled
    def test_20_received_not_billed_creates_validated_return_only(self):
        po = self._create_po([(self.beam, 10, 100), (self.bolt, 10, 20)])
        receipt = self._receive(po, {self.beam: 10, self.bolt: 10})
        wizard = self._open_wizard(po)
        self.assertFalse(wizard.has_draft_bills)
        self.assertEqual(self._wline(wizard, self.beam).scenario, "received_not_billed")
        self._wline(wizard, self.beam).write({"action": "return", "return_qty": 3})

        log = self._confirm(wizard)

        self.assertEqual(log.routing, "return_only")
        self.assertFalse(log.refund_id)
        self.assertFalse(po.invoice_ids)
        ret = log.return_picking_ids
        self.assertEqual(len(ret), 1)
        self.assertEqual(ret.state, "done")
        self.assertEqual(ret.location_dest_id.usage, "supplier")
        self.assertEqual(ret.move_ids.origin_returned_move_id.picking_id, receipt)
        self.assertEqual(self._returned_qty(ret, self.beam), 3)
        self.assertFalse(ret.move_ids.filtered(lambda m: m.product_id == self.bolt))
        self.assertEqual(self._pol(po, self.beam).qty_received, 7)
        self.assertEqual(self._pol(po, self.bolt).qty_received, 10)
        self.assertEqual(po.return_log_count, 1)
        self.assertRegex(log.name, r"^PRET/")

    def test_21_draft_bill_is_flagged_not_credited(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 10})
        po.action_create_invoice()  # left in draft
        wizard = self._open_wizard(po)
        self.assertTrue(wizard.has_draft_bills)
        line = self._wline(wizard, self.bolt)
        self.assertEqual(line.qty_billed, 0)
        line.write({"action": "return", "return_qty": 2})
        log = self._confirm(wizard)
        self.assertEqual(log.routing, "return_only")

    # ------------------------------------------------ scenario B: received, billed
    def test_30_received_and_billed_creates_return_and_draft_refund(self):
        po = self._create_po([(self.beam, 10, 100)])
        self._receive(po, {self.beam: 10})
        self._bill(po)
        wizard = self._open_wizard(po)
        line = self._wline(wizard, self.beam)
        self.assertEqual(line.scenario, "billed")
        line.write({"action": "return", "return_qty": 4})
        self.assertEqual(line.credit_qty, 4)
        self.assertEqual(line.credit_amount, 400)

        log = self._confirm(wizard)

        self.assertEqual(log.routing, "return_credit")
        self.assertEqual(log.return_picking_ids.state, "done")
        self.assertEqual(self._returned_qty(log.return_picking_ids, self.beam), 4)
        refund = log.refund_id
        self.assertEqual(refund.move_type, "in_refund")
        self.assertEqual(refund.state, "draft")
        self.assertEqual(refund.partner_id, self.vendor)
        self.assertIn(log.name, refund.ref)
        self.assertEqual(len(refund.invoice_line_ids), 1)
        rline = refund.invoice_line_ids
        self.assertEqual(rline.quantity, 4)
        self.assertEqual(rline.price_unit, 100)
        self.assertEqual(rline.tax_ids, self.purchase_tax)
        self.assertEqual(rline.purchase_line_id, self._pol(po, self.beam))
        self.assertAlmostEqual(refund.amount_untaxed, 400)
        self.assertIn(refund, po.invoice_ids)
        self.assertAlmostEqual(log.amount_credited, 400)
        pol = self._pol(po, self.beam)
        self.assertEqual(pol.qty_received, 6)
        self.assertEqual(pol.qty_invoiced, 6)  # 10 billed - 4 (draft) refund

    # -------------------------------------------- mixed partial / full returns
    def test_40_mixed_partial_full_routing(self):
        po = self._create_po([(self.beam, 10, 100), (self.bolt, 10, 20), (self.panel, 10, 50)])
        self._receive(po, {self.beam: 10, self.bolt: 10, self.panel: 10})
        # beam fully billed, bolt not billed, panel billed 6/10
        self._bill(po, qty_by_product={self.panel: 6}, delete_products=self.bolt)

        wizard = self._open_wizard(po)
        self.assertEqual(self._wline(wizard, self.beam).scenario, "billed")
        self.assertEqual(self._wline(wizard, self.bolt).scenario, "received_not_billed")
        self.assertEqual(self._wline(wizard, self.panel).scenario, "partially_billed")
        self._wline(wizard, self.beam).write({"action": "return", "return_qty": 10})   # full
        self._wline(wizard, self.bolt).write({"action": "return", "return_qty": 2})    # partial
        self._wline(wizard, self.panel).write({"action": "return", "return_qty": 7})   # partial

        log = self._confirm(wizard)

        ret = log.return_picking_ids
        self.assertEqual(len(ret), 1)  # single receipt -> single return
        self.assertEqual(self._returned_qty(ret, self.beam), 10)
        self.assertEqual(self._returned_qty(ret, self.bolt), 2)
        self.assertEqual(self._returned_qty(ret, self.panel), 7)

        refund_lines = log.refund_id.invoice_line_ids
        self.assertEqual(refund_lines.product_id, self.beam | self.panel)
        self.assertEqual(refund_lines.filtered(lambda l: l.product_id == self.beam).quantity, 10)
        # panel: received 10, billed 6, return 7 -> only 6 - 3 = 3 were billed
        self.assertEqual(refund_lines.filtered(lambda l: l.product_id == self.panel).quantity, 3)
        self.assertAlmostEqual(log.refund_id.amount_untaxed, 10 * 100 + 3 * 50)

        self.assertEqual(self._pol(po, self.beam).qty_received, 0)
        self.assertEqual(self._pol(po, self.bolt).qty_received, 8)
        self.assertEqual(self._pol(po, self.panel).qty_received, 3)
        self.assertEqual(self._pol(po, self.panel).qty_invoiced, 3)

    def test_41_multiple_receipts_lifo_allocation(self):
        po = self._create_po([(self.bolt, 10, 20)])
        first = self._receive(po, {self.bolt: 4})
        second = self._receive(po, {self.bolt: 6})  # the backorder
        wizard = self._open_wizard(po)
        self._wline(wizard, self.bolt).write({"action": "return", "return_qty": 8})

        log = self._confirm(wizard)

        returns = log.return_picking_ids
        self.assertEqual(len(returns), 2)
        by_origin = {r.move_ids.origin_returned_move_id.picking_id: r for r in returns}
        self.assertEqual(self._returned_qty(by_origin[second], self.bolt), 6)
        self.assertEqual(self._returned_qty(by_origin[first], self.bolt), 2)
        self.assertEqual(self._pol(po, self.bolt).qty_received, 2)

    def test_42_sequential_returns_never_double_credit(self):
        po = self._create_po([(self.beam, 10, 100)])
        self._receive(po, {self.beam: 10})
        self._bill(po)

        wiz1 = self._open_wizard(po)
        self._wline(wiz1, self.beam).write({"action": "return", "return_qty": 3})
        log1 = self._confirm(wiz1)
        self.assertEqual(log1.refund_id.invoice_line_ids.quantity, 3)

        wiz2 = self._open_wizard(po)
        line = self._wline(wiz2, self.beam)
        self.assertEqual(line.qty_received, 7)
        self.assertEqual(line.qty_billed, 7)  # draft refund already deducted
        with self.assertRaises(ValidationError):
            line.write({"action": "return", "return_qty": 8})
        line.write({"action": "return", "return_qty": 2})
        log2 = self._confirm(wiz2)
        self.assertEqual(log2.refund_id.invoice_line_ids.quantity, 2)
        self.assertNotEqual(log1.refund_id, log2.refund_id)

    def test_43_stale_wizard_is_revalidated_on_confirm(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 10})
        wiz1 = self._open_wizard(po)
        wiz2 = self._open_wizard(po)
        self._wline(wiz1, self.bolt).write({"action": "return", "return_qty": 8})
        self._wline(wiz2, self.bolt).write({"action": "return", "return_qty": 5})
        wiz2.action_review()  # reviewed on the old snapshot
        self._confirm(wiz1)
        with self.assertRaises(ValidationError):
            wiz2.action_confirm()
        self.assertEqual(self._pol(po, self.bolt).qty_received, 2)

    # ------------------------------------------------------- filters & bulk ops
    def test_50_filters_and_bulk_actions(self):
        po = self._create_po([(self.beam, 10, 100), (self.bolt, 10, 20), (self.panel, 10, 50)])
        self._receive(po, {self.beam: 10, self.bolt: 3, self.panel: 8})
        wizard = self._open_wizard(po)
        self.assertEqual(wizard.visible_line_ids, wizard.line_ids)

        wizard.filter_text = "bolt"  # matches product name, case-insensitive
        wizard.action_apply_filter()
        self.assertEqual(wizard.visible_line_ids.product_id, self.bolt)

        wizard.filter_text = "PRW-PANEL"  # SKU
        wizard.action_apply_filter()
        self.assertEqual(wizard.visible_line_ids.product_id, self.panel)

        wizard.write({"filter_text": False, "filter_qty_field": "qty_received", "filter_qty_min": 5})
        wizard.action_apply_filter()
        self.assertEqual(wizard.visible_line_ids.product_id, self.beam | self.panel)

        wizard.write({"filter_qty_max": 9})
        wizard.action_apply_filter()
        self.assertEqual(wizard.visible_line_ids.product_id, self.panel)

        wizard.action_return_all_visible()
        self.assertEqual(self._wline(wizard, self.panel).return_qty, 8)
        self.assertEqual(self._wline(wizard, self.beam).action, "keep")

        wizard.action_reset_filter()
        self.assertEqual(wizard.visible_line_ids, wizard.line_ids)
        wizard.filter_selected_only = True
        wizard.action_apply_filter()
        self.assertEqual(wizard.visible_line_ids.product_id, self.panel)

        wizard.action_keep_all_visible()
        self.assertEqual(self._wline(wizard, self.panel).action, "keep")

    def test_60_log_is_immutable(self):
        po = self._create_po([(self.bolt, 10, 20)])
        self._receive(po, {self.bolt: 10})
        wizard = self._open_wizard(po)
        self._wline(wizard, self.bolt).write({"action": "return", "return_qty": 1})
        log = self._confirm(wizard)
        with self.assertRaises(UserError):
            log.write({"note": "tampered"})
        with self.assertRaises(UserError):
            log.unlink()
