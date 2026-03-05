import stardag as sd

# Use named profiles for different environments
sd.configure(profile="production")

# Or switch at runtime
with sd.profile("staging"):
    sd.build(my_task())

# Profiles are defined in ~/.stardag/config.toml
# [profiles.production]
# target_root = "s3://prod-bucket/stardag"
# [profiles.staging]
# target_root = "s3://staging-bucket/stardag"
