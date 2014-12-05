import angel.util.dedup_files
import angel.util.file
import angel.util.process
import ctypes
import glob
import os
import random
import shutil
import signal
import sys
import time


class AngelVersionManager():

    """ Version Manager creates and maintains information about branch/builds for a given project.

    Versions are stored under a top-level "version dir", where each branch is a subdirectory, and
    each version for a branch is a sub-sub directory.

    Each branch has a "default" version; and the version dir has a "default" branch as well, which
    are tracked by using symlinks.

    In addition, we track downgrade info for each version.

    Creating an AngelVersionManager with a non-existent versions_dir path will trigger the creation
    of the directory and prepare it for add-version calls.

    """

    _versions_dir = None

    def __init__(self, versions_dir):
        """
        @param versions_dir: path to top level of our versions dir
        """
        self._versions_dir = versions_dir
        if not os.path.isdir(versions_dir):
            try:
                os.makedirs(versions_dir)
                os.makedirs(self._get_angel_version_data_dir(), mode=0700)
                print >>sys.stderr, "Created new versions dir (at %s)" % versions_dir
            except Exception as e:
                raise angel.exceptions.AngelVersionException("Unable to create new versions dir '%s' (%s)" % (versions_dir, e))


    def _get_angel_version_data_dir(self):
        return os.path.join(self._versions_dir, '.angel_version_data')


    def _get_checksum_hardlink_path(self):
        return os.path.join(self._get_angel_version_data_dir(), 'dedup_hardlinks')


    def add_version(self, branch, version, path_to_src_code, sleep_ratio=0):
        """Add the files at the given path to our version system, hardlink-copying it as given branch and version.
        @param branch: branch name, as a string
        @param version: branch version, as a string, in X.Y format; 1.10 is "newer" than 1.9
        @param path_to_src_code: path to code to add to version system
        @param sleep_ratio: ratio of sleep-to-work; useful for background slow installs on loaded systems
        """

        new_version_path = self.get_path_for_version(branch, version)

        def _first_time_install_logic():
            # Check if we're a new install:
            if not os.path.exists(self._get_default_branch_symlink()):
                # If there's no default branch, then it's a new install:
                print >>sys.stderr, "Creating default branch/version links and activating version"
                self.activate_version(branch, version)

            # Check if the default symlinks need creating (on new branches):
            if not os.path.exists(self._get_default_version_symlink(branch)):
                print >>sys.stderr, "Creating default version link"
                self.set_default_version_for_branch(branch, version)

        if self.is_version_installed(branch, version):
            # We've seen failure where the version installed but didn't activate,
            # so re-check first-time installs even if version is installed:
            _first_time_install_logic()
            raise angel.exceptions.AngelVersionException("Branch %s, version %s already installed" % (branch, version))

        # Checksum files, when they exist, contain checksums for files in the src directory, meaning we
        # can skip calculating checksums for those entries.
        checksum_file = os.path.join(path_to_src_code, '.angel', 'file_checksums')
        src_path_checksum_values = None
        if not os.path.isfile(checksum_file):
            print >>sys.stderr, "Warning: no checksum file at %s" % checksum_file
        else:
            src_path_checksum_values = angel.util.dedup_files.dedup_load_checksum_file(checksum_file)

        # Create a dedup-based copy of the version:
        angel.util.dedup_files.dedup_create_copy(path_to_src_code,
                                                 new_version_path,
                                                 self._get_checksum_hardlink_path(),
                                                 file_checksums=src_path_checksum_values,
                                                 sleep_ratio=sleep_ratio)

        # Add the versions_dir info into the versions .angel directory:
        open(os.path.join(new_version_path,".angel","versions_dir"), "w").write(self._versions_dir)

        # Check and run any first-time install logic:
        _first_time_install_logic()


    def exec_command_with_version(self, branch, version, command, args, env=None):
        """Exec the command (path relative to project basedir) using the given branch and version."""
        basedir = self.get_path_for_version(branch, version)
        if not os.path.isdir(basedir):
            raise angel.exceptions.AngelVersionException("Can't find branch %s, version %s (missing %s)." % (branch, version, basedir))
        command_path = os.path.realpath(os.path.join(basedir, command))
        if not os.path.isfile(command_path):
            raise angel.exceptions.AngelVersionException("Can't find %s command in branch %s, version %s." (command, branch, version))
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            if env:
                os.execve(command_path, (command_path,) + args, env)
            else:
                os.execv(command_path, (command_path,) + args)
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Unable to exec %s (%s)." % (command_path, e))
        raise angel.exceptions.AngelVersionException("Error in exec of %s." % command_path)


    def get_available_installed_branches(self):
        """Return a list of locally-available branches, sorted by name."""
        if not os.path.isdir(self._versions_dir):
            raise angel.exceptions.AngelVersionException("Unable to get available installed branches " +
                                                         "(versions dir '%s' doesn't exist)." % (self._versions_dir))
        try:
            # Get a list of all dirs whose first character is a-z0-9 followed by anything;
            # this way we avoid _default_branch and .angel* entries.
            old_dir = os.getcwd()
            os.chdir(self._versions_dir)
            dirs = glob.glob("[a-zA-Z0-9]*")
            os.chdir(old_dir)
            return sorted(dirs)
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Unable to get available installed branches (exception: %s)." % (e))


    def get_default_branch(self):
        ''' Return the name of the default branch.
        This can be different than what's running if --use-branch was specified. '''
        branch_symlink = self._get_default_branch_symlink()
        if not os.path.islink(branch_symlink):
            return None  # Then we're a new install (hopefully)
        return os.path.basename(os.path.realpath(branch_symlink))


    def get_default_version(self, branch):
        ''' Return the default version for the given branch.
        This can be different than what's running if --use-version was specified. '''
        version_symlink = self._get_default_version_symlink(branch)
        if version_symlink is None:
            return None
        if not os.path.islink(version_symlink):
            return None  # Then the given branch isn't installed
        return os.path.basename(os.path.realpath(version_symlink))


    def get_available_installed_versions(self, branch):
        """Return a list of installed versions for the given branch, sorted by version number (oldest first)."""
        branches_dir = os.path.join(self._versions_dir, branch)
        if not os.path.isdir(branches_dir):
            raise angel.exceptions.AngelVersionException("Unknown branch '%s'" % branch)
        dirs = glob.glob("%s/[0-9]*" % (branches_dir))
        # Versions are X[.Y], this will capture all names whose first character is 0-9 to skip our _default symlink
        versions = ()
        for dir in dirs:
            versions += (os.path.basename(dir),)
        def sort_function(x,y):
            if x == y:
                return 0
            if self.is_version_newer(x,y):
                return -1
            return 1
        return sorted(versions, cmp=sort_function)


    def _get_path_for_branch(self, branch):
        """Returns the path for where versions for the given branch should be stored (regardless if it exists)"""
        return os.path.join(self._versions_dir, branch)


    def get_path_for_version(self, branch, version):
        """Returns the path for where the base dir of the given branch and
        version should be stored (regardless if it exists)"""
        if version is None or branch is None:
            raise angel.exceptions.AngelUnexpectedException("undefined branch %s or version %s" % (branch, version))
        return os.path.join(self._versions_dir, branch, version)


    def is_default_branch(self, branch):
        """Return true if the given branch is the default."""
        if self.get_default_branch() == branch:
            return True
        return False


    def is_default_branch_and_version(self, branch, version):
        """Return true if the given branch and version is the default version."""
        if self.get_default_branch() == branch and self.get_default_version(branch) == version:
            return True
        return False


    def get_highest_installed_version_number(self, branch):
        """Return the highest locally installed version number for the given branch; throws exception if not found."""
        available_versions = self.get_available_installed_versions(branch)
        if 0 == len(available_versions):
            raise angel.exceptions.AngelVersionException("No versions found for branch %s" % branch)
        return available_versions[-1]


    def delete_version(self, branch, version, delete_even_if_in_use=False):
        """Delete the given version from the system; throws exception if version is in use or not found."""
        if not self.is_version_installed(branch, version):
            raise angel.exceptions.AngelVersionException("Version %s not installed." % version)
        if not delete_even_if_in_use:
            if self.is_version_in_use(branch, version):
                raise angel.exceptions.AngelVersionException("Can't delete in-use version %s." % version)
        version_dir = self.get_path_for_version(branch, version)
        if not os.path.isdir(version_dir):
            raise angel.exceptions.AngelVersionException("Unable to find branch %s, version %s" % (branch, version))
        global _delete_version_ignore_handler_triggered
        _delete_version_ignore_handler_triggered = False
        old_sigint = None
        try:
            def ignore_handler(signum, frame):
                global _delete_version_ignore_handler_triggered
                _delete_version_ignore_handler_triggered = True
                print >>sys.stderr, "Interrupted; will finish removing %s.%s then abort" % (branch, version)
            old_sigint = signal.signal(signal.SIGINT, ignore_handler)
            if not os.path.isdir(self._versions_dir):
                print >>sys.stderr, "Missing branches dir during delete version?! (%s)" % self._versions_dir
                raise angel.exceptions.AngelVersionException("Missing versions dir")
            # Move the version to an invalid version path so that it's not accidentally used during delete:
            version_dir_deletion_path = os.path.join(self._versions_dir, branch, "_deleteing_%s" % (version))
            os.rename(version_dir, version_dir_deletion_path)
            shutil.rmtree(version_dir_deletion_path)
            try:
                angel.util.dedup_files.remove_unused_links(self._get_checksum_hardlink_path())
            except Exception as e:
                # On the off-chance that another process is also cleaning up, we ignore dedup issues.
                print >>sys.stderr, "Warning: unable to clean up dedup links while deleting branch %s, version %s (%s); ignoring." % (branch, version, e)
        except Exception as e:
            raise angel.exceptions.AngelUnexpectedException("Error deleting branch %s, version %s (%s: %s)" % (branch, version, version_dir, e))
        finally:
            if old_sigint:
                signal.signal(signal.SIGINT, old_sigint)
        if _delete_version_ignore_handler_triggered:
            raise KeyboardInterrupt
        del _delete_version_ignore_handler_triggered


    def _get_default_branch_symlink(self):
        """Return the path to the symlink that defines the default branch."""
        return os.path.join(self._versions_dir, '_default')


    def _get_default_version_symlink(self, branch):
        """Return the path to the symlink that defines the default version for the given branch."""
        if branch is None:
            raise angel.exceptions.AngelVersionException("Invalid branch")
        return os.path.join(self._versions_dir, branch, '_default')


    def is_version_pinned(self):
        """Return true if the active branch/version is pinned (e.g. version locked to prevent version switches)."""
        return os.path.isfile(self._get_version_pinned_filepath())


    def get_version_pinned_reason(self):
        """Return a text string explaining why the version is pinned."""
        try:
            return open(self._get_version_pinned_filepath()).read().rstrip()
        except Exception as e:
            print >>sys.stderr, "Warning: can't get version pinned reason (%s)." % e
            return "(Error: exception getting lock contents)"


    def _get_version_pinned_filepath(self):
        """Return the path to the file used to track version pinning."""
        return os.path.join(self._get_angel_version_data_dir(), "version_pinned.lock")


    def pin_version(self, reason=None):
        """Lock the version, preventing upgrades or branch changes from being activated. This is primarily for preventing
           a node from being upgraded for whatever reason we might have.
           If the ENV variable LC_DEPLOY_USER is set, we'll include a note with the contents of that value.
        """
        if reason is None:
            reason = "no reason given"
        if 'LC_DEPLOY_USER' in os.environ:
            reason += ' (pinned by %s)' % os.environ['LC_DEPLOY_USER']
        try:
            if not os.path.exists(os.path.dirname(self._get_version_pinned_filepath())):
                os.makedirs(os.path.dirname(self._get_version_pinned_filepath()))
            open(self._get_version_pinned_filepath(), 'wt').write(reason)
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Failed to pin system (%s)." % (e))


    def unpin_version(self):
        """Remove the version pin from our versioning system."""
        try:
            if os.path.isfile(self._get_version_pinned_filepath()):
                os.remove(self._get_version_pinned_filepath())
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Failed to unpin system (%s)." % (e))


    def is_branch_in_use(self, branch):
        """Return true if the given branch is actively or potentially in-use."""
        if self.get_default_branch() == branch:
            return True
        if angel.util.file.is_path_in_use(self._get_path_for_branch(branch)):
            return True
        return False


    def is_branch_installed(self, branch):
        try:
            return os.path.isdir(os.path.join(self._versions_dir, branch))
        except:
            return False


    def is_version_installed(self, branch, version):
        if version is None or branch is None:
            return False
        try:
            return os.path.isdir(os.path.join(self._versions_dir, branch, version, ".angel"))
        except:
            return False


    def is_newer_version_installed(self, branch, version):
        """Return true if there is a newer version than given version available locally for the given branch."""
        highest_version = self.get_highest_installed_version_number(branch)
        if self.is_version_newer(version, highest_version):
            return True
        if version == highest_version:
            return False
        print >>sys.stderr, "Warning: version '%s' higher than highest version %s?" % (version, highest_version)
        return False


    def is_version_in_use_by_processes(self, branch, version):
        """Return true if the given branch/version is actively running processes."""
        return angel.util.file.is_path_in_use(self.get_path_for_version(branch, version))


    def is_version_in_use(self, branch, version):
        """Return true if the given version of the given branch is actively or potentially in-use, including set as
        the default version for the branch."""
        if self.get_default_version(branch) == version:
            return True
        if self.is_version_in_use_by_processes(branch, version):
            return True
        return False


    def delete_stale_versions(self, branch, keep_newest_n_versions, limit=3):
        """Delete up to <limit> unused versions of given branch, without ever deleting anything in the N newest versions.
        Always excludes the running and default versions, so after this there may be still be more than N versions."""
        versions = self.get_available_installed_versions(branch)
        if len(versions) <= keep_newest_n_versions:
            return
        versions = versions[:-keep_newest_n_versions]
        for version in versions:
            if limit <= 0:
                return
            if not self.is_version_in_use(branch, version):
                self.delete_version(branch, version, delete_even_if_in_use=True)  # Skip re-checking if it's in use
                limit -= 1


    # Disabling this -- now that we're tucking the .gitcheckout dir under the versioned path, deduping the
    # innards of ".gitcheckout/.git" might be really, really bad; we need to add an "exclude" pattern match for this
    #def dedup_files(self, sleep_ratio=0):
    #    '''Dedup versioned files; where sleep ratio of 0 is no sleep at all, and at 1, very little work is done.'''
    #    return angel.util.dedup_files.dedup_files(self._versions_dir, sleep_ratio=sleep_ratio)


    def is_version_newer(self, a, b):
        """Return True if b > a, False otherwise.
            This assumes versions are of the format X[.Y[...Z], where X, Y, .. Z are ints.
            As long as any sha1-checksums come after the point where a version can be told to be newer, we're fine.
            If two identical versions have different checksums, though, this will throw an exception. (That should
            never happen though, right? Right?)"""
        if a == b:
            return False
        try:
            a_parts = a.split('.')
            b_parts = b.split('.')
            while len(a_parts) or len(b_parts):
                this_a = 0
                this_b = 0
                if len(a_parts):
                    this_a = int(a_parts[0])
                    a_parts.pop(0)
                if len(b_parts):
                    this_b = int(b_parts[0])
                    b_parts.pop(0)
                if this_b > this_a:
                    return True
                if this_b < this_a:
                    return False
            return False
        except Exception as e:
            raise angel.exceptions.AngelUnexpectedException("Invalid versions in comparison of '%s' to '%s' (%s)." % (a, b, e))


    def _get_downgrade_control_filepath(self, branch, downgrade_from_version):
        """Return the path to a file that stores the downgrade info for the version."""
        return os.path.join(self._get_angel_version_data_dir(), "downgrades", branch, "downgrade-from-%s" % downgrade_from_version)


    def get_downgrade_version(self, branch, downgrade_from_version):
        """Return the version that we downgrade to from the given version; throws exception if version doesn't exist."""
        from_path = self.get_path_for_version(branch, downgrade_from_version)
        if not os.path.isdir(from_path):
            raise angel.exceptions.AngelVersionException("Can't downgrade from %s; version not found." %
                                                         downgrade_from_version)
        downgrade_control_file = self._get_downgrade_control_filepath(branch, downgrade_from_version)
        if not os.path.isfile(downgrade_control_file):
            raise angel.exceptions.AngelVersionException("Can't downgrade from %s; control file %s not found." %
                                                         (downgrade_from_version, downgrade_control_file))
        try:
            downgrade_to_version = open(downgrade_control_file).read().rstrip()
            if not self.is_version_installed(branch, downgrade_to_version):
                raise angel.exceptions.AngelVersionException("Downgrade version %s no longer installed" %
                                                             (downgrade_to_version))
            return downgrade_to_version
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Can't downgrade from %s; invalid data in control file %s." %
                                                         (downgrade_from_version, downgrade_control_file))


    def set_downgrade_version(self, branch, downgrade_from_version, downgrade_to_version):
        """Set the downgrade version, where downgrade_from_version will be rolled back to downgrade_to_version on a downgrade call."""
        from_path = self.get_path_for_version(branch, downgrade_from_version)
        to_path = self.get_path_for_version(branch, downgrade_to_version)
        if not os.path.isdir(to_path) or not os.path.isdir(from_path):
            raise angel.exceptions.AngelVersionException("Missing paths while setting downgrade version (%s -> %s)." % (from_path, to_path))
        if self.is_version_newer(downgrade_from_version, downgrade_to_version):
            raise angel.exceptions.AngelVersionException("Downgrade version is newer than new version")
        downgrade_control_file = self._get_downgrade_control_filepath(branch, downgrade_from_version)
        if not os.path.isdir(os.path.dirname(downgrade_control_file)):
            try:
                os.makedirs(os.path.dirname(downgrade_control_file))
            except Exception as e:
                raise angel.exceptions.AngelVersionException("Unable to create dir for %s: %s" %
                                                             (downgrade_control_file, e))
        try:
            # Write to a tmp file so we don't punch through a hardlink -- this is the only place we actually edit a file
            # in our versions dir; so do this to avoid accidental changes if the surrounding code changes in the future.
            downgrade_control_file_tmp = '%s-%s' % (downgrade_control_file, time.time())
            open(downgrade_control_file_tmp, 'w').write(str(downgrade_to_version))
            os.rename(downgrade_control_file_tmp, downgrade_control_file)
        except Exception as e:
            raise angel.exceptions.AngelVersionException("Unable to set downgrade version (file %s; error %s)" %
                                                         (downgrade_control_file, e))


    def set_default_branch(self, branch, force=False):
        """Set the system to use the given branch by default."""

        if branch == self.get_default_branch():
            return

        if not self.is_branch_installed(branch):
            raise angel.exceptions.AngelVersionException("Can't set default to non-existent branch %s" % (branch))

        if self.is_version_pinned() and not force:
            raise angel.exceptions.AngelVersionException("Can't set default when pinning is on (%s)." % self.get_version_pinned_reason())

        new_default_branch_dir = self._get_path_for_branch(branch)
        symlink_path = self._get_default_branch_symlink()
        try:
            os.symlink(new_default_branch_dir, symlink_path + ".new")
            os.rename(symlink_path + ".new", symlink_path)
        except Exception as e:
            raise angel.exceptions.AngelUnexpectedException("Unable to update branch symlink to %s (%s)" % \
                                (new_default_branch_dir, e))
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.sync()
        except:
            print >>sys.stderr, "Warning: failed to call libc.sync()"


    def set_default_version_for_branch(self, branch, version, force=False):
        """Set the system to use given version for given branch by default."""

        if version == self.get_default_version(branch):
            return

        if not self.is_version_installed(branch, version):
            raise angel.exceptions.AngelVersionException("Can't find branch %s, version %s" % (branch, version))

        if self.is_version_pinned() and not force:
            raise angel.exceptions.AngelVersionException("Can't set default when pinning is on (%s)" % self.get_version_pinned_reason())

        new_default_version_dir = self.get_path_for_version(branch, version)
        symlink_path = self._get_default_version_symlink(branch)
        try:
            os.symlink(new_default_version_dir, symlink_path + ".new")
            os.rename(symlink_path + ".new", symlink_path)
        except Exception as e:
            raise angel.exceptions.AngelUnexpectedException("Unable to update version symlink to %s (%s)" % \
                                (new_default_version_dir, e))
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.sync()
        except:
            print >>sys.stderr, "Warning: failed to call libc.sync()"


    def activate_version(self, branch, version, downgrade_allowed=False, jitter=0):
        """Set the default version/branch to the given version/branch, marking the roll-back version,
        and triggering any post_activation script. This does not calling service_reload or handle running processes."""

        # Verify that the requested branch/version is installed:
        if not self.is_version_installed(branch, version):
            raise angel.exceptions.AngelVersionException("Branch %s, version %s not installed." % (branch, version))

        # If requested branch/version is already the default version, do nothing:
        if self.is_default_branch_and_version(branch, version):
            return

        current_default_branch = self.get_default_branch()  # None when we're a brand new install
        current_default_version_for_target_branch = self.get_default_version(branch)  # None when branch is first installed

        # Check if downgrading, and if allowed to do so:
        if not downgrade_allowed:
            if current_default_version_for_target_branch:
                if not self.is_version_newer(current_default_version_for_target_branch, version) and current_default_version_for_target_branch != version:
                    raise angel.exceptions.AngelVersionException("Can't activate an older version to replace version without downgrade-allowed flag (%s !> %s)" %
                                                                 (version, current_default_version_for_target_branch))

        # Warn if we're switching branches:
        if current_default_branch and branch != current_default_branch:
            print >>sys.stderr, "Warning: switching branches may cause problems if there are code or data differences. You may need to restart services or reset data."

        # If we're running with jitter, wait for a random amount of time:
        if jitter > 0:
            start_delay = int(jitter * random.random())
            while (start_delay > 0):
                angel.util.process.set_process_title('activate version: waiting %s seconds for jitter' % (start_delay))
                try:
                    time.sleep(1)
                except:
                    raise angel.exceptions.AngelVersionException("Aborted during jitter delay")
                start_delay -= 1

        # Define a run script command (To-do: migrate process helpers to angel and use that here)
        def _run_script(script_path):
            if not os.path.isfile(script_path):
                return
            pid = os.fork()
            if pid:
                (wait_pid, wait_exitcode) = os.waitpid(pid, 0)
                if wait_exitcode != 0 or wait_pid != pid:
                    raise angel.exceptions.AngelVersionException("Failed to run script %s " % script_path +
                                                                 "(exit %s for pid %s)" % (wait_exitcode, wait_pid))
            else:
                env = os.environ.copy()
                env["ANGEL_VERSIONS_DIR"] = self._versions_dir
                os.execve(script_path, (script_path,), env)
                os._exit(2)

        # Trigger pre_activate script, if present:
        pre_activate_script = os.path.join(self.get_path_for_version(branch, version), ".angel", "pre_activate.sh")
        _run_script(pre_activate_script)
        open("%s.receipt" % pre_activate_script, "w").write(str(time.time()))

        # Set the new version to be the default version:
        self.set_default_version_for_branch(branch, version)
        if branch != current_default_branch:
            self.set_default_branch(branch)

        # Mark what version to downgrade to:
        if current_default_version_for_target_branch:
            if branch == current_default_branch and self.is_version_newer(current_default_version_for_target_branch, version):
                # We only do this when upgrading to a newer version on the same branch;
                # cross-branch rollbacks aren't valid and rollbacks that "jump forward" don't make sense.
                self.set_downgrade_version(branch, version, current_default_version_for_target_branch)
        else:
            if current_default_version_for_target_branch:
                print >>sys.stderr, "Not setting downgrade (%s, %s, %s, %s)" %\
                    (version, current_default_version_for_target_branch, branch, current_default_branch)

        # Trigger post_activate script, if present:
        post_activate_script = os.path.join(self.get_path_for_version(branch, version), ".angel", "post_activate.sh")
        _run_script(post_activate_script)
        open("%s.receipt" % post_activate_script, "w").write(str(time.time()))


