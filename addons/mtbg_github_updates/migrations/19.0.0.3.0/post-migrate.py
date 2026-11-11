"""0.3.0: a snapshot's kind is the name of its fading rule (it was a fixed code)."""

NAMES = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "bimonthly": "Every 60 days",
         "halfyear": "Every 180 days", "yearly": "Yearly (kept forever)"}


def migrate(cr, version):
    for code, name in NAMES.items():
        cr.execute("UPDATE mtbg_offsite_backup SET tier = %s WHERE tier = %s", [name, code])
