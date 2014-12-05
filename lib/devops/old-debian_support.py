import os
import re
import socket
import signal
import sys
import time

import devops.file_and_dir_helpers
import devops.process_helpers
from devops.logging import log_to_syslog
from devops.unix_helpers import get_proc_title, set_proc_title
from angel.stats.disk_stats import disk_stats_get_usage
import angel.settings


def debian_is_debian_usable():
    (stdout, stderr, exitcode) = get_command_output('which apt-get')
    if 0 == exitcode: return True
    return False


def debian_is_disk_space_okay_for_install(print_errors=False):
    # Check that install has enough disk resources to work. This assumes /var is part of root partition.
    usage_info = disk_stats_get_usage()
    if usage_info['/']['free_mb'] < 1500: # Note: need ~twice the disk space as the files: one for the .deb download; then the expanded files.
        if print_errors:
            print >>sys.stderr, "Warning: low free disk space (%sMB available)." % usage_info['/']['free_mb']
        return False
    if usage_info['/']['free_inodes'] < 120000: # Need twice the number of files in system; currently at around 40k files in package (ouch).
        if print_errors:
            print >>sys.stderr, "Warning: low free innode count (%s available)." % usage_info['/']['free_inodes']
        return False
    return True


def debian_install_branch(branch,
                          run_at_low_priority=False,
                          force=False,
                          display_progress=False,
                          lock_timeout=0,
                          okay_to_delete_unused_as_needed=False):
    ''' Install (but do not activate) the latest version for the given branch. '''
    old_title = get_proc_title()
    set_proc_title('debian installer: starting')

    # We may want to install things "nicely" on running production systems:
    nice_value = 0
    if not force:
        nice_value = 5
        if run_at_low_priority:
            nice_value = 17
    if nice_value > 0:
        try:
            os.nice(nice_value)
        except:
            print >>sys.stderr, "Warning: unable to set process priority (harmless but annoying)."

    # Install the debian package:
    lock_name = "debian-support"
    set_proc_title('debian installer: acquiring upgrade lock')
    if 0 != devops.file_and_dir_helpers.get_lock(config, lock_name, lock_timeout=lock_timeout, print_errors=False, waiting_message="Waiting on upgrade lock"):
        if not force:
            print >>sys.stderr, "Error: upgrade already in process (can't get lock due to timeout or ctrl-c)."
            return -1
        else:
            print >>sys.stderr, "Warning: upgrade already in process (can't get lock due to timeout or ctrl-c); going to try anyway (--force enabled)."

    set_proc_title('debian installer: waiting for upgrade to finish')
    ret_val = _debian_upgrade_branch(branch, force, display_progress)
    devops.file_and_dir_helpers.release_lock(config, lock_name)
    set_proc_title(old_title)
    return ret_val


def debian_delete_branch(branch):
    # To-do: On ubuntu12, add --force-unsafe-io ?
    (output, output_stderr, exitcode) = get_command_output('dpkg -P %s' % _debian_get_package_name_for_branch(branch), ignore_ctrl_c=True, timeout_in_seconds=600) 
    if exitcode != 0:
        print >>sys.stderr, "dpkg -P failed:\n%s\n%s" % (output, output_stderr)
    return exitcode


def debian_does_branch_exist(branch):
    # Check version info to see if it exists:
    installed_version, repo_version = debian_branch_get_current_and_newest_version(branch, verbose=False)
    if repo_version is not None: return True
    return False


def debian_get_newest_version_number_for_branch(branch):
    ''' Return the newest version of the given branch available in the repo. '''
    installed_version, candidate_version = debian_branch_get_current_and_newest_version(branch, verbose=False)
    return candidate_version


def debian_branch_get_current_and_newest_version(branch, verbose=True):
    ''' Return the current debian installed version (or None) and newest version in repo (or None). '''
    return _debian_package_get_current_and_newest_version(_debian_get_package_name_for_branch(branch), verbose=verbose)


def debian_are_remote_updates_available(branch, force):
    ''' Returns true if a newer version of given branch exists in the code repo that we can install. '''
    if 0 != _debian_update_aptget_cache(force=force):
        print >>sys.stderr, "Warning: unable to update apt-get repo cache."

    # Check if there's anything newer in the repo:
    (installed_version, candidate_version) = debian_branch_get_current_and_newest_version(branch)
    if candidate_version is None:
        # then branch doesn't exist in repo
        return False
    elif installed_version is None:
        # then branch isn't installed locally
        return True 
    elif installed_version == candidate_version:
        # then repo and local are equal
        return False 
    else:
        # otherwise local is behind repo
        return True


