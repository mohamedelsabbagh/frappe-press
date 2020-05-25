# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals
from frappe.core.doctype.file.file import create_new_folder


def execute():
	create_new_folder("Backup Uploads", "Home")
