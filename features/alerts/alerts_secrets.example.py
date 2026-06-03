# alerts_secrets.example.py
#
# Copy this file to alerts_secrets.py on noether (the Pi/host running the logger)
# and fill in your real credentials.
#
#   cp alerts_secrets.example.py alerts_secrets.py
#   nano alerts_secrets.py
#
# alerts_secrets.py is listed in .gitignore and will NEVER be committed to GitHub.
# This example file is committed only to show the required format.

EMAIL_SENDER     = 'your.sender@gmail.com'    # Gmail account that sends the alerts
EMAIL_PASSWORD   = 'xxxx xxxx xxxx xxxx'      # Gmail App Password (16 chars)
EMAIL_RECIPIENTS = ['your.name@yale.edu']     # List of addresses that receive alerts
