# ============ Telegram API credentials (telegram_archiver) ============
# telegram_archiver.py reads API_ID and API_HASH from this file.
#
# How to use it:
#   1) copy this file:   cp config.example.py config.py
#   2) go to https://my.telegram.org  and log in with your phone number
#   3) open "API development tools" and create an app (any name is fine)
#   4) paste the two values into config.py
#
# KEEP config.py PRIVATE: never share it, upload it, or commit it to git.
# It must stay in the SAME folder as telegram_archiver.py.
#
# Alternative: export TG_API_ID / TG_API_HASH as environment variables.

API_ID = 0            # e.g.  API_ID = 123456
API_HASH = ""         # e.g.  API_HASH = "abcdef0123456789abcdef0123456789"
