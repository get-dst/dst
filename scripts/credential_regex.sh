# Credential shapes, in ONE place. Sourced by the pre-commit hook (.githooks/
# pre-commit) and any export-time gate, so the commit check and the export check
# use the same needles and cannot drift apart.
#
# dstadm_/dst_/dsto_/dstsess_ are dst's own prefixes: secrets.token_urlsafe(32)
# yields 40+ chars of base64url, so {30,} is comfortably below a real token and
# above any placeholder we write by hand (dst_YOUR_KEY, dst_x, dstadm_9aBc...).
#
# The leading (^|[^A-Za-z0-9_]) matters because `dst_` is short and generic: without
# it, an ordinary Python name like
# `test_dst_survives_a_round_trip_through_the_open_format` is a 30+ char match and
# the hook blocks a commit that contains no credential at all. A real token always
# starts one — after a space, quote, `=`, `:` or line start — never mid-identifier.
CREDS='(^|[^A-Za-z0-9_])(dstadm_|dst_|dsto_|dstsess_)[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|postgres(ql)?://[^/:@ ]+:[^/@ ]+@'

# The shipped dev defaults — migration 0001's role password and the compose
# superuser, both printed in .env.example, docker-compose.yml and config.py.
# They are documented constants, not secrets, and they appear in docs and fleet
# harness code by design. Allowing them EXACTLY (never a prefix match, never a
# whole line) keeps the DSN rule strict for real passwords while stopping the
# gate from crying wolf on its own defaults — the hook's comment says it
# already: a gate routinely bypassed is worse than none.
CREDS_ALLOW='^postgres(ql)?://dst:dst_dev@$|^postgres(ql)?://dst_app:dst_app_dev@$'
