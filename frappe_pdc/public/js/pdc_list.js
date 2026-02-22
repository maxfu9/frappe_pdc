frappe.listview_settings['PDC'] = {
	add_fields: ['pdc_status'],
	get_indicator: function (doc) {
		const status_colors = {
			"Pending": "orange",
			"Ready for Clearance": "yellow",
			"Approved for Clearance": "blue",
			"Handed Over": "purple",
			"Partially Cleared": "cyan",
			"Partially Bounced": "orange",
			"Cleared": "green",
			"Bounced": "red"
		};
		const status = doc.pdc_status || "Pending";
		const color = status_colors[status] || "grey";
		return [__(status), color, "pdc_status,=," + status];
	}
};
