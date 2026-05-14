from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, validate_phone_number

from ecommerce_integrations.controllers.customer import EcommerceCustomer
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
)


class ShopifyCustomer(EcommerceCustomer):
	def __init__(self, customer_id: str):
		self.setting = frappe.get_doc(SETTING_DOCTYPE)
		super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("contact_email") or customer.get("email")

		customer_group = self.setting.customer_group
		super().sync_customer(customer_name, customer_group)

		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		preferred_email = customer.get("contact_email") or customer.get("email")

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=preferred_email
			)
		if shipping_address:
			self.create_customer_address(
				customer_name, shipping_address, address_type="Shipping", email=preferred_email
			)

		self.create_customer_contact(customer)

	def create_customer_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		"""Create customer address(es) using Customer dict provided by shopify."""
		address_fields = _map_address_fields(shopify_address, customer_name, address_type, email)
		super().create_customer_address(address_fields)

	def update_existing_addresses(self, customer):
		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		email = customer.get("contact_email") or customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		old_address = self.get_customer_address_doc(address_type)

		if not old_address:
			self.create_customer_address(customer_name, shopify_address, address_type, email)
		else:
			exclude_in_update = ["address_title", "address_type"]
			new_values = _map_address_fields(shopify_address, customer_name, address_type, email)
			update_values = {k: v for k, v in new_values.items() if k not in exclude_in_update}

			# Avoid a write when there is no actual change; this reduces timestamp conflicts.
			changed = any((old_address.get(k) or "") != (v or "") for k, v in update_values.items())
			if not changed:
				return

			old_address.update(update_values)
			old_address.flags.ignore_mandatory = True
			try:
				old_address.save(ignore_permissions=True)
			except frappe.TimestampMismatchError:
				# Another worker updated the same address between read and save.
				# Reload and apply the latest Shopify values without creating a hard failure.
				old_address.reload()
				old_address.update(update_values)
				old_address.save(ignore_permissions=True, ignore_version=True)

	def create_customer_contact(self, shopify_customer: dict[str, Any]) -> None:
		preferred_email = shopify_customer.get("contact_email") or shopify_customer.get("email")

		if not (shopify_customer.get("first_name") and preferred_email):
			return

		contact_fields = {
			"status": "Passive",
			"first_name": shopify_customer.get("first_name"),
			"last_name": shopify_customer.get("last_name"),
			"unsubscribed": not shopify_customer.get("accepts_marketing"),
		}

		if preferred_email:
			contact_fields["email_ids"] = [{"email_id": preferred_email, "is_primary": True}]

		phone_no = shopify_customer.get("phone") or shopify_customer.get("default_address", {}).get("phone")

		if validate_phone_number(phone_no, throw=False):
			contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

		super().create_customer_contact(contact_fields)


def _map_address_fields(shopify_address, customer_name, address_type, email):
	"""returns dict with shopify address fields mapped to equivalent ERPNext fields"""
	address_fields = {
		"address_title": customer_name,
		"address_type": address_type,
		ADDRESS_ID_FIELD: shopify_address.get("id"),
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	return address_fields