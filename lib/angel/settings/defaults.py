
# Default required settings for angel.
# You should override these as needed in your application-level settings.


# Should versioned installs auto-upgrade themselves?
SYSTEM_AUTO_UPGRADE = True


# How many versions do we keep, across all branches?
SYSTEM_INSTALLED_VERSIONS_TO_KEEP = 10


# We determine which services to call reload on during upgrades by first looking for a boolean "xxx_SERVICE_RELOAD_ON_UPGRADE".
# If that's not defined, we look at DEFAULT_SERVICE_RELOAD_ON_UPGRADE.
# This allows us to pin a running service to a particular version by doing something like:
#     project-foo service conf set APACHE2_SERVICE_RELOAD_ON_UPGRADE False
#     project-foo --use-version X tool apache2 restart
DEFAULT_SERVICE_RELOAD_ON_UPGRADE = True


# Is it safe to allow data to be reset?
SYSTEM_RESET_DATA_ALLOWED = True



# User and group that services are run as (set to None for current user):
RUN_AS_USER = None
RUN_AS_GROUP = None


# Directory that all logs are written to:
LOG_DIR = "~/.angel-override-me/log"

# Directory for storing all application data (anything that needs backing up):
DATA_DIR = "~/.angel-override-me/data"

# Directory for lockfiles, for pidfile and lockfile data:
LOCK_DIR = "~/.angel-override-me/lock"

# Directory for runtime data files, where we write runtime data (e.g., temp conf files):
# (RUN_DIR may be cleared between runs; do NOT store any files inside here that are needed between runs)
RUN_DIR = "~/.angel-override-me/run"

# Path to cache dir, where we write cacheable data (e.g. redis swap file):
# (CACHE_DIR data must not need backups; its purpose is for keeping data between restarts that can speed things up)
CACHE_DIR = "~/.angel-override-me/cache"

# Path to tmp dir, where processes should write temporary data (they they should then clean up):
# (TMP_DIR is for small, short-lived files; it may be reset between restarts)
TMP_DIR = "~/.angel-override-me/tmp"


# Additional paths that should be included in env.PATH when execing commands:
BIN_PATHS="./bin:./code/bin"
