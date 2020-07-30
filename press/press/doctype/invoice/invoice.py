# -*- coding: utf-8 -*-
# Copyright (c) 2020, Frappe and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
from frappe.model.document import Document
from press.api.billing import get_stripe
from press.utils import log_error
from datetime import datetime
from calendar import monthrange
from press.press.doctype.team.team_invoice import TeamInvoice
from frappe import _
from frappe.utils import getdate


class Invoice(Document):
	def validate(self):
		self.validate_team()
		self.validate_dates()
		self.validate_duplicate()
		self.validate_items()
		self.validate_amount()

	def before_submit(self):
		try:
			self.create_stripe_invoice()
		except Exception:
			frappe.db.rollback()

			msg = "<pre><code>" + frappe.get_traceback() + "</pre></code>"
			self.add_comment("Comment", _("Action Failed") + "<br><br>" + msg)
			if self.stripe_invoice_id:
				self.db_set("stripe_invoice_id", self.stripe_invoice_id)
			frappe.db.commit()

			raise

	def create_stripe_invoice(self):
		stripe = get_stripe()
		customer_id = frappe.db.get_value("Team", self.team, "stripe_customer_id")

		invoice = None
		if not self.stripe_invoice_id:
			start = getdate(self.period_start)
			end = getdate(self.period_end).strftime("%b %d")
			period_string = f"{start.strftime('%b %d')} - {end.strftime('%b %d')} {end.year}"
			stripe.InvoiceItem.create(
				customer=customer_id,
				description=f"Frappe Cloud Subscription ({period_string})",
				amount=int(self.total * 100),
				currency=self.currency.lower(),
			)
			invoice = stripe.Invoice.create(
				customer=customer_id,
				collection_method="charge_automatically",
				auto_advance=True,
				idempotency_key=self.name,
			)
			self.stripe_invoice_id = invoice["id"]

		if not invoice:
			invoice = stripe.Invoice.retrieve(self.stripe_invoice_id)

		if invoice["status"] == "draft":
			invoice = stripe.Invoice.finalize_invoice(
				self.stripe_invoice_id, idempotency_key=self.name
			)

		self.starting_balance = invoice["starting_balance"] / 100
		self.ending_balance = (invoice["ending_balance"] or 0) / 100
		self.amount_due = invoice["amount_due"] / 100
		self.amount_paid = invoice["amount_paid"] / 100
		self.stripe_invoice_url = invoice["hosted_invoice_url"]
		if self.amount_due == 0:
			self.status = "Paid"
		else:
			self.status = "Unpaid"

	def validate_duplicate(self):
		if self.is_new():
			res = frappe.db.get_all(
				"Invoice",
				filters={
					"period_start": self.period_start,
					"period_end": self.period_end,
					"team": self.team,
				},
			)
			if res:
				frappe.throw(
					f"Duplicate Entry {res[0].name} already exists", frappe.DuplicateEntryError
				)

	def validate_team(self):
		self.customer_name = frappe.utils.get_fullname(self.team)
		self.customer_email = self.team
		self.currency = frappe.db.get_value("Team", self.team, "currency")
		if not self.currency:
			frappe.throw(
				f"Cannot create Invoice because Currency is not set in Team {self.team}"
			)

	def validate_dates(self):
		if not self.period_start:
			d = datetime.now()
			# period starts on 1st of the month
			self.period_start = d.replace(day=1, month=self.month, year=self.year)

		period_start = frappe.utils.getdate(self.period_start)

		if not self.period_end:
			# period ends on last day of month
			_, days_in_month = monthrange(period_start.year, period_start.month)
			self.period_end = period_start.replace(day=days_in_month)

		# due date
		self.due_date = self.period_end

	def validate_items(self):
		self.items = []
		self.site_usage = [row for row in self.site_usage if row.days_active > 0]
		for row in self.site_usage:
			plan = frappe.get_cached_doc("Plan", row.plan)
			price_per_day = plan.get_price_per_day(self.currency)

			self.append(
				"items",
				{
					"quantity": row.days_active,
					"rate": price_per_day,
					"amount": row.days_active * price_per_day,
					"description": (
						f"{row.site} active for {row.days_active} days on {plan.plan_title} Plan"
					),
				},
			)

	def validate_amount(self):
		total = 0
		for item in self.items:
			total += item.amount
		self.total = total

	def on_cancel(self):
		self.unlink_with_ledger_entries()

	def on_trash(self):
		self.unlink_with_ledger_entries()

	def unlink_with_ledger_entries(self):
		values = {
			"modified": frappe.utils.now(),
			"modified_by": frappe.session.user,
			"invoice": self.name,
		}
		frappe.db.sql(
			"""
			UPDATE
				`tabPayment Ledger Entry`
			SET
				`invoice` = null,
				`modified` = %(modified)s,
				`modified_by` = %(modified_by)s
			WHERE
				`invoice` = %(invoice)s
			""",
			values=values,
		)


