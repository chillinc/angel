
# Defines constants -- values that MUST NOT be overriden or modified by any code, and that aren't variable in any way.
# As a silly example, DAYS_IN_WEEK = 7 would always be defined here, but START_DAY_OF_WEEK is a variable (0 or 1) and thus would be defined in defaults.py.

# To use, just do:
#   import angel.settings
#   angel.constants.STATE_RUNNING_OK


# Consts for locks:
SERVICE_LOCKNAME = 'angel-service'
LOCKFILE_DATA_DAEMON_START_TIME = 'daemon_start_time'
LOCKFILE_DATA_CHILD_START_TIME = 'child_start_time'
LOCKFILE_DATA_CHILD_PID = 'child_pid'
LOCKFILE_DATA_PRIOR_CHILD_START_TIME = 'prior_child_start_time'
LOCKFILE_DATA_START_COUNT = 'start_count'
LOCKFILE_DATA_STATE_MESSAGE = 'status_message'


# Consts for system status, used by monitoring:
# Note: Ok, warn, error, and unknown values must line up with nagios values; do not change these value.
STATE_RUNNING_OK = 0       # System is running, all services are healthy
STATE_WARN = 1             # System is running, all services are responding, but at least one service will go to error unless operator corrects an issue
STATE_ERROR = 2            # System is supposed to be running, but at least one service is failing to respond -- this means the site is down or partially down
STATE_UNKNOWN = 3          # Unable to determine state of system
STATE_STARTING = 4         # System is in process of starting up; at least one service is not responding to requests but should be up soon (Note: for nagios, STATE_WARN is returned)
STATE_STOPPING = 5         # System is in process of stopping; services are not expected to respond to requests (Note: for nagios, STATE_WARN is returned)
STATE_STOPPED = 6          # System is in stopped state (Note: for nagios, STATE_WARN is returned)
STATE_DECOMMISSIONED = 7   # System decommission has been called (Nagios returns STATE_ERROR)


STATE_CODE_TO_TEXT = {}
STATE_CODE_TO_TEXT[STATE_RUNNING_OK] = 'OK'
STATE_CODE_TO_TEXT[STATE_WARN] = 'WARN'
STATE_CODE_TO_TEXT[STATE_ERROR] = 'ERROR'
STATE_CODE_TO_TEXT[STATE_UNKNOWN] = 'UNKNOWN'
STATE_CODE_TO_TEXT[STATE_STARTING] = 'STARTING'
STATE_CODE_TO_TEXT[STATE_STOPPING] = 'STOPPING'
STATE_CODE_TO_TEXT[STATE_STOPPED] = 'STOPPED'
STATE_CODE_TO_TEXT[STATE_DECOMMISSIONED] = 'DECOMMISSIONED'



# Consts for stat types, used by nagios and collectd system monitoring:
# (Remember to update STAT_TYPES_COLLECTD/STAT_TYPES_NAGIOS below.)
STAT_TYPE_BYTES = 'bytes'
STAT_TYPE_COUNTER = 'counter'             # Counters only go up, never down -- things that go up and down are gauges.
STAT_TYPE_GAUGE = 'gauge'
STAT_TYPE_QUEUE_SIZE = 'queue_size'
STAT_TYPE_RECORDS = 'records'
STAT_TYPE_MEMORY = 'memory'
STAT_TYPE_SECONDS = 'seconds'

# The codes here are used by nagios / collectd for handling data types correctly; do not change the mappings.
# ( In some cases, our data is coming in from a nagios plugin, and the only unit info we have comes from the unit code that nagios gives us.
#   So that we map those unit types onto the most logical stat type, some nagios types have an extra '~' on them to prevent them from matching.
#   That is, both "bytes" and "memory" types would be 'b' in nagios, but mapping 'b' back to 'memory' would be wrong for disk usage.
#   To prevent this, we set the nagios code for memory to 'b~' here; and then when the nagios code is used elsewhere, the '~' is stripped out.  )

STAT_TYPES_COLLECTD = {}
STAT_TYPES_NAGIOS = {}

STAT_TYPES_NAGIOS[STAT_TYPE_BYTES] = 'b'
STAT_TYPES_COLLECTD[STAT_TYPE_BYTES] = 'bytes'

# This isn't quite right, but will work for now:
STAT_TYPES_NAGIOS['B'] = STAT_TYPES_NAGIOS[STAT_TYPE_BYTES]      # check_http uses capitol B; alias that here.
STAT_TYPES_COLLECTD['B'] = STAT_TYPES_COLLECTD[STAT_TYPE_BYTES]

STAT_TYPES_NAGIOS[STAT_TYPE_COUNTER] = ''
STAT_TYPES_COLLECTD[STAT_TYPE_COUNTER] = 'count'

STAT_TYPES_NAGIOS[STAT_TYPE_GAUGE] = ''
STAT_TYPES_COLLECTD[STAT_TYPE_GAUGE] = 'gauge'

STAT_TYPES_NAGIOS[STAT_TYPE_QUEUE_SIZE] = ''
STAT_TYPES_COLLECTD[STAT_TYPE_QUEUE_SIZE] = 'queue_length'
        
STAT_TYPES_NAGIOS[STAT_TYPE_RECORDS] = ''
STAT_TYPES_COLLECTD[STAT_TYPE_RECORDS] = 'records' 
        
STAT_TYPES_NAGIOS[STAT_TYPE_MEMORY] = 'b~' # See '~' note above.
STAT_TYPES_COLLECTD[STAT_TYPE_MEMORY] = 'memory'
        
STAT_TYPES_NAGIOS[STAT_TYPE_SECONDS] = 's'
STAT_TYPES_COLLECTD[STAT_TYPE_SECONDS] = 'seconds'