def _debian_package_get_current_and_newest_version(debian_package_name, verbose=True):
    if 0 != _debian_update_aptget_cache():
        print >>sys.stderr, "Warning: unable to update package cache"
    try:
        (version_info, stderr, exitcode) = devops.process_helpers.get_command_output('apt-cache policy ' + debian_package_name, timeout_in_seconds=60)
        if 0 != exitcode: return None, None
    except Exception as e:
        print >>sys.stderr, "Error: apt-cache failed (%s)." % e
        return None, None

    # version_info will look something like:
    #    <debian_package_name>:
    #      Installed: (none)
    #      Candidate: 552.00395.g780fb8c
    #      Version table:
    matches = re.search(r'Installed:\s+([^\s]+)\s+Candidate:\s+([^\s]+)', version_info)
    if matches:
        installed_version = matches.group(1)
        candidate_version = matches.group(2)
        if installed_version == '(none)': installed_version = None
        # Debian versions are now "111-10.04" / "111-12.04" -- split off Debian version info in below parsing:
        try:
            installed_version = int(installed_version.split('-')[0])
        except:
            pass
        try:
            candidate_version = int(candidate_version.split('-')[0])
        except:
            pass
        return installed_version, candidate_version
    if verbose: print >>sys.stderr, "Warning: can't find package information for '%s'." % debian_package_name
    return None, None


def _debian_install_package(debian_package_name, display_progress, retry_count=3):
    ''' Run dpkg installer for the given package. '''
    start_time = time.time()

    # Check that we're running as root:
    if 0 != os.getuid():
        print >>sys.stderr, "Error: upgrade must be run as root."
        return 1

    upgrade_command = 'DEBIAN_FRONTEND=noninteractive apt-get -y install ' + debian_package_name # To-do: look into dpkg --force-unsafe-io -- not sure how to pass that through apt-get

    # apt-get has no option to tell it to upgrade dependent packages.
    # We sneak in all installed packages that start with 'namesake-' (but not namesake-chill to avoid other branches) at the same time so as
    # to make sure that any dependencies we need get upgrade as well. 
    (dpkg_stdout, dpkg_stderr, dpkg_exitcode) = devops.process_helpers.get_command_output("dpkg -l namesake-'*' | grep namesake- | grep -v namesake-chill- | cut -b -40 | grep ii | cut -b 5- | awk '{print $1}' | tr '\\n' ' '", timeout_in_seconds=60)
    if 0 == dpkg_exitcode:
        upgrade_command += ' ' + dpkg_stdout
    else:
        print >>sys.stderr, "Warning: unable to generate list of namesake packages in _debian_install_package(), dependencies won't be upgrade."

    # Run the apt-get install:
    log_to_syslog("_debian_install_package: %s" % upgrade_command)
    if display_progress:
        dummy, newest_version = _debian_package_get_current_and_newest_version(debian_package_name)
        print ("Downloading and staging %s version %s..." % (debian_package_name.replace('namesake-chill-', ''), newest_version)),
        # (Funky replace call above so we can use this function to install *any* debian package, but pretty-print it when it's our chill package.)
        sys.stdout.flush()

    upgrade_stdout, upgrade_stderr, upgrade_exitcode = devops.process_helpers.get_command_output(upgrade_command, ignore_ctrl_c=True, timeout_in_seconds=1200)

    try:
        log_entry = '********************\n' + \
                    'Start time: %s\n' % start_time + \
                    'Command: %s\n' % upgrade_command + \
                    'Exit code: %s\n' % upgrade_exitcode + \
                    'Stderr/stdout:\n%s\n%s\n\n\n' % (upgrade_stderr, upgrade_stdout)
        open('/var/log/chillops-install.log', 'a').write(log_entry)
    except Exception as e:
        print >>sys.stderr, "Error writing to chillops-install.log: %s\n%s" % (e, log_entry)

    if 100 == upgrade_exitcode:
        if 0 != _debian_aptget_fix_broken():
            print >>sys.stderr, "\n\n******\nUnknown dpkg error %s when running %s:\n%s\n%s\n****** Please send this error info to devops.\n" % (upgrade_exitcode, upgrade_command, upgrade_stdout, upgrade_stderr)
            return 1
    if upgrade_exitcode != 0:
        if retry_count > 0:
            print >>sys.stderr, "Warning: _debian_install_package failed (%s); retrying in 5 seconds (%s retries left)." % (upgrade_exitcode, retry_count)
            try:
                time.sleep(5)
            except:
                return 1
            return _debian_install_package(debian_package_name, display_progress, retry_count=retry_count-1)
        else:
            print >>sys.stderr, "Warning: _debian_install_package failed (%s); no retries left. Error info:\n%s\n%s." % (upgrade_exitcode, upgrade_stderr, upgrade_stdout)
            # We return upgrade_exitcode below; run cleanup in case that helps on next attempt.

    # Remove old packages to free up disk space:
    _debian_aptget_cleanup()

    end_time = time.time()
    run_time = (int(end_time - start_time))
    if 0 == upgrade_exitcode and display_progress:
        if run_time < 4:
            print "done."
        else:
            on_hostname = ''
            try:
                on_hostname = " on node %s" % socket.gethostname()
            except:
                pass
            print "installed in %s seconds%s." % (run_time, on_hostname)

    return upgrade_exitcode


