# Stardag reads configuration from ~/.stardag/config.toml
#
# [target.roots]
# default = "s3://prod-bucket/stardag"
#
# Or use per-environment overrides via env vars:
#
# STARDAG_TARGET_ROOTS__DEFAULT=s3://staging-bucket/stardag
#
# Example config with multiple target roots:
#
# [target.roots]
# default = "./local-output"
# s3 = "s3://my-bucket/stardag"
#
# The "default" root is used unless a task specifies otherwise.
# Target roots determine where task outputs are persisted.
