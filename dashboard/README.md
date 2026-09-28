# Local dashboard output

This directory is for generated, self-contained PAPER-ONLY reports. It has no
runtime server, package manager, network dependency, or broker connection.

Use `hedge.dashboard.write_dashboard("dashboard/hedge-report.html", report)`
after building a report with `hedge.flows.build_report`. Open the resulting
HTML file locally in a browser.