def _debian_get_etag_cachefile_path():
    return '/tmp/.namesake-deploy-repo-etag'


def _debian_get_last_seen_etag():
    path = _debian_get_etag_cachefile_path()
    data = None
    if os.path.exists(path): data = open(path).read()
    return data


def _debian_set_last_seen_etag(etag):
    path = _debian_get_etag_cachefile_path()
    try:
        open(path, 'w').write(etag)
    except IOError:
        print >>sys.stderr, "Warning: unable to write upgrade cache file '%s', try running as root. (Upgrade will continue without cache.)" % path


def _debian_update_aptget_cache(force=False, retry_count=3):
    ''' Update the local apt-get package lists. Returns non-zero if apt-get encounters serious local errors.
        This function is smart about caching the etag header from our repo, which then avoids us hitting 
        our own servers and the debian servers when not necessary.
    '''

    # Double-check that we're running as root:
    if 0 != os.getuid():
        print >>sys.stderr, "Error: apt-get update must be run as root."
        return 1

    # Do some cache checking: if the etag header on our repo server hasn't changed since we last ran,
    # then there won't be any changes -- so don't even bother running apt-get:
    etag = _debian_get_current_etag_for_namesake_repo()
    if etag is None:
        print >>sys.stderr, "Error: apt-get update can't fetch etag from repo (check firewall and repo status)."
        return 1

    etagCachedValue = _debian_get_last_seen_etag()

    if etagCachedValue != etag or force:
        # Update apt-get list of packages:
        try:
            (aptget_stdout, aptget_stderr, aptget_exitcode) = devops.process_helpers.get_command_output('/usr/bin/apt-get -q -q update', timeout_in_seconds=60)
        except KeyboardInterrupt:
            print >>sys.stderr, "Error: apt-get update canceled."
            return 1

        if aptget_exitcode:

            # Set stdout / stderr results to empty string if None -- this can happen if the timeout is reached
            if aptget_stdout is None:
                aptget_stdout = ""
            if aptget_stderr is None:
                aptget_stderr = ""

            if "dpkg was interrupted, you must manually run 'sudo dpkg --configure -a' to correct the problem" in aptget_stderr:
                # This happens when a previous upgrade ran dpkg and it was aborted or failed mid-install:
                print >>sys.stderr, "Warning: debian dpkg was previously interrupted, attempting to auto-repair."
                if 0 != _debian_dpkg_fix_pending():
                    print >>sys.stderr, "Error: debian dpkg was previously interrupted, unable to auto-repair."
                    return 1

            elif 100 == aptget_exitcode and "Could not get lock" in aptget_stderr:
                # This happens when another instance of apt-get is running; wait a few seconds and try again:
                if retry_count > 0:
                    try:
                        time.sleep(3) # In addition to 2 seconds below, so 5 seconds in this condition
                    except:
                        print >>sys.stderr, "Error: apt-get update retry aborted."
                        return 1
                else:
                    print >>sys.stderr, "Error: apt-get update failed; apt-get was unable to get the lock."

            elif 100 == aptget_exitcode and ("open (28: No space left on device)" in aptget_stderr):
                # This can happen if the debian apt-get stuff gets corrupted, which causes it to mis-calculate the free disk space:
                print >>sys.stderr, "Warning: encountered 'open (28: No space left on device)' error; running apt-get clean all and trying again."
                if 0 != _debian_aptget_clean_all():
                    print >>sys.stderr, "Error: apt-get hit 'No space left on device' error."
                    print >>sys.stderr, aptget_stderr
                    return 1

            elif 100 == aptget_exitcode and ("Requested Range Not Satisfiable" in aptget_stderr or "Hash Sum mismatch" in aptget_stderr):
                # This happens when we've downloaded part of a repo package list and the upstream fails:
                if 0 != _debian_aptget_fix_partial_download():
                    print >>sys.stderr, "Error: apt-get responding with 'Requested Range Not Satisfiable'."
                    return 1

            elif 139 == aptget_exitcode and "Segmentation fault" in aptget_stderr:
                # We've seen this happen once on Ubuntu 10, and it was really confussing; so here's a hint:
                print >>sys.stderr, "Error: apt-get seg faulted. This can happen if the apt cache gets corrupted. If this is a non-prod node, try:\n find /var/cache/apt -type f | sudo xargs rm\n"
                return 1

            else:
                # I suspect the error:
                #   "Some index files failed to download, they have been ignored, or old ones used instead"
                # is connected to "Requested Range Not Satisfiable" error.
                print >>sys.stderr, "Warning: '/usr/bin/apt-get -q -q update' failed.\n\n******\nUnknown apt-get error %s:\n%s\n%s\n****** Please send this error info to devops.\n" % (aptget_exitcode, aptget_stderr, aptget_stdout)

            # If we had a soft error, we'll get here -- retry the update again if retries left:
            if retry_count <= 0:
                print >>sys.stderr, "Error: failed to run 'apt-get update'."
                return 1
            try:
               time.sleep(2)
            except:
               return 1
            return _debian_update_aptget_cache(force=force, retry_count=retry_count-1)

        # Update etag cache:
        _debian_set_last_seen_etag(etag)

    return 0


