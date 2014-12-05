
import os
import re
import sys
import time
from devops.generic_service import GenericService
from angel.util.pidfile import is_pid_in_pidfile_running
from angel.util.checksum import get_md5_of_path_contents
from devops.simple_cache import simple_cache_get, simple_cache_set
from angel.stats.disk_stats import disk_stats_get_usage, disk_stats_get_usage_for_path
import angel.settings


class DevopsService(GenericService):
    '''
    Manages OS and deploy-level logic; provides some tools for devops use.
    '''

    HIDDEN_TOOLS = ('update-system-config',)

    # This is a devops service that configs OS-level system stuff one-time on install and reports OS-level stats into our status update.
    # We do this so that OS-level stats can flow back to our service monitoring system in-line with other metrics.
    # It also allows us to report errors on things like free disk space to prevent upgrades from causing system failure.

    def trigger_start(self):
        # This is a gross place to squeeze this in, but it'll work for now:
        if os.path.exists('/sbin/blockdev') and os.path.exists('/dev/md0'):
            (out, err, exitcode) = self.getCommandOutput('/sbin/blockdev --report /dev/md0')
            if exitcode == 0 and out and out.find('rw  4096') > 0:
                print >>sys.stderr, "Setting readahead of /dev/md0 to 128 sectors"
                self.runCommand('/sbin/blockdev --setra 128 /dev/md0')

        # Override parent class, to avoid spurious "no lockfile" warning:
        return 0


    def service_start(self):
        return 0 # Someday: self.start_supervisor_with_function('devops-manager', self.runDevopsManager)


    def stop(self):
        return 0 # Override default logic that tries to stop based on _supervisor_pidfile


    def decommission_precheck(self):
        return True


    def decommission(self):
        print >>sys.stderr, "Warning: devops.decommission() isn't waiting for logfiles to drain yet."
        allowed_drain_time = 600
        while self._getPostfixQueueSize() > 0:
            time.sleep(10)
            allowed_drain_time -= 10
            if allowed_drain_time < 0:
                print >>sys.stderr, "Postfix queue has messages"
                return 1
        return self.trigger_stop()


    # We override trigger_status instead of service_status to completely bypass the supervisor status checks:
    def trigger_status(self):
        return self._getStatus()


    def shell_tool_linux_nettop(self):
        '''Run iftraf
        '''
        iftraf = self.which('iftraf')
        if iftraf is None:
            print >>sys.stderr, "Error: can't find iftraf (are you on linux?)"
            return 1
        print >>sys.stderr, "You will need to add filters:\n" +\
                            "              0.0.0.0/0:0                      --> 96.246.115.240/16:0                   E\n" +\
                            "              10.0.0.0/8:0                     --> 10.0.0.0/8:0                          E\n" +\
                            "              0.0.0.0/0:0                      --> 0.0.0.0/0:0                           I\n"
        time.sleep(2)
        return self.execCommand(iftraf)


    def shell_tool_update_system_config(self):
        ''' Called by devops system logic after a first-time install or a version switch. '''

        # There are a number of services which we don't have service objects for, because they're not part of the devops package (i.e. ubuntu-specific).
        # That is, basic Linux tweaks that we do across the board and that don't have any variable settings.

        # This logic could be handled by chef or puppet, but we already have a good distribution mechanism, so easy enough to piggy-back on top of it.

        # We used to store this logic in debian/postinst, but this doesn't work when we can switch versions with our multi-version setup. That is, we might upgrade 
        # system level (Specifically, auto-download without auto-upgrade means that any conf changes to system-level conf files won't be picked up.) So, instead, we 
        # tuck the setup logic here, as part of the devops switch_version() lagic, call this.
        
        # Only run if we're root:
        if os.getuid() != 0:
            print >>sys.stderr, "Error: update-system-config needs to be run as root."
            return 1

        # Only run if we're on linux (cheap and fast way to tell; admittedly not portable):
        if not sys.platform.startswith('linux'):
            print >>sys.stderr, "Error: update-system-config only runs on Linux."
            return 1 

        # Only run if we're an actual install (i.e. not a git-checkout run):
        if not self._angel.is_versioned_install():
            print >>sys.stderr, "Error: not a versioned install."
            return 1

        return self._configAll() # returns 0 on no errors; non-zero otherwise


    #--------  Config logic ---------------------------------------------------------------

    def _runCommand(self, cmd, silent=False):
        if not silent: print >>sys.stderr, "System service running: %s" % cmd
        return self.runCommand(cmd)


    def _configAll(self):

        # Config Linux OS-level components; returning the number of errors.

        # VERY important notes:
        #  1) All config operations must be idempotent and be safe to run whether we're running or stopped.
        #  2) All changes are outside package management, so uninstalling the package will NOT undo any
        #     modifications this makes.

        errors = 0
        errors += self._configBase()
        errors += self._configPostfix()
        errors += self._configNagios()
        errors += self._configCollectd()
        errors += self._configMonit()
        return errors


    def _configBase(self):

        # Install Linux-based /etc files that tweak the OS as appropriate for this version.
        # We copy conf files into various subdirs, instead of just running installConf() on the whole thing, 
        # so that we can check individual subsystems and restart just those we need to. It also helps prevent us from
        # doing really stupid things (like clobbering random files under /etc).

        # Kernel parameters:
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='sysctl.d', dest_path='/etc/sysctl.d/')
        if change_count > 0:
            # Make sure any kernel parameter changes are applied
            if 0 != self._runCommand('service procps start'):
                return 1

        # User limits (note these won't apply to any existing processes / shells):
        self.createConfDirFromTemplates(src_path='security/limits.d', dest_path='/etc/security/limits.d/')

        # Init.d script:
        self.createConfDirFromTemplates(src_path='init.d', dest_path='/etc/init.d/')
        if not os.path.exists('/etc/rc2.d/S20tappy'):
            if 0 != self._runCommand('update-rc.d -f tappy remove && update-rc.d tappy start 20 2 3 4 5 . stop 20 0 1 6 .'):
                return 1

        # Bash autocomplete:
        self.createConfDirFromTemplates(src_path='bash_completion.d', dest_path='/etc/bash_completion.d/')

        # Cron:
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='cron.d', dest_path='/etc/cron.d/')
        if change_count > 0:
            if 0 != self._runCommand('service cron restart'):
                return 1

        return 0


    def _configPostfix(self):
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='mail-aliases/aliases', dest_path='/etc/aliases')
        if change_count > 0:
            if 0 != self._runCommand('newaliases'):
                return 1

        # Check if the hostname has changed -- this can happen if auto-conf sets it after we run through this.
        # Failure to re-check this will cause postfix to queue up mail and not deliver it.
        hostname_has_changed = False
        out, err, exitcode = self.getCommandOutput("/bin/hostname -f")
        if 0 == exitcode and os.path.isfile('/etc/mailname'):
            try:
                current_mailname = open('/etc/mailname').read().rstrip()
                if out.rstrip() != current_mailname:
                    hostname_has_changed = True
            except:
                print >>sys.stderr, "Error: unable to check current postfix mailname (%s)." % e

        (dummy, change_count) = self.createConfDirFromTemplates(src_path='postfix', dest_path='/etc/postfix/')
        if change_count > 0 or hostname_has_changed:
            self._runCommand('hostname -f > /etc/mailname')
            self._runCommand('cd /etc/postfix && postmap /etc/postfix/relay_domains')
            self._runCommand('cd /etc/postfix && postmap /etc/postfix/transport')
            self._runCommand('cd /etc/postfix && postmap /etc/postfix/relay_passwords')
            self._runCommand('service postfix start && service postfix reload') # Start in case it's stopped; reload in case it's running
        return 0


    def _configNagios(self):
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='nagios', dest_path='/etc/nagios/')
        if change_count > 0:
            self._runCommand("perl -pi -e 's:^allowed_hosts:#allowed_hosts:gs' /etc/nagios/nrpe.cfg")
            self._runCommand('service nagios-nrpe-server restart')
        return 0


    def _configCollectd(self):
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='collectd', dest_path='/etc/collectd/')
        # Always restart collectd -- it dereferences the /usr/bin/chill symlink path, so upgrades require it to be kicked.
        # Plus, doing this means we won't hold an old version of the codebase open, avoiding the need for an extra prior version.
        self._runCommand('service collectd restart > /dev/null', silent=True)
        return 0


    def _configMonit(self):
        (dummy, change_count) = self.createConfDirFromTemplates(src_path='monit.d', dest_path='/etc/monit.d/')
        if change_count > 0:
            self._runCommand("perl -pi -e 's:\#\s+include /etc/monit\.d:include /etc/monit\.d:gs' /etc/monit/monitrc")
            self._runCommand("perl -pi -e 's:startup=0:startup=1:' /etc/default/monit")
            self._runCommand('sysv-rc-conf --level 2345 monit on') # Make sure monit starts on reboot:
            if os.path.exists('/var/run/monit.pid'):
                self._runCommand('service monit restart')
            else:
                self._runCommand('service monit start')
        return 0


    #--------  Devops Manager logic -------------------------------------------------------

    def runDevopsManager(self):
        ''' Process that manages devops-level checks while system is running. '''
        print >>sys.stderr, "runDevopsManager: test"
        while True:
            time.sleep(2)


    #--------  Status logic ---------------------------------------------------------------

    def _getStatus(self):
        # Note that this function is called even when services are stopped.
        if not os.path.isfile('/proc/loadavg'):
            return self.getStatStruct(message='Not supported on this platform', state=angel.constants.STATE_RUNNING_OK)
        # Return a status struct with all our system-level checks.
        stat_struct = self.getStatStruct()
        self._checkVersion(stat_struct)
        self._checkLoad(stat_struct)
        self._checkDisk(stat_struct)
        self._checkNetwork(stat_struct)
        self._checkPostfix(stat_struct)
        self._checkCron(stat_struct)
        return stat_struct


    def _checkVersion(self, stat_struct):
        # To-Do: pull latest version from shared storage (e.g. zookeeper) and compare if we're behind newest-known?
        try:
            self.addDataPointToStatStruct(stat_struct, 'build', self._angel.get_project_code_version())
            self.updateStatStruct(stat_struct, message='build %s' % self._angel.get_project_code_version(), state=angel.constants.STATE_RUNNING_OK)
        except Exception:
            self.updateStatStruct(stat_struct, message='unknown build number', state=angel.constants.STATE_WARN)


    def _checkLoad(self, stat_struct):        
        active_services = filter(lambda x: x, self._angel.get_enabled_or_running_service_objects())
        # Find the largest MONITORING_LOAD1_THRESHOLD in all active services that are not None on this node
        LOAD_1_WARN_THRESHOLD   = max( map(lambda x: x.MONITORING_LOAD1_THRESHOLD, active_services))
        LOAD_5_WARN_THRESHOLD   = max( map(lambda x: x.MONITORING_LOAD5_THRESHOLD, active_services))
        LOAD_15_WARN_THRESHOLD  = max( map(lambda x: x.MONITORING_LOAD15_THRESHOLD, active_services))
        SHORTTERM_SPIKE_GRACE   = max( map(lambda x: x.MONITORING_SHORTTERM_SPIKE_GRACE, active_services))
        SHORTTERM_SPIKE_TIME    = max( map(lambda x: x.MONITORING_SHORTTERM_SPIKE_TIME, active_services))


        # In general, we only want to flag error alerts when the system is hard-down, but a super high load suggests something is seriously borked, so we'll error on that as well:
        LOAD_1_ERROR_THRESHOLD  = 40
        LOAD_5_ERROR_THRESHOLD  = 40
        LOAD_15_ERROR_THRESHOLD = 40

        try:
            (load_1, load_5, load_15) = os.getloadavg()
        except:
            self.updateStatStruct(stat_struct, message="os.getloadavg failed", state=angel.constants.STATE_WARN)
            return

        # Determine what state we should be in -- we enter "warn" state when the load is higher than threshold+shorterm_spike_grace,
        # or if the load is higher than threshold for more than spike_time. This allows us to ignore things like a load5 value > 0.9
        # for only a few minutes, but still catch conditions where the load on a box goes high for longer than expected.
        # (We could probably skip this if "load60" existed...)
        state = angel.constants.STATE_RUNNING_OK

        latest_warn_start_time_cache_keyname = 'devops-checkload-latest-warn-start-time'
        latest_warn_start_time = simple_cache_get(latest_warn_start_time_cache_keyname)

        if load_1 > LOAD_1_WARN_THRESHOLD or load_5 > LOAD_5_WARN_THRESHOLD or load_15 > LOAD_15_WARN_THRESHOLD:
            # Then load is in warning state...
            shortterm_spike_grace_allowed = SHORTTERM_SPIKE_GRACE
            if not latest_warn_start_time:
                # ...and we just entered warning state.
                simple_cache_set(latest_warn_start_time_cache_keyname, int(time.time()))
            else:
                if time.time() - latest_warn_start_time > SHORTTERM_SPIKE_TIME:
                    # We've been in warning state for longer than our grace period:
                    shortterm_spike_grace_allowed = 0
            # Re-check if the warning state, with the spike grace, is enough to trigger an alert:
            if load_1 > (LOAD_1_WARN_THRESHOLD+shortterm_spike_grace_allowed) or load_5 > (LOAD_5_WARN_THRESHOLD+shortterm_spike_grace_allowed) or load_15 > (LOAD_15_WARN_THRESHOLD+shortterm_spike_grace_allowed):
                state = angel.constants.STATE_WARN
        else:
            if latest_warn_start_time:
                # Then we just transitioned from warn state to ok state, clear the latest warning start time marker:
                simple_cache_set(latest_warn_start_time_cache_keyname, None)

        if load_1 > LOAD_1_ERROR_THRESHOLD or load_5 > LOAD_5_ERROR_THRESHOLD or load_15 > LOAD_15_ERROR_THRESHOLD:
            state = angel.constants.STATE_ERROR

        message = ''
        if load_1 > LOAD_1_WARN_THRESHOLD: message += "Load 1: %s " % (load_1)
        if load_5 > LOAD_5_WARN_THRESHOLD: message += "Load 5: %s " % (load_5)
        if load_15 > LOAD_15_WARN_THRESHOLD: message += "Load 15: %s " % (load_15)

        self.addDataPointToStatStruct(stat_struct, 'load1', load_1)
        self.updateStatStruct(stat_struct, message=message, state=state)


    def _checkPathIsOkay(self, path):
        # This checks if the kernel has forced the disk into read-only mode due to ext3 errors;
        # it happens that the exception for a read-only filesystem takes precedence over "Permission denied" and "Operation not permitted" errors.
        try:
            path = os.path.expanduser(path)
            os.utime(path, (os.stat(path).st_atime, os.stat(path).st_mtime))
        except Exception as e:
            if 'Permission denied' in e or 'Operation not permitted' in e:
                return True
            else:
                print >>sys.stderr, "Error when checking path %s: %s" % (path, e)
                return False
        return True

        
    def _checkDisk(self, stat_struct):
        # Warn thresholds for disk usage:
        DISK_PERCENTAGE_WARN_THRESHOLD = 80
        DISK_FREE_SPACE_LEFT_WARN_THRESHOLD = 1000 # MB

        # Error thresholds for disk usage -- keep in mind that errors should only be reported when the system is actively failing. We'll give a little wiggle room here though...
        DISK_PERCENTAGE_ERROR_THRESHOLD = 98
        DISK_FREE_SPACE_LEFT_ERROR_THRESHOLD = 250 # MB

        # Verify that filesystems backing our project and DATA_DIR are okay:
        if not self._checkPathIsOkay(self._angel.get_project_base_dir()) or \
                (os.path.exists(self._config['DATA_DIR']) and not self._checkPathIsOkay(self._config['DATA_DIR'])):
            self.updateStatStruct(stat_struct, message='Filesystem issue; check dmesg',
                                  state=angel.constants.STATE_ERROR)

        # If running software raid, check disks & raid health:
        if os.path.exists('/proc/mdstat'):
            mdstat_fh = open('/proc/mdstat')
            mdstat_fh.readline()
            mdstat_data = mdstat_fh.read()  
            mdstat_fh.close()
            if mdstat_data != 'unused devices: <none>\n':
                self.mergeStatStructs(stat_struct, self.checkStatusViaNagiosPlugin('check_linux_raid', []))

        usage_info = disk_stats_get_usage()
        for partition in usage_info:

            # Check absolute space:
            state = angel.constants.STATE_RUNNING_OK
            message = ''

            if usage_info[partition]['free_mb'] < DISK_FREE_SPACE_LEFT_WARN_THRESHOLD:
                self.updateStatStruct(stat_struct, message="Disk %s: %sMB left" % (partition, usage_info[partition]['free_mb']))
                self.updateStatStruct(stat_struct, state=angel.constants.STATE_WARN)

            if usage_info[partition]['free_mb'] < DISK_FREE_SPACE_LEFT_ERROR_THRESHOLD:
                self.updateStatStruct(stat_struct, state=angel.constants.STATE_ERROR)

            if usage_info[partition]['free_inodes'] < 90000:
                self.updateStatStruct(stat_struct, message="Disk %s: %s inodes left" % (partition, usage_info[partition]['free_inodes']))
                self.updateStatStruct(stat_struct, state=angel.constants.STATE_WARN)
                if usage_info[partition]['free_inodes'] < 10000:
                    self.updateStatStruct(stat_struct, state=angel.constants.STATE_ERROR)

            self.updateStatStruct(stat_struct, message=message, state=state)
            # Check percentage space:
            state = angel.constants.STATE_RUNNING_OK
            message = ''
            if usage_info[partition]['used_percent'] > DISK_PERCENTAGE_WARN_THRESHOLD:
                message = "Disk %s: %s%% full" % (partition, usage_info[partition]['used_percent'])
                state = angel.constants.STATE_WARN
            if usage_info[partition]['used_percent'] > DISK_PERCENTAGE_ERROR_THRESHOLD:
                message = "Disk %s: %s%% full" % (partition, usage_info[partition]['used_percent'])
                state = angel.constants.STATE_ERROR
            self.updateStatStruct(stat_struct, message=message, state=state)

        # Make sure that data_dir is big enough on ec2 nodes -- not ideal to check DATA_DIR this way, but better than nothing for now:
        if self._config['DATA_DIR'].startswith('/mnt'):
            active_services = self._angel.get_enabled_or_running_service_objects()
            try:
                min_ok_data_dir_size = max( map(lambda x: x.MIN_OK_DATA_DIR_DISK_SIZE_IN_MB, active_services))  # Find the largest value in all active services on this node
                max_ok_data_dir_usage = min( map(lambda x: x.MAX_OK_DATA_DIR_DISK_USAGE, active_services))  # 0 to 1 (0 to 100%)
                disk_stats = disk_stats_get_usage_for_path(self._config['DATA_DIR'])
                if disk_stats['size_mb'] < min_ok_data_dir_size:
                    self.updateStatStruct(stat_struct, message="DATA_DIR too small", state=angel.constants.STATE_WARN)
                if disk_stats['used_percent'] > (100*max_ok_data_dir_usage):
                    self.updateStatStruct(stat_struct, message="DATA_DIR at %s%%" % disk_stats['used_percent'], state=angel.constants.STATE_WARN)
            except AttributeError:
                self.updateStatStruct(stat_struct, message="Can't figure out required DATA_DIR size (invalid service in enabled/running list)", state=angel.constants.STATE_WARN)


    def _checkNetwork(self, stat_struct):
        # To-Do
        # /proc/net/dev
        # Ignore first 3 lines; 4th line should be eth0
        # (interface, bytes_rec, dummy, dummy, dummy, dummy, dummy, dummy, dummy, bytes_tx, dummy) = split on spaces
        good_hosts = ()
        bad_hosts = ()
        hosts = self._angel.get_all_known_hosts()
        for host in hosts:
            # Ping with packet size 2000 to make sure jumbo packets are working
            (stdout, stderr, exitcode) = self.getCommandOutput(self.which("ping"),args=("-s", 2000, "-c", 1, "-t", 1, host))
            if 0 != exitcode:
                bad_hosts += (host,)
            else:
                good_hosts += (host,)
        if len(bad_hosts):
            self.updateStatStruct(stat_struct, message="%s of %s peers down: %s" %
                                                       (len(bad_hosts), len(hosts), ', '.join(bad_hosts)))
        else:
            self.updateStatStruct(stat_struct, message="%s peers ok" %
                                                       len(good_hosts))
        return


    def _checkPostfix(self, stat_struct):
        # We expect postfix to be listening on localhost:25. This is unrelated to EMAIL_HOST:EMAIL_PORT.
        smtp_status = self.checkStatusViaNagiosPlugin('check_smtp', ['-H', '127.0.0.1', '-t', '2', '-p', 25])
        self.deleteStatFromStatStruct(smtp_status, 'time')
        if not self.isStatStructStateOk(smtp_status):
            self.updateStatStruct(smtp_status, message="Postfix: %s" % smtp_status['message'], replace_instead_of_append=True)
        self.mergeStatStructs(stat_struct, smtp_status)
        self._checkPostfixQueueSize(stat_struct)


    def _checkPostfixQueueSize(self, stat_struct):
        queue_size = simple_cache_get('devops-service-postfix-queue-size', get_function=self._getPostfixQueueSize, get_function_ttl_in_seconds=60)
        if queue_size is None:
            return self.updateStatStruct(stat_struct, message="Postfix: error checking queue size", state=angel.constants.STATE_UNKNOWN)
        self.addDataPointToStatStruct(stat_struct, 'postfix_queue', queue_size)
        if queue_size > 0:
            self.updateStatStruct(stat_struct, message="Postfix: %s queued messages" % queue_size)
        if queue_size > 5:
            self.updateStatStruct(stat_struct, state=angel.constants.STATE_WARN)
        if queue_size > 1000:
            self.updateStatStruct(stat_struct, state=angel.constants.STATE_ERROR)



    def _getPostfixQueueSize(self):
        queue_size, err, exitcode = self.getCommandOutput("/usr/sbin/postqueue -p | grep '^[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]' | wc -l")
        # to-do: this will return queue size of 0 and exit code of 0 when postqueue fails -- ie postqueue: fatal: open /etc/postfix/main.cf: No such file or directory
        # (grep note: when queue is empty, postqueue 'Mail queue is empty', so grep A-Z0-9 chars 5 times to avoid matching 'Mail '.)
        if exitcode != 0:
            print >>sys.stderr, "Error: _getPostfixQueueSize failed: %s / %s" % (exitcode, err)
            return None
        try:
            queue_size = int(queue_size)
            return queue_size
        except:
            print >>sys.stderr, "Error: _getPostfixQueueSize cast failed: %s" % queue_size
        return None

    def _checkCron(self, stat_struct):
        cron_pid = '/var/run/crond.pid'
        if not os.path.isfile(cron_pid):
            self.updateStatStruct(stat_struct, message="Cron pidfile missing", state=angel.constants.STATE_WARN)
            return
        if not is_pid_in_pidfile_running(cron_pid):
            self.updateStatStruct(stat_struct, message="Cron stopped", state=angel.constants.STATE_WARN)


    def shell_tool_copy_to_s3(self, path, remote_name=None, verbose=False, silent=False,
                              s3_bucket=None, s3_key=None, s3_secret=None, s3_region=None):
        ''' Copy files to a tmp public S3 bucket and return a URL for the contents.
            - Note: only print a URL to STDOUT, so we can backtick this).
             * path: file or directory to copy
             * remote_name: name of file to copy to in S3 (defaults to a checksum, IP-named, safe filename)
             * s3_bucket: bucket to copy to (defaults to DEVOPS_S3_TMP_BUCKET_NAME)
             * s3_key: AWS access key to use (defaults to DEVOPS_S3_TMP_BUCKET_ACCESS_KEY_ID)
             * s3_secret: AWS secret key to use (defaults to DEVOPS_S3_TMP_BUCKET_SECRET_ACCESS_KEY)
             * s3_region: AWS endpoint (defaults to DEVOPS_S3_TMP_BUCKET_REGION)
             * verbose: display transfer status
             * silent: print as little as possible
        '''

        # See https://github.com/rlmcpherson/s3gof3r -- would give us faster uploads for this...

        # Set defaults if no overrides are given:
        if s3_bucket is None:
            s3_bucket = self._config['DEVOPS_S3_TMP_BUCKET_NAME']
        if s3_key is None:
            s3_key = self._config['DEVOPS_S3_TMP_BUCKET_ACCESS_KEY_ID']
        if s3_secret is None:
            s3_secret = self._config['DEVOPS_S3_TMP_BUCKET_SECRET_ACCESS_KEY']
        if s3_region is None:
            s3_region = self._config['DEVOPS_S3_TMP_BUCKET_REGION']

        # Set up file and path vars:
        file = None
        if path.startswith('~'):
            path = os.path.expanduser(path)

        # Figure out which tmp dir to use:
        tmp_dir = self._config['TMP_DIR']
        try:
            tmp_test = os.path.join(tmp_dir, ".devops-tmp-test-%s" % time.time())
            open(tmp_test, "w")
            os.remove(tmp_test)
        except:
            tmp_dir = "/tmp"

        # Generate a temporary s3cmd config using our settings:
        tmp_s3_cfg = '[default]\naccess_key = %s\nsecret_key = %s\nhost_base = %s\n' % (s3_key, s3_secret, s3_region)
        tmp_s3_cfg_file = os.path.join(tmp_dir, '.copy-to-s3-cfg-%s' % os.getpid())
        try:
            open(tmp_s3_cfg_file, 'w').write(tmp_s3_cfg)
        except Exception as e:
            print >>sys.stderr, "Error: unable to create temp s3cmd config (%s)." % e
            return 1

        # Check the file we're sending -- or if it's stdin, generate a file:
        delete_tmp_file_after_transfer = False  # So we know to remove tmp file in stdin case
        tmp_file = None
        file_is_stdin = False
        if '-' == path:
            # s3cmd doesn't have a "read from stdin" feature (well, not released yet), so we'll hack this ourselves:
            tmp_file = os.path.join(tmp_dir, 'copy-to-s3-stdin-%s-%s' % (int(time.time()), os.getpid()))
            delete_tmp_file_after_transfer = True
            file_is_stdin = True
            try:
                tmp_fh = os.open(tmp_file, os.O_WRONLY|os.O_CREAT, 0600)
                size = 0
                size_warning_printed = False
                size_warning_threshold = 1024*1024*20
                for input in sys.stdin:
                    os.write(tmp_fh, input)
                    size += len(input)
                    if size > size_warning_threshold and not size_warning_printed:
                        if not silent:
                            print >>sys.stderr, "Warning: passing STDIN to copy-to-s3 generates a tmp file equal to the entire size of STDIN; make sure you don't fill up disk."
                        size_warning_printed = True
                os.close(tmp_fh)
                if size_warning_printed or verbose:
                    if not silent:
                        print >>sys.stderr, "STDIN tmp file created (%s bytes); uploading it to S3 now." % size
                if 0 == size:
                    print >>sys.stderr, "Error: nothing on stdin."
                    return 1
            except Exception as e:
                print >>sys.stderr, "Error: unable to generate tmp file %s: %s" % (tmp_file, e)
                try:
                    os.remove(tmp_file)
                except:
                    pass
                return 1
            file = tmp_file
        
        if os.path.isdir(path):
            if path.endswith('/'):
                path = path[:-1]
            dir_name = os.path.basename(path)
            tmp_file = os.path.join(tmp_dir, "%s.tgz" % dir_name)
            if os.path.exists(tmp_file):
                tmp_file = os.path.join(tmp_dir, "%s-%s.tgz" % (dir_name, int(time.time())))
            tar_exit_code = 0
            try:
                tar_exec = self.which('tar')
                tar_args = ('-czf', tmp_file, path)
                tar_exit_code = self.execCommand(tar_exec, args=tar_args, run_as_child_and_block=True)
                if 0 != tar_exit_code:
                    print >>sys.stderr, "Error: tar failed (%s) when running %s %s" % (tar_exit_code, tar_exec, ' '.join(tar_args))
                    return 1
            except (Exception, KeyboardInterrupt) as e:
                tar_exit_code = -1
            finally:
                if 0 != tar_exit_code and os.path.isfile(tmp_file):
                    print >>sys.stderr, "Error: unable to generate temporary tar file (%s)." % (tmp_file)
                    os.remove(tmp_file)
            delete_tmp_file_after_transfer = True
            file = tmp_file

        if os.path.isfile(path):
            file = path

        if file is None:
            print >>sys.stderr, "Error: path '%s' isn't a file." % path
            return 1

        upload_size = os.stat(file).st_size
        if upload_size <= 0:
            print >>sys.stderr, "Error: file %s is empty." % file
            return 1

        # Generate a semi-useful, safe name:
        if remote_name is None:
            hostname_part = '-%s' % self._angel.get_project_code_branch()
            # Using the branch name can be misleading if we run a different branch in a cluster during testing,
            # but it's probably more useful than not to do
            if '127.0.0.1' != self._angel.get_private_ip_addr():
                hostname_part += '-%s' % self._angel.get_private_ip_addr()
            checksum_part = ''
            checksum = get_md5_of_path_contents(file)
            if checksum:
                checksum_part = '-%s' % checksum[0:8]
            origname_part = ''
            if not file_is_stdin:
                origname_part = '.'.join(os.path.basename(file).lower().split('.')[:-1])[0:32]
                origname_part = '-' + re.sub('[^0-9a-z]+', '-', origname_part)
            suffix = ''
            if not file_is_stdin:
                suffix = file.split('.')[-1].lower()
                if suffix == file:
                    suffix = '.data'
                else:
                    suffix = '.' + suffix
            else:
                suffix = '.data'
            remote_name = 'devops%s%s%s%s' % (hostname_part, checksum_part, origname_part, suffix)

        with_progress = False
        s3_cmd_flags = ('--no-progress',)
        if not silent:
            if verbose or upload_size > 1024*1024*2:
                with_progress = True
                s3_cmd_flags = ('--progress',)

        try:
            # -rr for reduced redundancy (cheaper)
            args = ('--config=%s' % tmp_s3_cfg_file, 'put', file, 's3://%s/%s' % (s3_bucket, remote_name), "-rr") + \
                   s3_cmd_flags
            if not silent and verbose:
                print >>sys.stderr, "Running: s3cmd %s" % ' '.join(args)
            if with_progress:
                if 0 != self.execCommand(self.which('s3cmd'), args=args, run_as_child_and_block=True):
                    print >>sys.stderr, "Error: s3cmd failed."
                    return 1
            else:
                (stdout, stderr, exitcode) = self.getCommandOutput(self.which('s3cmd'), args=args)
                if 0 != exitcode:
                    print >>sys.stderr, stderr
                    return 1
            # To-do: maybe list the object and make sure the size matches?
            print "https://%s.%s/%s" % (s3_bucket, s3_region, remote_name)
            return 0
        finally:
            os.remove(tmp_s3_cfg_file)
            if delete_tmp_file_after_transfer:
                os.remove(tmp_file)