def submit_invoices():
	"""This method will run every day and submit the invoices whose period end was the previous day"""

	# get draft invoices whose period has ended before
	today = frappe.utils.today()
	invoices = frappe.db.get_all(
		"Invoice", {"status": "Draft", "period_end": ("<", today), "total": (">", 0)}
	)
	for d in invoices:
		invoice = frappe.get_doc("Invoice", d.name)
		try:
			invoice.submit()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			log_error("Invoice Submit Failed", invoice=d.name)

		try:
			# create invoice for next month
			d = datetime.now()
			d = d.replace(month=invoice.month, year=invoice.year)
			next_month = frappe.utils.add_months(d, 1)
			ti = TeamInvoice(invoice.team, next_month.month, next_month.year)
			if not ti.get_draft_invoice():
				ti.create()
		except Exception:
			frappe.db.rollback()
			log_error(
				"Invoice creation for next month failed",
				month=next_month.month,
				year=next_month.year,
			)


def process_stripe_webhook(doc, method):
	"""This method runs after a Stripe Webhook Log is created"""
	if doc.event_type not in ["invoice.payment_succeeded", "invoice.payment_failed"]:
		return

	event = frappe.parse_json(doc.payload)
	stripe_invoice = event["data"]["object"]
	invoice = frappe.get_doc("Invoice", {"stripe_invoice_id": stripe_invoice["id"]})
	team = frappe.get_doc("Team", invoice.team)

	if doc.event_type == "invoice.payment_succeeded":
		invoice.db_set(
			{
				"payment_date": datetime.fromtimestamp(
					stripe_invoice["status_transitions"]["paid_at"]
				),
				"docstatus": 1,
				"status": "Paid",
				"starting_balance": stripe_invoice["starting_balance"] / 100,
				"ending_balance": (stripe_invoice["ending_balance"] or 0) / 100,
				"amount_due": stripe_invoice["amount_due"] / 100,
				"amount_paid": stripe_invoice["amount_paid"] / 100,
				"stripe_invoice_url": stripe_invoice["hosted_invoice_url"],
			}
		)

		# unsuspend sites
		team.unsuspend_sites(
			reason=f"Unsuspending sites because of successful payment of {invoice.name}"
		)

	elif doc.event_type == "invoice.payment_failed":
		attempt_date = stripe_invoice.get("webhooks_delivered_at")
		if attempt_date:
			attempt_date = datetime.fromtimestamp(attempt_date)
		attempt_count = stripe_invoice.get("attempt_count")
		invoice.db_set(
			{
				"payment_attempt_count": attempt_count,
				"payment_attempt_date": attempt_date,
				"status": "Unpaid",
			}
		)

		if attempt_count > 1:
			# suspend sites
			sites = team.suspend_sites(
				reason=f"Suspending sites because of failed payment of {invoice.name}"
			)
			if sites:
				send_email_for_failed_payment(invoice, sites)


def send_email_for_failed_payment(invoice, sites=None):
	team = frappe.get_doc("Team", invoice.team)
	email = team.user
	payment_method = team.default_payment_method
	last_4 = frappe.db.get_value("Stripe Payment Method", payment_method, "last_4")
	account_update_link = frappe.utils.get_url("/dashboard/#/welcome")

	frappe.sendmail(
		recipients=email,
		subject="Payment Failed for Frappe Cloud Subscription",
		template="payment_failed",
		args={
			"payment_link": invoice.stripe_invoice_url,
			"amount": invoice.get_formatted("amount_due"),
			"account_update_link": account_update_link,
			"last_4": last_4 or "",
			"card_not_added": not payment_method,
			"sites": sites,
		},
	)