def _debian_dpkg_fix_pending():
    return _debian_fix_helper('dpkg --configure -a')


def _debian_aptget_fix_partial_download():
    return _debian_fix_helper('rm -rf /var/lib/apt/lists/partial/*')


def _debian_aptget_clean_all():
    return _debian_fix_helper('apt-get clean all')


def _debian_aptget_fix_broken():
    return _debian_fix_helper('apt-get -f install')


def _debian_fix_helper(command):
    log_to_syslog("_debian_fix_helper: %s" % command)
    ret_out, ret_err, ret_val = devops.process_helpers.get_command_output(command, timeout_in_seconds=300)
    if ret_val == 0: return 0
    print >>sys.stderr, "Error: unable to fix debian installer; please send the following to devops: exit %s when running '%s'\n%s\n%s\n" % (ret_val, command, ret_out, ret_err)
    return 1



def _debian_get_package_name_for_branch(branch):
    if branch is None:
        print >>sys.stderr, "Warning: missing branch name in _debian_get_package_name_for_branch()."
        return None
    return 'namesake-chill-%s' % branch


def _debian_get_current_etag_for_namesake_repo(retry_count=3):
    (etagHeader, stderr, exitcode) = devops.process_helpers.get_command_output('curl --silent --head http://deploy.namesaketools.com/ubuntu/db/packages.db | grep ETag')
    if 0 == exitcode:
        matches = re.search(r'ETag:\s+\"([^\"]+)', etagHeader)
        if matches:
            return matches.group(1)
    if retry_count > 0:
        try:
            time.sleep(3)
        except:
            return None
        return _debian_get_current_etag_for_namesake_repo(retry_count=retry_count-1)
    log_to_syslog("_debian_get_current_etag_for_namesake_repo: failed to contact repo")
    print >>sys.stderr, "Error: unable to contact namesake repo (no etag found)."
    return None


def _debian_aptget_cleanup(run_in_background=True):
    # Remove old packages (runs in a child process so that we finish installing faster; normally this is fast but occasionally it pauses for a few seconds):
    
    # Remove sigchild handler, so we don't zombie:
    old_sigchld = signal.getsignal(signal.SIGCHLD)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    if run_in_background:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            if 0 != os.fork():
                # Parent process: restore sig handler and return
                if old_sigchld is not None:
                    signal.signal(signal.SIGCHLD, old_sigchld)
                return
            else:
                sys.stdout = open(os.devnull)  # Something in subprocess or here is printing a blank newline on exit; make that go away...

        except:
            print >>sys.stderr, "Warning: unable to create cleanup process."
            return

    try:
        log_to_syslog("_debian_aptget_cleanup: apt-get autoclean")
        (out, err, exitcode) = devops.process_helpers.get_command_output('apt-get autoclean', timeout_in_seconds=120)  # Remove old packages otherwise we leak disk
        if 0 != exitcode:
            print >>sys.stderr, "Warning: 'apt-get autoclean' failed (exit %s: stdout='%s'; stderr='%s')." % (exitcode, out, err)
    except Exception as e:
        print >>sys.stderr, "Warning: 'apt-get autoclean' failed with exception '%s'." % e

    if run_in_background:
        # Child process always exits here and ignores any failures.
        sys.exit(0)


def _debian_upgrade_branch(branch, force, display_progress):
    ''' Update namesake debian packages as necessary. Returns 0 on success, non-zero otherwise. '''

    # Check if there's a newer package for the given branch than currently installed:
    if not debian_are_remote_updates_available(branch, force):
        if not force:
            return 0

    if not debian_is_disk_space_okay_for_install():
        _debian_aptget_cleanup(run_in_background=False)

    if not force and not debian_is_disk_space_okay_for_install(print_errors=display_progress):
        return 1

    # Install or upgrade debian package (debian uses the 'install' command for upgrades):
    return _debian_install_package(_debian_get_package_name_for_branch(branch), display_progress)
