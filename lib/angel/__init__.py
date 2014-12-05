
from __future__ import absolute_import

import glob
import grp
import imp
import multiprocessing
import os
import pipes
import pwd
import Queue
import random
import re
import signal
import shlex
import shutil
import socket
import struct
import sys
import textwrap
import traceback
import time

import angel.constants
import angel.exceptions
import angel.settings
import angel.settings.defaults
import angel.versions
from angel.util.pidfile import get_only_running_pids, is_any_pid_running
import angel.util.file
import angel.util.terminal

import devops.process_helpers
import devops.file_and_dir_helpers
from devops.unix_helpers import set_proc_title, hard_kill_all
from devops.logging import log_to_syslog
from devops.logging import log_to_syslog, log_to_wall, log_get_logfile_paths, log_tail_logs
from devops.monitoring import run_status_check
from devops.unix_helpers import set_proc_title, kill_and_wait, get_all_children_of_process


class Angel():

    """ Main class for calls from the command line. """

    _settings = None
    _project_name = None
    _project_base_dir = None
    _project_entry_script = None

    _angel_version_manager = None
    _cached_data = {}  # Use to cache some values, like private IP address, for performance

    def __init__(self, project_name, project_base_dir, project_entry_script, angel_settings):
        """
        @param angel_settings: AngelSettings object that contains config for angel and application services
        """
        self._settings = angel_settings
        self._project_name = project_name
        self._project_base_dir = project_base_dir
        self._project_entry_script = project_entry_script

        # If we are a versioned install, init our version manager:
        if os.path.isfile(os.path.join(self._get_angel_dir(), "versions_dir")):
            try:
                versions_dir = open(os.path.join(self._get_angel_dir(), "versions_dir")).read().rstrip()
                self._angel_version_manager = angel.versions.AngelVersionManager(versions_dir)
            except Exception as e:
                print >>sys.stderr, "Warning: can't create angel version manager (%s)." % e

        # Verify that our required dirs exist:
        owner_user = None
        owner_group = None
        if 'RUN_AS_USER' in self._settings: owner_user = self._settings['RUN_AS_USER']
        if 'RUN_AS_GROUP' in self._settings: owner_group = self._settings['RUN_AS_GROUP']
        for dir_name in ('TMP_DIR', 'LOG_DIR', 'CACHE_DIR', 'DATA_DIR', 'RUN_DIR', 'LOCK_DIR'):
            if dir_name not in self._settings:
                raise angel.exceptions.AngelArgException("Missing %s setting" % dir_name)
            dir_path = self._settings[dir_name]
            if not os.path.exists(dir_path):
                angel.util.file.create_dirs_if_needed(dir_path, owner_user=owner_user, owner_group=owner_group)


    def _get_angel_dir(self):
        """Return the path to the angel control directory, used on versioned installs."""
        return os.path.join(self._project_base_dir, ".angel")


    def get_settings(self):
        """Return the settings object loaded at init time."""
        return self._settings


    def get_setting(self, setting):
        """Return the settings object loaded at init time."""
        return self._settings[setting]


    def get_version_manager(self):
        """Return the version manager for accessing other versions of the project, or None on a non-versioned setup."""
        return self._angel_version_manager


    def is_versioned_install(self):
        """Return true if the project is running out of a versioned (built) install,
        as opposed to a git checkout, etc."""
        return os.path.exists(os.path.join(self.get_project_base_dir(), ".angel", "code_version"))


    def get_usage_as_string(self, args=None, levels=1):
        usage_info = self.get_usage(args=args, levels=levels)
        if usage_info is None:
            return None

        width = 1024
        right_column_offset = 50
        if angel.util.terminal.terminal_width_is_true_size():
            width = angel.util.terminal.terminal_width()
            right_column_offset = int(width*0.3)

        def format_line(label, description, initial_indent, is_option=False):
            description_leadin = ""
            if is_option:
                description_leadin = "  -- "
                label = "[%s]" % label
            return textwrap.fill(label + " "*(right_column_offset - initial_indent - len(label)) +
                                 description_leadin + description,
                                 width,
                                 initial_indent=' '*initial_indent,
                                 subsequent_indent=' '*(right_column_offset + len(description_leadin)),
                                 break_on_hyphens=False)

        def format_string(usage_info, initial_indent, recursion_level):
            if not usage_info:
                return None
            ret_val = ""
            if 0 == recursion_level:
                return ret_val
            if "options" in usage_info:
                if levels == recursion_level and "commands" in usage_info:
                    ret_val += "\n" + " "*initial_indent + "OPTIONS\n"
                for option in sorted(usage_info["options"]):
                    if "hidden" in usage_info["options"][option]:
                        continue
                    label = option
                    try:
                        label = usage_info["options"][option]["label"]
                    except:
                        pass
                    description = "(missing description)"
                    if usage_info["options"][option]:
                        if "description" in usage_info["options"][option]:
                            description = usage_info["options"][option]["description"]
                    ret_val += format_line(label, description, initial_indent, is_option=True) + "\n"

            if "commands" in usage_info:
                if levels == recursion_level and "options" in usage_info:
                    ret_val += "\n" + " "*initial_indent + "COMMANDS\n"
                for command in sorted(usage_info["commands"]):
                    if "hidden" in usage_info["commands"][command]:
                        continue
                    label = command
                    try:
                        label = usage_info["commands"][command]["label"]
                    except:
                        pass
                    if "commands" in usage_info["commands"][command]:
                        label += " ..."
                    description = "(missing description)"
                    if usage_info["commands"][command]:
                        if "description" in usage_info["commands"][command]:
                            description = usage_info["commands"][command]["description"]
                    ret_val += format_line(label, description, initial_indent) + "\n"
                    if recursion_level > 1:
                        child_commands = format_string(usage_info["commands"][command],
                                                       initial_indent + 3,
                                                       recursion_level-1)
                        if child_commands is None:
                            child_commands = " "*initial_indent + "(Error: unable to fetch usage info for %s.)\n" % command
                        ret_val += child_commands

            return ret_val

        preamble_left_col = "%s ..." % self.get_project_name()
        if args:
            preamble_left_col = "%s %s ..." % (self.get_project_name(), ' '.join(args))
        description = ""
        if "description" in usage_info:
            description = usage_info["description"]

        return preamble_left_col + " "*(right_column_offset-len(preamble_left_col)) + description + "\n" + \
               format_string(usage_info, 2, levels)


    def get_usage(self, args=None, levels=1):
        """Return a tree of arg options, at least N levels deep (potentially more than N).

        If args is given, traverses down the options tree for just the given args, and then return N levels.
        (Note that additional parts of the tree may be filled in.)
        If an invalid arg is given, then the response will be None; the caller can loop over this function
        popping args to discover which arg is invalid.

        The return value will be a dict, where values of the dict are either additional dicts with sub-options,
        or a string as the leaf value that is text describing the command.
        Each internal node MAY have a dict value of ".usage" that describes the general category of commands.

        This function will be used for generating auto-complete options for bash, so take care in honoring
        the above constraints.
        """

        if args:
            args = args[:]  # Make a copy so that the value of args in the caller isn't changed

        # Define a function that invokes load_function()s as necessary
        # to traverse through the usage tree -- we'll use this to fill
        # in the parts of the tree that we need visible to meet
        # the args and levels values.
        def load_values(location, recurssion_level):
            if not location:
                return
            if "load_function" in location:
                try:
                    values = location["load_function"]()
                    if values:
                        for k in values:
                            location[k] = values[k]
                except angel.exceptions.AngelArgException as e:
                    print >>sys.stderr, "Error: can't get values (%s)." % e
                    raise(e)
            if recurssion_level > 0:
                if 'commands' in location:
                    for command in location['commands']:
                        load_values(location['commands'][command], recurssion_level-1)

        usage_to_return = self._get_top_of_usage_tree()

        if (args):
            while len(args):
                load_values(usage_to_return, 0)
                arg = args.pop(0)
                if 'commands' not in usage_to_return:
                    raise angel.exceptions.AngelArgException("Don't have any usage info for the '%s' command." % arg)
                if arg not in usage_to_return['commands']:
                    raise angel.exceptions.AngelArgException("No command '%s' found." % arg)
                usage_to_return = usage_to_return['commands'][arg]

        load_values(usage_to_return, levels)
        return usage_to_return


    def _get_top_of_usage_tree(self):

        commands = {}
        options = {}

        options["--duration"] = {
            "description": "Run for N seconds and then exit.",
            "label": "--duration <seconds>",
            ".argcount": 1
        }

        if self._angel_version_manager:

            options["--if-branch"] = {
                "description": "Only run if branch matches (non-zero exit otherwise).",
                "label": "--if-branch <branch>",
                ".argcount": 1,
                "possible_values_function":
                    lambda: self._angel_version_manager.get_available_installed_branches
            }

            options["--if-version"] = {
                "description": "Only run if version number matches (non-zero exit otherwise). When given with '+' any version equal to or great is allowed; likewise when given with '-', any version equal to or less is allowed.",
                "label": "--if-version <version>|<version+>|<version->",
                ".argcount": 1,
                "possible_values_function":
                    # Generate a function that takes all branches and returns a list of names with "+" and "-" variants:
                    lambda: [val for sublist in [(y+'-', y, y+'+')
                                              for y in self._angel_version_manager.get_available_installed_versions()]
                             for val in sublist]
            }

        options["--jitter"] = {
            "description": "Wait randomly up to N number of seconds before running.",
            "label": "--jitter <seconds>",
            ".argcount": 1
        }
        options["--loop-forever"] = {
            "description": "Run the command in a loop forever (exits on Ctrl-C)."
        }
        options["--start-time"] = {
            "description": "Run command at given unix epoch time.",
            ".argcount": 1
        }

        if self._angel_version_manager:
            options["--use-branch"] = {
                "description": "Run using given branch.",
                "label": "--use-branch <branch>",
                ".argcount": 1,
                #"parse_values_func": pop_one,
                "load_function": self._angel_version_manager.get_available_installed_branches
            }

            def _get_versions():
                branch = self.get_project_code_branch()
                # This gets messy real fast. Any auto-complete or help code should be re-execing
                # us with the right branch / version before we get here, if we do this right.
                #if '--use-branch' in args:
                #    try:
                #        branch = args[args.index('--use-branch')+1]
                #    except:
                #        return None
                return self._angel_version_manager.get_available_installed_versions(branch)
            options["--use-version"] = {
                "description": "Run using code for given version. When given with '+' use latest version >= version number (or error); when 'latest', use newest installed version; when 'current', use current default version.",
                "label": "--use-version <version>|<version+>|latest|current",
                "load_function": _get_versions
            }


        # Add "make" command when Makefile is present.
        # Any lines in the file that start with "#    make " will be parsed
        # for "#    make <target>     <one-line description" and added to usage.
        if os.path.isfile(os.path.join(self.get_project_base_dir(), "Makefile")):
            def get_make_commands():
                top_makefile_targets = "grep '^#    make ' '%s'" % \
                                       os.path.join(self.get_project_base_dir(), "Makefile")
                (output, output_stderr, exitcode) = devops.process_helpers.get_command_output(top_makefile_targets)
                if not output or exitcode != 0:
                    return None
                ret_val={"commands": {}}
                for line in output.split("\n"):
                    try:
                        (dummy_hash, dummy_make, target, description) = line.split(None, 3)
                    except:
                        pass
                    ret_val["commands"][target] = {"description": description}
                return ret_val
            commands["make"] = {
                "description": "run make",
                "load_function": get_make_commands,
                "hidden": True
            }


        commands["service"] = {
            "description": "start, stop, and manage services",
            "commands": {
                "autoconf": {
                    "description": "automatically update conf settings using EC2 machine tags (will reload services as needed)"
                },
                "conf": {
                    "description": "edit local config settings; remember to call 'service reload' to apply changes",
                    "commands": {
                        "set": {
                            "description": "set given key(s) in the node's local conf_dir",
                            "label": "set key=value [key2=..]"
                        },
                        "unset": {
                            "description": "if set, remove given key(s) from node's local conf_dir",
                            "label": "unset key [key2..]"
                        }
                    }
                },
                "mode": {
                    "description": "set services to run in normal or maintenance mode",
                    "commands": {
                        "maintenance": {
                            "description": "switch services to maintenance mode (stop accepting public traffic)"
                        },
                        "regular": {
                            "description": "resume accepting regular traffic (switch out of maintenance-mode)"
                        }
                    }
                },
                "reload": {
                    "description": "gracefully reload services to apply config and code changes (ignored by services that don't support it)",
                    "options": {
                        "--code-only": {
                            "description": "only reload code (automatically called after an upgrade)"
                        },
                        "--flush-caches": {
                            "description": "notify services that originating data was potentially changed (so services that cache stuff can reset caches)"
                        },
                        "--skip-conf": {
                            "description": "attempt to preserve current conf during reload (not guaranteed)"
                        }
                    }
                },
                "repair": {
                    "description": "attempt to repair/restart any service in an error state"
                },
                "restart": {
                    "description": "restart all services (services are stopped then started)",
                    "options": {
                        "--wait": {
                            "label": "--wait[=<secs>]",
                            "description": "wait for all services to come up before returning (non-zero exit after <secs>; 600 default)"
                        }
                    }
                },
                "rotate-logs": {
                    "description": "request services to rotate log files over where possible"
                },
                "start": {
                    "description": "start service processes",
                    "options": {
                        "--wait": {
                            "label": "--wait[=<secs>]",
                            "description": "wait for all services to come up before returning (non-zero exit after <secs>; 600 default)"
                        }
                    }
                },
                "stop": {
                    "description": "shutdown all service processes",
                    "options": {
                        "--hard": {
                            "description": "hard kill services without waiting for any in-progress work to be finished"
                        }
                    }
                }
            }
        }


        commands["show"] = {
            "description": "show info about configuration and running services",
            "commands": {
                "logfiles": {
                    "label": "logfiles [<name>..]",
                    "description": "list full paths to log files; with 'name', filter filesnames; use '-name' to exclude, '+name' to AND include"
                },
                "logs": {
                    "label": "logs [<name>..<name>]",
                    "description": "show logging data as it happens; show only logs whose name match; multiple names ORed unless +/-'ed",
                    "options": {
                        "--last": {
                            "label": "--last <n>",
                            "description": "show the last N lines of each file, then exit"
                        },
                        "--show-host": {
                            "description": "include hostname in output"
                        },
                        "--show-names": {
                            "description": "include filenames in output"
                        }
                    }
                },
                "var": {
                    "label": "var <name>",
                    "description": "show given variable, just the value, suitable for backtick expansion"
                },
                "vars": {
                    "label": "vars [name..]",
                    "description": "show all variables, if <name> is given, show only variables whose name contains that string",
                    "options": {
                        "--format": {
                            "label": "--format <suffix>",
                            "description": "export variables in given format (.properties, .py, .sh); Note: list/dict variables will not be included"
                        }
                    }
                }
            }
        }


        commands["status"] = {
            "description": "gather status info about system",
            "options": {
                "--format": {
                    "description": "Generate output suitable for other systems (nagios, collectd, errors-only, silent)"
                },
                "--timeout": {
                    "label": "--timeout=<secs>",
                    "description": "wait at most N seconds for status checks to complete"
                },
                "--wait": {
                    "label": "--wait[=<secs>]",
                    "description": "wait for services to come up before returning (non-zero exit after <secs>; default is 600)"
                }
            },
            "commands": {
                "service": {
                    "description": "only show info for given named service(s)",
                    "possible_values_function": lambda: sorted(self.get_service_names())
                },
                "state": {
                    "description": "only show info for state of services (unexpected running or missing running)"
                }
            }
        }


        commands["package"] = {
            "description": "package management (for package-based installations)",
            "commands": {
                "branch": {
                    "label": "branch <branch>",
                    "description": "switch to given branch",
                    "options": {
                        "--force": {
                            "description": "skip safety checks which switching branches (not recommended)"
                        }
                    }
                },
                #"decommission": {
                #    "description": "permanently stop services and drain all data off the node (for node termination)"
                #},
                "delete": {
                    "label": "delete <version>",
                    "description": "delete the given version",
                    "options": {
                        "--branch": {
                            "label": "--branch <branch>",
                            "description": "delete version for given branch instead of current branch"
                        }
                    }
                },
                "pinning": {
                    "description": "pin the system to the current version and branch (prevents upgrades from being activated)",
                    "commands": {
                        "on": {
                            "description": "pin system to current version",
                            "display": "on [message]"
                        },
                        "off": {
                            "description": "remove version pin"
                        }
                    }
                },
                #"purge": {
                #    "description": "remove unused branches and versions",
                #    "options": {
                #        "--more": {
                #            "description": "clear out additional files where possible (will clear log dir!)"
                #        },
                #        "--keep-newest": {
                #            "label": "--keep-newest N",
                #            "description": "delete versions older than the N most-recently installed ones (defaults to ~7)"
                #        }
                #    }
                #},
                "rollback": {
                    "label": "rollback <version>",
                    "description": "rollback to the prior version before given version (See also: 'upgrade --downgrade-allowed --version=X')"
                },
                "upgrade": {
                    "description": "install and activate the latest version available from repo",
                    "options": {
                        "--branch": {
                            "label": "--branch <branch>",
                            "description": "upgrade and switch to given branch"
                        },
                        "--downgrade-allowed": {
                            "description": "when upgrading: allow switching to an older version"
                        },
                        "--download-only": {
                            "description": "download but do not activate the upgrade"
                        },
                        "--force": {
                            "description": "upgrade regardless of service status"
                        },
                        "--jitter": {
                            "label": "--jitter[=<seconds>]",
                            "description": "randomly wait up to N seconds before running upgrade (default is 120)"
                        },
                        "--skip-reload": {
                            "description": "upgrade the system but do not reload any running services",
                        },
                        "--version": {
                            "label": "--version <version>",
                            "description": "upgrade to given version instead of newest (use '--version highest-installed' for newest locally available)"
                        },
                        "--wait": {
                            "label": "--wait[=<seconds>]",
                            "description": "wait for services to report ok before returning (non-zero exit after <secs>; default is 600)"
                        }
                    }
                },
                "add-version": {
                    "description": "<versions-dir> <path-to-src> <branch> <version>"
                },
                "check-version": {
                    "label": "--version <version> [--branch <branch>]",
                    "description": "check if given version is installed and available"
                },
                "versions": {
                    "description": "list all locally-available branches and versions"
                }
            }
        }


        def load_tools_usage():
            ret_val = {"commands": {}}
            for service in sorted(self.get_service_names()):
                service_obj = self.get_service_object_by_name(service)
                if service_obj is None:
                    print >>sys.stderr, "Warning: unknown service '%s'." % service
                    continue
                service_description = '(Missing description; service %s missing docstring.)' % service
                if service_obj.__doc__ is not None:
                    service_description = service_obj.__doc__.lstrip().split('\n')[0].lstrip().rstrip()
                ret_val['commands'][service.replace("_", "-")] = {
                    "description": service_description,
                    "load_function": service_obj.get_usage_for_tools
                }
            return ret_val
        commands["tool"] = {"description": "tools for individual services",
                            "load_function": load_tools_usage}


        commands["version"] = {
            "description": "show version info"
        }


        commands["help"] = {
            "description": "show command options and help",
            "display": "help [args..]",
            "options": {
                "--levels": {
                    "description": "Number of levels deep to show help"
                }
            }
        }


        def load_usage_for_bin(bin_path):
            return {
                "description": "Bin usage not implemented (%s)" % bin_path,
                "hidden": True
            }

        bin_command_prefix = '%s-' % self.get_project_name()
        for relative_bin_dir_path in self.get_settings()['BIN_PATHS'].split(':'):
            full_bin_dir_path = os.path.join(self.get_project_base_dir(), relative_bin_dir_path)
            if not os.path.isdir(full_bin_dir_path):
                continue
            for bin_file in os.listdir(full_bin_dir_path):
                if bin_file.startswith(bin_command_prefix):
                    command_name = bin_file[len(bin_command_prefix):]
                    if command_name in commands:
                        continue  # First occurrence in path takes precedence
                    commands[command_name] = {
                        "load_function": lambda: load_usage_for_bin(os.path.join(full_bin_dir_path, bin_file))
                    }

        return {
            "description": "%s command help for branch %s, version %s" % (self.get_project_name(),
                                                                          self.get_project_code_branch(),
                                                                          self.get_project_code_version()),
            "commands": commands,
            "options": options
        }



    def run(self, args):
        """ Run angel and return the exit code for the given command. Use exec_args when you don't care
        about getting control back; exec_args will avoid an extra parent process running.
        :param args: command line args (or equivalent) to use, including whatever --options we're given.
        :return:
        """
        try:
            pid = os.fork()
        except:
            print >>sys.stderr, "Error: run() unable to fork (out of resources?)"
            return -2
        if pid:
            (wait_pid, wait_exitcode) = os.waitpid(pid, 0)
            if wait_pid != pid:
                print >>sys.stderr, "Waitpid != pid (%s vs %s)" % (wait_pid, pid)
                return -1
            return wait_exitcode
        else:
            self.exec_args(args)
            os._exit(2)  # Shouldn't be reachable


    def exec_args(self, args):
        """Run angel with the given command line args, including whatever --option flags we're given.
        Use run() instead of exec_args if you need control returned to you.
        """

        # If no args given, spit out usage:
        if args is None or 0==len(args):
            print >>sys.stderr, self.get_usage_as_string()
            sys.exit(1)

        # Open project_base_dir in read mode so that there's an active FD open to this version of our code,
        # for "in use" checks in our versioning system:
        if self._angel_version_manager:
            os.open(self.get_project_base_dir(), os.O_RDONLY)

        # For prod, we shove the deploy login into an ENV variable that
        # should have made its way all the way through to here.
        if args:
            command_string_for_logging = "%s %s" % (self.get_project_name(), ' '.join(args))
            user_log_info = 'Running command as uid %s: ' % (os.getuid())
            if 'LC_DEPLOY_USER' in os.environ:
                user_log_info = 'Remote deploy user %s running command as uid %s: ' % \
                                (os.environ['LC_DEPLOY_USER'], os.getuid())
                log_to_wall(os.environ['LC_DEPLOY_USER'], command_string_for_logging)
                del os.environ['LC_DEPLOY_USER']
            log_to_syslog("%s%s" % (user_log_info, command_string_for_logging))
            set_proc_title(command_string_for_logging)

        # We wrap _run, so that our setup logic is separate and so we can catch arg exceptions here:
        try:
            exit_code = self._run(args[:])
            if exit_code is None:
                print >>sys.stderr, "Error: run() returned no exit code."
                exit_code = 2
            try:
                sys.stdout.flush()  # Must flush stdout/stderr -- os._exit won't do this for us
            except:
                # We can get IOError if the stdout/stderr is a pipe that's dead
                pass
            try:
                sys.stderr.flush()
            except:
                pass
            os._exit(exit_code)  # Can't use sys.exit() -- if run() is using multiprocess, it blows up

        except angel.exceptions.AngelArgException as e:
            print >>sys.stderr, "Error: %s" % e
            while len(args):
                if args[0] == "help":
                    # This is a weird edge case: calling help on a subcommand that doesn't exist
                    # will cause us to see "help" as the command, step around it.
                    args=args[1:]
                try:
                    print >>sys.stderr, self.get_usage_as_string(args=args[:])
                    sys.exit(1)
                except:
                    args.pop()

        # This shouldn't be reachable:
        sys.exit(3)


    def _run(self, args):

        is_interactive = 'TERM' in os.environ
        opt_duration = None
        opt_if_branch = None
        opt_if_version = None
        opt_jitter = None
        opt_start_time = None
        opt_use_branch = None
        opt_use_version = None
        opt_loop_forever = None

        category = args.pop(0)

        while category.startswith('--'):
            if category == '--duration':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing duration')
                opt_duration = args.pop(0)

            elif category == '--if-branch':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing --if-branch value')
                opt_if_branch = args.pop(0)

            elif category == '--if-version':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing --if-version value.')
                opt_if_version = args.pop(0)

            elif category == '--jitter':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing jitter value.')
                opt_jitter = args.pop(0)

            elif category == '--start-time':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing start time.')
                opt_start_time = args.pop(0)

            elif category == '--use-branch':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing branch name.')
                opt_use_branch = args.pop(0)

            elif category == '--use-version':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing version.')
                opt_use_version = args.pop(0)

            elif category == '--loop-forever':
                opt_loop_forever = True

            elif category == '--skip-make':
                pass  # This option is used by our control script, before we're set up.

            else:
                raise angel.exceptions.AngelArgException("unknown option '%s'." % category)

            if not len(args):
                raise angel.exceptions.AngelArgException("missing command.")
            category = args.pop(0)

        command_string_for_logging = ' '.join(args)
        if len(command_string_for_logging) > 40:
            command_string_for_logging = command_string_for_logging[:32] + "..."

        if opt_use_version is None and opt_use_branch == self.get_project_code_branch():
            # Then this is a no-op; skip version checks entirely so that
            # --use-branch <current> works in non-versioned installs
            opt_use_branch = None

        if opt_use_branch is not None or opt_use_version is not None:
            if opt_use_version is not None:
                if opt_use_branch is None or opt_use_branch == self.get_project_code_branch():
                    if opt_use_version == self.get_project_code_version():
                        # The this is a no-op (version and branch requested are equal to us),
                        # skip any version switching so we work in non-versioned installs:
                        opt_use_branch = None
                        opt_use_version = None

        if opt_use_branch is not None or opt_use_version is not None:
            if self._angel_version_manager is None:
                print >>sys.stderr, "Error: versions aren't supported on this install."
                return 1

            if opt_use_branch is None:
                opt_use_branch = self.get_project_code_branch()
            try:
                if opt_use_version is None or opt_use_version == 'current':
                    opt_use_version = self._angel_version_manager.get_default_version(opt_use_branch)
                elif opt_use_version == 'latest':
                    opt_use_version = self._angel_version_manager.get_highest_installed_version_number(opt_use_branch)
                else:
                    is_minimum_version_check = False
                    if opt_use_version[-1] == '+':
                        is_minimum_version_check = True
                        opt_use_version = opt_use_version[:-1]
                    if is_minimum_version_check:
                        highest_available_version = self._angel_version_manager.get_highest_installed_version_number(opt_use_branch)
                        if self._angel_version_manager.is_version_newer(highest_available_version, opt_use_version):
                            print >>sys.stderr, "Error: can't find recent enough version (available: %s; minimum requested: %s)." % \
                                                (highest_available_version, opt_use_version)
                            return 1
                        opt_use_version = highest_available_version
            except Exception as e:
                print >>sys.stderr, "Error: unable to support --use-branch/--use-version %s/%s (%s)." % \
                                    (opt_use_branch, opt_use_version, e)
                return 1

            if opt_use_branch != self.get_project_code_branch() or opt_use_version != self.get_project_code_version():
                args = (category,) + tuple(args)
                if opt_start_time is not None:
                    args = ('--start-time', opt_start_time) + args
                if opt_duration is not None:
                    args = ('--duration', opt_duration) + args
                if opt_jitter is not None:
                    args = ('--jitter', opt_jitter) + args
                if opt_loop_forever:
                    args = ('--loop-forever',) + args
                # Run correct version (returns if the branch/version doesn't exist or can't be executed):
                try:
                    self._angel_version_manager.exec_command_with_version(opt_use_branch,
                                                                          opt_use_version,
                                                                          self._project_entry_script,
                                                                          args)
                except angel.exceptions.AngelExpectedException as e:
                    print >>sys.stderr, "Error in exec: %s" % e
                return 2  # Return 1 because exec should never return

        if opt_if_branch is not None:
            if opt_if_branch.lower() != self.get_project_code_branch().lower():
                print >>sys.stderr, "Not running (--if-branch %s doesn't match current branch %s)." %\
                                    (opt_if_branch, self.get_project_code_branch())
                return 0

        if opt_if_version is not None:
            or_older = False
            or_newer = False
            if opt_if_version.endswith('+'):
                or_newer = True
                opt_if_version = opt_if_version[:-1]
            if opt_if_version.endswith('-'):
                or_older = True
                opt_if_version = opt_if_version[:-1]
            desired_version = opt_if_version
            if or_older:
                if self._angel_version_manager.is_version_newer(desired_version, self.get_project_code_version()):
                    print >>sys.stderr, "Aborting (--if-version: running version %s is newer than %s)." % (self.get_project_code_version(), desired_version)
                    return 1
            elif or_newer:
                if self._angel_version_manager.is_version_newer(self.get_project_code_version(), desired_version):
                    print >>sys.stderr, "Aborting (--if-version: running version %s is older than %s)." % (self.get_project_code_version(), desired_version)
                    return 1
            else:
                if not self.get_project_code_version() == desired_version:
                    print >>sys.stderr, "Aborting (--if-version: running version %s isn't equal to %s)." % (self.get_project_code_version(), desired_version)
                    return 1

        if opt_start_time:
            try:
                seconds_until_start_time = int(opt_start_time) - int(time.time())
            except:
                print >>sys.stderr, "Error: invalid start time '%s'. Use Unix epoch format." % opt_start_time
                return 1
            if seconds_until_start_time < 0:
                print >>sys.stderr, "Warning: start time in past (%s seconds ago?); starting now." % seconds_until_start_time
                seconds_until_start_time = 0
            else:
                log_to_syslog("Delayed start: waiting %s seconds (%s)" % (seconds_until_start_time, command_string_for_logging))
                try:
                    time.sleep(seconds_until_start_time)
                    log_to_syslog("Delayed start: now running (%s)" % command_string_for_logging)
                except Exception as e:
                    log_to_syslog("Delayed start: error while waiting; bailing. (%s)" % command_string_for_logging)
                    print >>sys.stderr, "Error: exception during pre-start wait (%s); bailing." % e
                    return 1
                except KeyboardInterrupt:
                    return 1

        if opt_jitter is not None:
            jitter = 0
            try:
                jitter = float(opt_jitter) * random.random()
            except:
                print >>sys.stderr, "Error: invalid jitter '%s'." % opt_jitter
                return 1
            try:
                while jitter > 0:
                    set_proc_title('%s seconds jitter before %s command' % (jitter, category))
                    sleep_time = 1
                    if jitter < sleep_time:
                        sleep_time = jitter
                    jitter -= sleep_time
                    time.sleep(sleep_time)
            except:
                print >>sys.stderr, "Error: canceled during jitter wait."
                return 1

        if opt_duration is not None:
            try:
                duration_to_run = int(opt_duration)
            except:
                print >>sys.stderr, "Error: invalid duration '%s'." % opt_duration
                return 1
            log_to_syslog("Start command with max duration of %s seconds (%s)" % (duration_to_run, command_string_for_logging))
            kill_after_x_seconds(duration_to_run)

        # If looping forever, fork and let the child process run the job:
        if opt_loop_forever:
            loop_start_time = time.time()
            loop_count = 0
            error_count = 0
            sleep_amount = 0
            while True:
                loop_count+=1
                try:
                    child_pid = os.fork()
                    if 0 == child_pid:
                        break  # skip rest of opt_loop_forever and let the child call run_category below
                    (wait_pid, exitcode) = os.wait()
                    if wait_pid != child_pid or exitcode != 0:
                        print >>sys.stderr, "Warning: run %s (pid %s) returned exit code %s." % \
                                            (loop_count, child_pid, exitcode/256)
                        error_count += 1

                    time.sleep(0.1)  # Sleep for a tenth of a second, so we don't spin like mad on a very-fast command
                    sleep_amount += 0.1

                except Exception as e:
                    print >>sys.stderr, "Warning: run %s (pid %s) threw an exception (%s)." % \
                                            (loop_count, child_pid, e)
                    time.sleep(2)
                    sleep_amount += 2

                except KeyboardInterrupt:
                    loop_end_time = time.time()
                    time_per_loop = (loop_end_time - loop_start_time - sleep_amount) / loop_count
                    print >>sys.stderr, "^C\n%s runs, %s errors, %.03f seconds per run." % (loop_count, error_count, time_per_loop)
                    if error_count:
                        sys.exit(1)
                    sys.exit(0)

        # Now that we've handled all --options, run the actual command:
        return self._run_command(category, args[:], is_interactive=is_interactive)


    def _run_command(self, category, args=(), is_interactive=False):

        if category == "help":
            levels = 1
            if len(args) and args[0] == "--levels":
                try:
                    args.pop(0)
                    levels = int(args.pop(0))
                except:
                    raise angel.exceptions.AngelArgException("Invalid levels value")
            usage_str = self.get_usage_as_string(args=args, levels=levels).rstrip()
            if not usage_str:
                raise angel.exceptions.AngelArgException("Invalid help args")
            print "\n" + usage_str + "\n"
            return 0

        if category == "tab-completion-options":
            usage_info = self.get_usage(args=args, levels=1)
            if not usage_info:
                return 1
            print ' '.join(usage_info)
            return 0

        if category == 'version':
            if len(args):
                print >>sys.stderr, "Unknown version options."
                return 1
            # For debian package installs, "version" MUST print "<branch-name> <build-number>" and exit
            # regardless of settings / conf stuff for package installation to work.
            print "%s %s" % (self.get_project_code_branch(), self.get_project_code_version())
            return 0

        elif category == 'cluster':
            if len(args) == 0:
                raise angel.exceptions.AngelArgException('missing cluster service.')
            command = args.pop(0)

            if command == 'conf':
                if len(args) == 0:
                    raise angel.exceptions.AngelArgException('missing cluster conf command.')
                action = args.pop(0)

                if action == 'list':
                    print >>sys.stderr, "To-do: list dynamic settings"
                    return 1

                if action == 'unset':
                    if len(args) != 1: raise angel.exceptions.AngelArgException('missing unset arguments.')
                    setting_name = args.pop(0)
                    print >>sys.stderr, "To-do: unset %s" % setting_name
                    return 1

                if action == 'set':
                    if not len(args): raise angel.exceptions.AngelArgException('missing set arguments.')
                    setting_name = args.pop(0)
                    setting_value = None
                    if len(args) == 1:
                        setting_value = args.pop(0)
                    else:
                        if setting_name.find('=') > 0:
                            (setting_name, setting_value) = setting_name.split('=', 1)
                    if setting_value is None or setting_name.find('=') > 0:
                        raise angel.exceptions.AngelArgException('missing set value; strings should be quoted.')
                    print >>sys.stderr, "To-do: set %s to %s" % (setting_name, setting_value)
                    return 1

            raise angel.exceptions.AngelArgException("unknown cluster command '%s'." % command)


        elif category == 'make':
            makefile = os.path.join(self.get_project_base_dir(), "Makefile")
            if os.path.exists(makefile):
                return devops.process_helpers.exec_process("/usr/bin/make", args=args, chdir_path=self.get_project_base_dir())
            else:
                print >>sys.stderr, "Error: no Makefile at %s." % makefile
                return 1


        elif category == 'package':

            if 0 != os.getuid():
                print >>sys.stderr, "Warning: package command may fail without root access."

            if not len(args):
                raise angel.exceptions.AngelArgException('missing action.')
            verb = args.pop(0)


            if verb == 'add-version':
                versions_dir = None
                src_path = None
                branch = None
                version = None
                sleep_ratio = 0.1
                try:
                    while len(args):
                        if args[0] == "--sleep-ratio":
                            args.pop(0)
                            sleep_ratio = float(args.pop(0))
                        else:
                            versions_dir = args.pop(0)
                            src_path = args.pop(0)
                            branch = args.pop(0)
                            version = args.pop(0)
                except:
                    raise angel.exceptions.AngelArgException('<versions dir> <src path> <branch name> <version>')
                vm = angel.versions.AngelVersionManager(versions_dir)
                vm.add_version(branch, version, src_path, sleep_ratio=sleep_ratio)
                return 0


            elif verb == "check-version":
                branch=self.get_project_code_branch()
                silent=False
                version=None
                try:
                    while len(args):
                        arg = args.pop(0)
                        if arg == "--version":
                            version = args.pop(0)
                        elif arg == "--silent":
                            silent = True
                        elif arg == "--branch":
                            branch = args.pop(0)
                        else:
                            raise angel.exceptions.AngelArgException("Unknown arg '%s'" % arg)
                except:
                    raise angel.exceptions.AngelArgException("Invalid args")
                if self._angel_version_manager is None:
                    if not silent:
                        print "Branch %s version %s: not a versioned install." % (branch, version)
                    return 1
                if self._angel_version_manager.is_version_installed(branch, version):
                    if not silent:
                        print "Branch %s version %s ok." % (branch, version)
                    return 0
                else:
                    if not silent:
                        print "Branch %s version %s missing." % (branch, version)
                    return 1


            # The remaining package commands require running from a versioned setup; we exclude some above
            # as that those commands can be used to generate a new versioned setup.
            if self._angel_version_manager is None and verb != "add-version":
                print >>sys.stderr, "Error: not a versioned install."
                return 1


            if verb == 'branch':
                force = False
                branch = None
                version = None
                while len(args):
                    opt = args.pop(0)
                    if opt == '--force':
                        force = True
                    elif branch is None:
                        branch = opt
                    else:
                        raise angel.exceptions.AngelArgException('unknown option "%s".' % opt)
                if branch is None:
                    raise angel.exceptions.AngelArgException('missing branch name.')
                self.activate_version_and_reload_code(branch, version, force=force)
                return 0


            elif verb == 'decommission':
                return self.decommission()


            elif verb == 'delete':
                version = None
                branch = None
                while len(args):
                    opt = args.pop(0)
                    if opt == '--branch':
                        branch = args.pop(0)
                    else:
                        if version is None:
                            version = opt
                        else:
                            raise angel.exceptions.AngelArgException("Unknown option '%s'" % opt)
                if version is None:
                    raise angel.exceptions.AngelArgException('Missing version to delete')
                if branch is None:
                    branch = self.get_project_code_branch()
                return self._angel_version_manager.delete_version(branch, version)


            elif verb == 'rollback':
                if len(args) != 1:
                    raise angel.exceptions.AngelArgException('missing version to downgrade from.')
                downgrade_from_version = args.pop(0)

                if downgrade_from_version != self.get_project_code_version():
                    # This might seem confusing, but it's correct. We rollback *from* a given version.
                    # The prior running version is tracked as part of the current running version; otherwise
                    # in a cluster setup where various nodes might not have yet rolled forward, some nodes will
                    # accidentally get downgraded a version further than expected.
                    # We guard against this by defining downgrades as coming "from" a certain version,
                    # as opposed to "to" a version.
                    print >>sys.stderr, "Error: can't rollback from version %s; node is running a different version (%s)." % \
                                        (downgrade_from_version, self.get_project_code_version())
                    return 1
                downgrade_to_version = self._angel_version_manager.get_downgrade_version(self.get_project_code_branch(),
                                                                                         downgrade_from_version)
                self.activate_version_and_reload_code(self.get_project_code_branch(), downgrade_to_version, downgrade_allowed=True)
                return 0


            elif verb == 'upgrade':
                branch = None
                downgrade_allowed = False
                download_only = False
                force = False
                jitter = 0
                skip_reload = False
                version = None
                wait_for_ok = False         # After upgrade, if running, do we wait for all services to show status OK before returning?
                wait_for_ok_timeout = 600   # Number of seconds to wait for ok-after-upgrade before returning; if we timeout, non-zero exit
                while len(args):
                    try:
                        opt = args.pop(0)
                        if opt == '--branch':
                            branch = args.pop(0)
                        elif opt == '--downgrade-allowed':
                            downgrade_allowed = True
                        elif opt == '--download-only':
                            download_only = True
                        elif opt == '--force':
                            force = True
                        elif opt[:8] == '--jitter':
                            jitter = 120
                            if (len(opt) > 8):
                                try:
                                    jitter = int(opt[9:])
                                except:
                                    raise angel.exceptions.AngelArgException('--jitter=<seconds> requires a number')
                        elif opt == '--skip-reload':
                            skip_reload = True
                        elif opt == '--version':
                            version = args.pop(0)
                        elif opt[:6] == '--wait':
                            wait_for_ok = True
                            if len(opt) > 7:
                                try:
                                    wait_for_ok_timeout = int(opt[7:])
                                except:
                                    raise angel.exceptions.AngelArgException('--wait=<seconds> requires a number')
                        else:
                            raise angel.exceptions.AngelArgException('unknown option "%s".' % opt)
                    except IndexError:
                        raise angel.exceptions.AngelArgException('missing value.')

                if branch is None:
                    branch = self.get_project_code_branch()

                if version == 'latest':
                    version = None

                if version == 'highest-installed':
                    try:
                        version = self._angel_version_manager.get_highest_installed_version_number(branch)
                    except:
                        print >>sys.stderr, "Error: no versions for branch '%s' are installed." % branch
                        return 1

                if download_only:
                    if wait_for_ok:
                        print >>sys.stderr, "Warning: Ignoring --wait option; doesn't apply with --download-only."
                        wait_for_ok = False

                self.activate_version_and_reload_code(branch, version,
                                                      force=force,
                                                      jitter=jitter,
                                                      skip_reload=skip_reload,
                                                      downgrade_allowed=downgrade_allowed,
                                                      download_only=download_only,
                                                      wait_for_ok=wait_for_ok,
                                                      wait_for_ok_timeout=wait_for_ok_timeout)
                return 0


            elif verb == 'pinning':
                if not len(args): raise angel.exceptions.AngelArgException('no pinning option given.')
                action = args.pop(0)
                reason = None
                if len(args):
                    reason = args.pop(0)
                if len(args): raise angel.exceptions.AngelArgException("unknown arguments '%s'." % args)
                if action == 'on':
                    try:
                        self._angel_version_manager.pin_version(reason)
                        pinned_branch = self._angel_version_manager.get_default_branch()
                        pinned_version = self._angel_version_manager.get_default_version(pinned_branch)
                        print "System pinned to branch %s, version %s." % (pinned_branch, pinned_version)
                    except Exception as e:
                        print >>sys.stderr, "Error: unable to pin version (%s)." % e
                        return 1

                elif action == 'off':
                    if not self._angel_version_manager.is_version_pinned():
                        print >>sys.stderr, "System version isn't pinned; ignoring."
                        return 0
                    try:
                        self._angel_version_manager.unpin_version()
                        return 0
                    except Exception as e:
                        print >>sys.stderr, "Error: unable to unpin version (%s)." % e
                        return 1

                else:
                    raise angel.exceptions.AngelArgException("unknown pinning option '%s'." % action)


            elif verb == 'versions':
                default_branch = self._angel_version_manager.get_default_branch()
                branches = self._angel_version_manager.get_available_installed_branches()
                longest_branch_name_len = max(len("Branch"), max(map(len,branches)))
                longest_version_number = len("Version")
                install_time_len = 19  # because: "2014-09-02 20:02:24"
                max_width = angel.util.terminal.terminal_width()
                for branch in sorted(branches):
                    default_version_for_branch = self._angel_version_manager.get_default_version(branch)
                    versions = self._angel_version_manager.get_available_installed_versions(branch)
                    for version in versions:
                        if len(version) > longest_version_number:
                            longest_version_number = len(version)

                print "%s  %s  %s  %s" % ("Branch".ljust(longest_branch_name_len),
                                          "Version".ljust(longest_version_number),
                                          "Install Time".ljust(install_time_len),
                                          "State")
                print "%s  %s  %s  %s" % ("-"*longest_branch_name_len,
                                          "-"*longest_version_number,
                                          "-"*install_time_len,
                                          "-"*(max_width - longest_branch_name_len - longest_version_number - install_time_len - 6))

                for branch in sorted(branches):
                    default_version_for_branch = self._angel_version_manager.get_default_version(branch)
                    versions = self._angel_version_manager.get_available_installed_versions(branch)
                    for version in sorted(versions):

                        path = self._angel_version_manager.get_path_for_version(branch, version)
                        install_time = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(os.stat(path).st_mtime))

                        print "%s  %s  %s " % (branch.ljust(longest_branch_name_len),
                                              version.ljust(longest_version_number),
                                              install_time.ljust(install_time_len)),
                        sys.stdout.flush()

                        notes = []
                        is_running = self._angel_version_manager.is_version_in_use_by_processes(branch, version)
                        is_unused = not self._angel_version_manager.is_version_in_use(branch, version)
                        if is_running:
                            notes += ["in-use"]
                        else:
                            if is_unused:
                                notes += ["unused"]

                        if default_version_for_branch == version:
                            if default_branch == branch:
                                notes += ["system-default"]
                                if self._angel_version_manager.is_version_pinned():
                                    notes += ["pinned (%s)" % self._angel_version_manager.get_version_pinned_reason()]
                            else:
                                notes += ["branch-default"]

                        if self.get_project_code_version() == version and self.get_project_code_branch() == branch:
                            notes += ["this-version"]

                        print ', '.join(notes)

                return 0

            else:
                raise angel.exceptions.AngelArgException("unknown action '%s'." % verb)


        elif category == 'tool':
            if len(args) == 0:
                raise angel.exceptions.AngelArgException('missing tool service.')
            service = args.pop(0)
            parent_service = service.split('-')[0]  # Only useful if we're a sub-service
            if len(args) == 0:
                raise angel.exceptions.AngelArgException('missing tool command.')
            else:
                command = args.pop(0)

            # Tool commands can be implemented as functions named shell_tool_XXX in the service's python class,
            # or as executable commands stored under ./services/<service>/bin/<projectname>-<service>-<command> and ./services/<service>/server/bin.
            # This naming setup allows us to keep a consistent set of commands but change the implementation as needed.

            arg_copy = list(args)

            # First, check if we have a shell_tool_XXX function():
            try:
                tool_args = []
                tool_kwargs = {}
                pass_remaining_args_unexamined = False
                while len(arg_copy):
                    name = arg_copy.pop(0)
                    # If we see a magic '--' flag, we flip to a "don't process anything else past this point; pass it in as args" mode.
                    if pass_remaining_args_unexamined:
                        tool_args.append(name)
                        continue
                    if name == '--':
                        pass_remaining_args_unexamined = True
                        continue
                    if name[0:2] == '--':
                        name = name[2:]
                        if name.find('=') > 0:
                            # --value=key
                            name,value = name.split('=',1)
                        else:
                            # --value key
                            if not len(arg_copy):
                                # Then last option in arg_copy w/o a value, it's a flag:
                                value = True
                            else:
                                # Then more options; check if next one starts with '--' in which case this one is a flag:
                                if arg_copy[0][0:2] == '--':
                                    value = True
                                else:
                                    value = arg_copy.pop(0)

                        # Cast true/false strings to bools so we can negate them for --no-flags:
                        if isinstance(value, str):
                            if value.lower() == 'false': value = False
                            elif value.lower() == 'true': value = True

                        # Support for --no-key flag; i.e. if "--flag" is true, then "--no-flag" is false:
                        if isinstance(value, bool) and name[:3] == 'no-':
                            value = not value
                            name = name[3:]

                        tool_kwargs[name.replace('-','_')] = value
                    else:
                        tool_args.append(name)

                service_obj = self.get_service_object_by_name(service)
                if service_obj is None:
                    raise angel.exceptions.AngelArgException("unknown service '%s'." % service)

                # Check that the requested tool isn't in the DISABLED_TOOLS list:
                disable_tools = getattr(service_obj, 'DISABLED_TOOLS')
                if command in disable_tools:
                    print >>sys.stderr, "Error: tool %s exists but is disabled; check that you're using the correct devops name." % command
                    return 1

                tool_func_name = 'shell_tool_%s' % command.replace('-','_') # Python can't have '-' in functions, map them to '_' automatically
                tool_func = getattr(service_obj, tool_func_name)

                # Inspect service class for a python function that defines the tool:
                import inspect
                argspec = inspect.getargspec(tool_func)
                if argspec.defaults is not None:
                    kwdefaults = zip( argspec.args[-len(argspec.defaults):], argspec.defaults )  # (required1,required2,optionalA,optionalB),(A,B) -> ((optionalA,A), (optionalB,B))
                    for kwdefault_name, kwdefault_value in kwdefaults:
                        if kwdefault_name in tool_kwargs and type(kwdefault_value) is not type(None):
                            if type(kwdefault_value) is bool and type(tool_kwargs[kwdefault_name]) is not bool:
                                # We convert the case-insensative strings 'True' and 'False' strings to bools in the parser, up above; so if we don't have a bool here, then it's a bad input.
                                raise angel.exceptions.AngelArgException("bad value '%s' for option --%s (requires 'true' or 'false')." %\
                                               (tool_kwargs[kwdefault_name], kwdefault_name))
                            try:
                                tool_kwargs[kwdefault_name] = type(kwdefault_value)(tool_kwargs[kwdefault_name])  # Take our existing key-value and cast value to be the type in default key-value
                            except:
                                raise angel.exceptions.AngelArgException("bad value '%s' for option --%s (requires an %s)." %\
                                               (tool_kwargs[kwdefault_name], kwdefault_name, type(kwdefault_value)))
                    normalized_tool_kwargs = {}
                    for tool_kwarg in tool_kwargs:
                        if tool_kwarg in dict(kwdefaults):
                            normalized_tool_kwargs[tool_kwarg] = tool_kwargs[tool_kwarg]
                        else:
                            # We do a few standard substitutions, to support standard command-line conventions.
                            if tool_kwarg.startswith('with_') and 'without_%s' % tool_kwarg[5:] in dict(kwdefaults):
                                normalized_tool_kwargs['without_%s' % tool_kwarg[5:]] = not tool_kwargs[tool_kwarg]
                            elif tool_kwarg.startswith('without_') and 'with_%s' % tool_kwarg[8:] in dict(kwdefaults):
                                normalized_tool_kwargs['with_%s' % tool_kwarg[8:]] = not tool_kwargs[tool_kwarg]
                            else:
                                raise angel.exceptions.AngelArgException("unknown option '--%s'; use '%s tool %s help %s' for usage info." % (tool_kwarg, self.get_project_name(), service, command))
                    tool_kwargs = normalized_tool_kwargs
                try:
                    ret_val = tool_func(*tool_args, **tool_kwargs)
                    if ret_val is None:
                        print >>sys.stderr, "Error: %s didn't return an exit code." % (tool_func_name)
                        return 1
                    if type(ret_val) == bool:
                        if ret_val is True:
                            return 0
                        else:
                            return 1
                    try:
                        ret_val = int(ret_val)
                    except Exception as e:
                        print >>sys.stderr, "Warning: tool didn't return an int (%s: %s)" % (ret_val, e)
                        return 0
                    return ret_val
                except TypeError as e:
                    trace = traceback.format_exc(sys.exc_info()[2])
                    if 'takes at least' not in trace:
                        print >>sys.stderr, "Error: unable to run 'tool %s %s [...]' (%s)." % (service, command, e)
                        print >>sys.stderr, trace
                    else:
                        print >>sys.stderr, "Error: invalid command-line options (%s)." % (e)
                        print >>sys.stderr, "Usage: %s ..." % self.get_command_for_running(args=("tool", service))
                        service_obj.shell_tool_help(command=command, print_to_stderr=True)
                    return 1
                except Exception as e:
                    print >>sys.stderr, "Error: exception thrown while running %s tool: %s (%s)" % (command, e, e.__class__)
                    print >>sys.stderr, traceback.format_exc(sys.exc_info()[2]),
                    return 1
                except:
                    print >>sys.stderr, "Error: unknown except caught while running %s tool." % (command)
                    print >>sys.stderr, traceback.format_exc(sys.exc_info()[2])
                    return 1

            except AttributeError:
                # This exception occurs when the function doesn't exist -- continue onto checking for bin dir scripts
                pass
            except Exception as e:
                print >>sys.stderr, "Error: exception running %s command %s: %s" % (service, command, e)
                print >>sys.stderr, traceback.format_exc(sys.exc_info()[2])
                return 2

            # If not shell tool function, then check if we have an executable.
            # We do this after checking for functions so that functions can interpose on executables.
            # See ./services/README.txt for an explanation of the following paths.
            possible_paths = (
                os.path.join(self.get_project_base_dir(), 'services', service,        'bin', "%s-%s" % (service,command) ),
                os.path.join(self.get_project_base_dir(), 'services', parent_service, 'bin', "%s-%s" % (service,command) ),
                os.path.join(self.get_project_base_dir(), 'services', parent_service, 'bin', "%s-%s" % (parent_service,command) ),
                os.path.join(self.get_project_base_dir(), 'services', service,        'server', 'bin', command),
                os.path.join(self.get_project_base_dir(), 'services', parent_service, 'server', 'bin', command),
                os.path.join(self.get_project_base_dir(), 'built', 'services', service,        'server', 'bin', command),
                os.path.join(self.get_project_base_dir(), 'built', 'services', parent_service, 'server', 'bin', command)
                )
            tool_exec = None
            for p in possible_paths:
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    tool_exec = p
                    break
            if tool_exec:
                # Export settings to a simple key=value tmp file that tools can read:
                # (Done so that tools don't have to figure out the logic for loading all the settings input points.)
                os.environ['%s_SETTINGS' % self.get_project_name().upper()] = \
                    self.get_settings().export_settings_to_tmpfile()

                existing_python_path = ''
                if 'PYTHONPATH' in os.environ:
                    existing_python_path = ':%s' % os.environ['PYTHONPATH']
                os.environ['PYTHONPATH'] = '%s/share/python:%s/code/app-server/src%s' % (self.get_project_base_dir(),
                                                                                         self.get_project_base_dir(),
                                                                                         existing_python_path)
                try:
                    os.execv(tool_exec, [tool_exec] + list(args))
                except Exception as e:
                    print >>sys.stderr, "Error: unable to run %s: %s" % (tool_exec, e)
                return 1

            print >>sys.stderr, "Error: can't find tool '%s'. Try: %s help tool %s" % (command, self.get_project_name(), service)


        elif category == 'service':

            if self.is_decommissioned():
                print >>sys.stderr, "Error: system is decommissioned; service commands not supported."
                return 1

            self.verify_user_is_root_or_cmd_user()

            if not len(args):
                raise angel.exceptions.AngelArgException('missing action.')

            # For a number of service commands, we lock to prevent concurrent starts / stop or other modifications that aren't safe to run simultaneously.
            class LockUnavailableError(Exception):
                pass
            def _get_lock_or_error():
                if 0 != devops.file_and_dir_helpers.get_lock(self.get_settings(), angel.constants.SERVICE_LOCKNAME):
                    raise LockUnavailableError()

            need_to_release_lock = False
            try:
                verb = args.pop(0)

                if verb == 'conf':
                    if not len(args): raise angel.exceptions.AngelArgException('missing action.')
                    action = args.pop(0)
                    if not len(args): raise angel.exceptions.AngelArgException('missing key.')

                    if action == 'unset':
                        while len(args):
                            self._settings.unset_override_and_save(args.pop(0))
                        return 0

                    if action == 'set':
                        while len(args):
                            key = args.pop(0)
                            value = None
                            if key.find('=') > 0:
                                key, value = key.split('=',1)
                            elif 0 != len(args):
                                value = args.pop(0)
                            else:
                                raise angel.exceptions.AngelArgException('missing value.')
                            self._settings.set_override_and_save(key, value)
                            return 0

                    raise angel.exceptions.AngelArgException('unknown conf action "%s".' % action)

                if verb == 'autoconf':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    force = False
                    while len(args):
                        opt = args.pop(0)
                        if opt == '--force':
                            force = True
                        else:
                            raise angel.exceptions.AngelArgException('unknown autoconf option "%s".' % opt)
                    #ret_val = system_autoconf(self.get_settings(), force=force)
                    ret_val = -1
                    if ret_val == 0: return 0
                    if ret_val < 0:
                        return 1
                    # No errors; changes found -- need to re-run update_system_config
                    if not self.are_services_running():
                        print "Autoconf updated config (change count=%s); ready to start." % ret_val
                        return 0
                    print "Autoconf updated config (change count=%s); reloading code to apply changes." % ret_val
                    return self.service_reload(reload_code=False, reload_conf=True)

                if verb == 'mode':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    if 1 != len(args): raise angel.exceptions.AngelArgException('mode requires either maintenance or regular keyword.')
                    opt = args.pop(0)
                    if 'regular' == opt or '200' == opt:
                        return self.set_maintenance_mode(False)
                    if 'maintenance' == opt or '503' == opt:
                        return self.set_maintenance_mode(True)
                    raise angel.exceptions.AngelArgException('unknown mode %s.' % opt)

                if verb == 'reload':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    reload_code = True
                    reload_conf = True
                    flush_caches_requested = False
                    while len(args):
                        opt = args.pop(0)
                        if opt == '--skip-conf':
                            reload_conf = False
                        elif opt == '--flush-caches':
                            flush_caches_requested = True
                        elif opt == '--code-only':
                            # Same as --skip-conf, but code upgrades use this and
                            # it's a clearer name in case stuff changes in the future.
                            reload_conf = False
                        else:
                            raise angel.exceptions.AngelArgException("unknown reload option '%s'." % opt)
                    return self.service_reload(reload_code=reload_code, reload_conf=reload_conf, flush_caches_requested=flush_caches_requested)

                if verb == 'start' or verb == 'restart':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    wait_timeout = None   # If not None, we'll wait for up to this number of seconds before returning; if we timeout, non-zero exit
                    while len(args):
                        opt = args.pop(0)
                        if '--wait' == opt[:6]:
                            wait_timeout = 600
                            if len(opt) > 6:
                                try:
                                    wait_timeout = int(opt[7:])
                                except:
                                    raise angel.exceptions.AngelArgException('--wait=<seconds> requires a number')
                        else:
                            raise angel.exceptions.AngelArgException("unknown option '%s'." % opt)
                    if verb == 'start':
                        ret_val = self.service_start(timeout=wait_timeout)
                    else:
                        ret_val = self.service_restart(timeout=wait_timeout)
                    if is_interactive:
                        try:
                            self.are_services_status_ok(wait_for_ok=True, timeout=1)
                        except KeyboardInterrupt:
                            pass
                        self.run(['status',])
                    return ret_val

                if verb == 'stop':
                    hard_stop = False
                    while len(args):
                        opt = args.pop(0)
                        if '--hard' == opt:
                            hard_stop = True
                        else:
                            raise angel.exceptions.AngelArgException("unknown option '%s'." % opt)

                    if not hard_stop:
                        _get_lock_or_error()
                        need_to_release_lock = True
                    return self.service_stop(hard_stop=hard_stop)

                if verb == 'rotate-logs':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    return self.service_rotate_logs()

                if verb == 'repair':
                    _get_lock_or_error()
                    need_to_release_lock = True
                    return self.service_repair()

                raise angel.exceptions.AngelArgException("unknown service command '%s'." % verb)

            except LockUnavailableError:
                print >>sys.stderr, "Error: can't get service lock."
                return 1

            finally:
                if need_to_release_lock:
                    devops.file_and_dir_helpers.release_lock(self.get_settings(), angel.constants.SERVICE_LOCKNAME)


        elif category == 'status':
            format = None
            interval = None
            timeout = None
            do_state_checks = False
            do_service_checks = False
            wait_for_ok = False
            wait_for_ok_timeout = 600
            check_only_these_services = None
            while len(args):
                opt = args.pop(0)
                if '--format=' == opt[0:9]:
                    format = opt[9:]
                elif '--interval=' == opt[0:11]:
                    try:
                        interval = int(opt[11:])
                    except:
                        raise angel.exceptions.AngelArgException('--interval=<seconds> requires a number')
                elif opt[:9] == '--timeout':
                    try:
                        if len(opt) > 9:
                            timeout = int(opt[10:])
                        else:
                            timeout = int(args.pop(0))
                    except:
                        raise angel.exceptions.AngelArgException('--timeout=<seconds> requires a number')

                elif opt[:6] == '--wait':
                    wait_for_ok = True
                    if len(opt) > 7:
                        try:
                            wait_for_ok_timeout = int(opt[7:])
                        except:
                            raise angel.exceptions.AngelArgException('--wait=<seconds> requires a number')
                elif opt == 'service':
                    do_service_checks = True
                    check_only_these_services = []
                elif opt == 'state':
                    do_state_checks = True
                else:
                    if check_only_these_services is None:
                        raise angel.exceptions.AngelArgException('unknown option %s' % opt)
                    else:
                        check_only_these_services += [opt,]

            if not do_state_checks and not do_service_checks:
                # Then we haven't requested any particular status info; give 'em everything:
                do_state_checks = True
                do_service_checks = True

            if wait_for_ok:
                if self.are_services_running():
                    self.are_services_status_ok(wait_for_ok=True, timeout=wait_for_ok_timeout)
                else:
                    print >>sys.stderr, "Warning: ignoring --wait options; services aren't running."

            return run_status_check(self, do_state_checks=do_state_checks, do_service_checks=do_service_checks, check_only_these_services=check_only_these_services, timeout=timeout, format=format, interval=interval)


        elif category == 'show':
            if not len(args):
                raise angel.exceptions.AngelArgException('missing action.')
            verb = args.pop(0)

            if verb == 'logfiles':
                filters = ()
                while len(args):
                    opt = args.pop(0)
                    if opt.startswith('--'):
                        raise angel.exceptions.AngelArgException("Unknown option %s." % opt)
                    else:
                        filters += (opt,)
                log_paths = log_get_logfile_paths(os.path.expanduser(self.get_settings()['LOG_DIR']), filters=filters, include_system_logs=False)
                if log_paths is None:
                    return 1
                if len(log_paths):
                    print '\n'.join(log_paths)
                return 0

            elif verb == 'logs' or verb == 'log':
                filters = ()
                show_names = False
                show_host = False
                show_timestamps = True
                last_n_lines = None
                with_color = True
                if not sys.stdout.isatty():
                    with_color = False
                while len(args):
                    opt = args.pop(0)
                    if opt == '--show-names':
                        show_names = True
                    elif opt == '--last':
                        try:
                            last_n_lines = int(args.pop(0))
                        except:
                            raise angel.exceptions.AngelArgException('invalid line count.')
                    elif opt == '--show-host':
                        show_host = True
                    elif opt == '--without-color':
                        with_color = False
                    elif opt == '--without-time':
                        show_timestamps = False
                    else:
                        filters += (opt,)

                line_preamble = ''
                if show_host:
                    line_preamble = '[%s]' % self.get_node_hostname()
                return log_tail_logs(os.path.expanduser(self.get_settings()['LOG_DIR']), filters=filters, show_names=show_names, line_preamble=line_preamble, colored_output=with_color, show_timestamps=show_timestamps, last_n_lines=last_n_lines)

            elif verb == 'variables' or verb == 'vars':
                format = ''
                filter = ()
                while len(args):
                    a = args.pop(0)
                    if a.startswith('--'):
                        if a == '--format':
                            if 0 == len(args): raise angel.exceptions.AngelArgException('missing format value.')
                            format = args.pop(0)
                        else:
                            print >>sys.stderr, 'Error: unknown vars option "%s".' % a
                            return 1
                    else:
                        filter += (a,)

                filtered_config = {}
                exported_data = None

                # Build up a list of keys to export, and check that they're exportable:
                settings = self.get_settings()
                for key in settings:
                    if len(filter) != 0:
                        found = False
                        excluded = False
                        for f in filter:
                            if f.startswith('-'):
                                if key.lower().find(f[1:].lower()) >= 0:  # Then we're excluding these matches...
                                    excluded = True
                            if f.startswith('+'):
                                if key.lower().find(f[1:].lower()) >= 0:
                                   found = True
                                else:
                                   excluded = True
                            if key.lower().find(f.lower()) >= 0:
                                found = True
                        if not found or excluded:
                            continue
                    if type(settings[key]) is str:
                        if len(settings[key].split('\n')) > 1:
                            print >>sys.stderr, "Error: multi-line config setting %s not allowed. (%s)" % (key, settings[key])
                            return 1
                    if type(settings[key]) not in (float, int, bool, type(None), str) and format != '':
                        print >>sys.stderr, "Warning: unsupported data type %s for settings key %s; skipping it in export." % (type(settings[key]), key)
                        continue
                    filtered_config[key] = settings[key]

                if 0 == len(filtered_config):
                    print >>sys.stderr, "Warning: no variables matched filter '%s'." % ' '.join(filter)
                    return 0  # Don't treat this as an actual error
                # Export data into given format, defaulting with key=value:
                if format == '':
                    exported_data = ''
                    for key in sorted(filtered_config):
                        exported_data += '%s=%s\n' % (key, repr(filtered_config[key]))

                elif format == '.properties':
                    exported_data = '\n! These settings were exported in Java .properties format using "show vars"; do not manually edit this file.\n\n'
                    for key in sorted(filtered_config):
                        exported_data += '%s: %s\n' % (key, filtered_config[key])

                elif format == '.py':
                    exported_data = '\n# These settings were exported in Python .py format using "show vars"; do not manually edit this file.\n\n'
                    for key in sorted(filtered_config):
                        exported_data += '%s=%s\n' % (key, repr(filtered_config[key]))

                elif format == '.sh':
                    exported_data = '\n# These settings were exported in shell format using "show vars"; do not manually edit this file.\n\n'
                    for key in sorted(filtered_config):
                        exported_data += "export %s_SETTING_%s='%s'\n" % \
                            (self.get_project_name().upper(), key, str(filtered_config[key]).replace('\'', '\'\"\'\"\''))

                else:
                    raise angel.exceptions.AngelArgException("unknown format '%s'." % format)

                if exported_data is None:
                    print >>sys.stderr, "Error: export failed?"
                    return 1

                print exported_data.rstrip()
                return 0


            elif verb == 'variable' or verb == 'var':
                if not len(args):
                    raise angel.exceptions.AngelArgException('missing variable name to show.')
                variable_name = args.pop(0)
                variable_name = variable_name.upper()
                if self.get_settings().is_set(variable_name):
                    print str(self.get_settings()[variable_name])
                    return 0
                else:
                    print  >>sys.stderr, "No variable named %s." % variable_name
                    return 1


            else:
                raise angel.exceptions.AngelArgException("unknown show command '%s'." % verb)


        else:
            # project-dash scripts are of the form <projectname>-<name> and located under
            # of the bin paths listed in BIN_PATHS.
            # This allows projects to define custom scripts and be able to use our
            # --use-version, --use-branch, --duration, and related options, and
            # to not have to have all of the BIN_PATHS located in the actual env PATH.
            script_name = "%s-%s" % (self.get_project_name(), category)
            script_path = None
            paths = ''
            for bin_path in self.get_settings()['BIN_PATHS'].split(':'):
                bin_path = os.path.abspath(os.path.join(self.get_project_base_dir(), bin_path))
                paths += '%s:' % bin_path
                path = os.path.join(bin_path, script_name)
                if not script_path and os.path.isfile(path) and os.access(path, os.X_OK):
                    script_path = path
            os.environ['PATH'] = paths + os.environ['PATH']  # Our paths MUST come first
            if script_path:
                return devops.process_helpers.exec_process(script_path, args=args)

        # If we get here, no category matched, and we don't have a project-dash script
        raise angel.exceptions.AngelArgException("unknown command '%s'." % category)


    def verify_user_is_root_or_cmd_user(self):
        if os.getuid() == 0:
            return True
        if 'RUN_AS_USER' not in self._settings or self._settings['RUN_AS_USER'] is None:
            #print >>sys.stderr, 'Warning: RUN_AS_USER missing.'
            return
        try:
            if os.getuid() == pwd.getpwnam(self._settings['RUN_AS_USER']).pw_uid:
                return
        except:
            print >>sys.stderr, 'Warning: config option RUN_AS_USER set to non-existent user "%s".' % self._settings['RUN_AS_USER']
        raise angel.exceptions.AngelSettingsException("command can't be run as the current user (try sudo?)")


    def get_project_base_dir(self):
        """Return the path to the base directory of the project we're managing."""
        return self._project_base_dir


    def get_project_exec(self):
        """Return a shell path that will run this version of the project."""
        full_path=os.path.realpath(os.path.join(self.get_project_base_dir(), self._project_entry_script))
        # On cluster installs, we often have symlinks from /usr/bin pointed to us. If we happen to have
        # such a symlink, then return just the script name, since it's in our path already.
        # We don't check all of ENV['PATH']; we could check a "safe" path (/bin/:/usr/bin:/usr/local/bin), but this is fine.
        usr_bin_path = os.path.join("/usr/bin", os.path.basename(full_path))
        try:
            if os.path.samefile(usr_bin_path, full_path):
                return os.path.basename(full_path)
        except:
            pass
        return full_path


    def get_project_name(self):
        """Return the name of the project (as set up by control script,
        usually this is the name of the command you run in the shell).
        """
        return self._project_name


    def get_project_code_branch(self):
        """Return the branch name of the project (i.e. the git branch that this version is running from), or None if unknown.
        If we are running from a git checkout, this will be as-reported from git.
        If we are running from a deployed build, a file under <based_dir>/.angel/code_branch is read.
        """

        code_branch = None
        cache_key = 'get_project_code_branch'
        if cache_key in self._cached_data:
            return self._cached_data[cache_key]

        # Deployed code case:
        filepath = os.path.join(self.get_project_base_dir(), '.angel', 'code_branch')
        if os.path.isfile(filepath):
            try:
                code_branch = open(filepath).read()[:-1]
            except Exception as e:
                print >>sys.stderr, "Error: can't determine branch (failed to read %s: %s)." % (filepath, e)
                return None

        # Git checkout case:
        # We don't use "git symbolic-ref HEAD" because that involves creating a subprocess,
        # which involves resetting SIGCHLD to avoid dpkg weirdness, but we can't do that inside other places.
        # Instead, we're reading straight out of the .git file; this may need updating if git changes its format.
        if code_branch is None:
            dirpath = self.get_project_base_dir()
            while (len(dirpath)>1 and not os.path.exists(os.path.join(dirpath, '.git'))):
                dirpath = os.path.dirname(dirpath)
            filepath = os.path.join(dirpath, '.git', 'HEAD')
            if os.path.isfile(filepath):
                try:
                    code_branch = open(filepath).readline().rstrip().split('/')[2]
                except IndexError:
                    # This will happen if the git checkout is in a detached state (happens in Jenkins).
                    # We'll fall-back on GIT_BRANCH, in case that's defined by the Jenkins Git plugin.
                    if 'GIT_BRANCH' in os.environ:
                        code_branch = os.environ['GIT_BRANCH'].split('/')[1]
                    else:
                        raise angel.exceptions.AngelSettingsException("Unable to determine git branch (try setting GIT_BRANCH in your env?)")
                except Exception as e:
                    print >> sys.stderr, "Error: can't determine branch (failed to read .git/HEAD file: %s)." % e
                    return None

        if code_branch is None:
            print >>sys.stderr, "Error: can't determine branch (can't find .angel or .git directory under %s)." % \
                                (self.get_project_base_dir())
            return None

        self._cached_data[cache_key] = code_branch
        return code_branch


    def get_project_code_version(self):
        """Return the build version of the project (i.e. the jenkins build number or a git-based version number).

        If we are running from a git checkout, this version number is generated as X.Y.sha1, where:
         - X is the most recent tag of the format "jenkins-<branch_name>-<X>"
         - Y is the number of commits
         - sha1 is an alphanumeric+hypen string of the most recent commit, and possibly additional flags ("-dirty")

        If we are running from a deployed build, a file under <based_dir>/.angel/code_version is read and returned.

        Note that minor versions are not comparable as numbers. That is, 3.10 is *newer* than 3.9, but 3.9 > 3.10.
        """

        code_version = None
        cache_key = 'get_project_code_version'
        if cache_key in self._cached_data:
            return self._cached_data[cache_key]

        # To-do? May need to return os.environ['BUILD_NUMBER'] if it's defined -- jenkins will set that
        # while we're building but before the build is tagged.
        if 'BUILD_NUMBER' in os.environ:
            print >>sys.stderr, "Warning: os.environ['BUILD_NUMBER'] defined (%s) but code not written to use it." % \
                os.environ['BUILD_NUMBER']

        # Deployed code case:
        filepath = os.path.join(self.get_project_base_dir(), '.angel', 'code_version')
        if os.path.isfile(filepath):
            try:
                code_version = open(filepath).read()[:-1]
            except Exception as e:
                print >>sys.stderr, "Error: can't determine version (failed to read %s: %s)." % (filepath, e)
                return None

        # Git checkout case:
        dirpath = self.get_project_base_dir()
        while (len(dirpath)>1 and not os.path.exists(os.path.join(dirpath, '.git'))):
            dirpath = os.path.dirname(dirpath)
        if os.path.isdir(os.path.join(dirpath, '.git')):
            code_version = self._get_code_version_from_git(dirpath=dirpath)

        if code_version is None:
            print >>sys.stderr, "Error: can't determine version (can't find .angel directory under %s or git directory at %s)." % \
                                (self.get_project_base_dir(), dirpath)
            return None

        self._cached_data[cache_key] = code_version
        return code_version



    def _get_code_version_from_git(self, dirpath=None):
        """Return a version number based on git tags and git commits, or None on error."""
        if dirpath is None:
            dirpath = self.get_project_base_dir()
        if not os.path.isdir(os.path.join(dirpath, '.git')):
            return None

        # We use git tags based on the string "jenkins-" and the current branch for our git-based version logic:
        branch_name = self.get_project_code_branch()
        expected_jenkins_tag = "jenkins-%s-" % branch_name

        # Check if we have uncommitted files (so we can add ".dirty" to the version):
        #  1. Update index so that git sees any recently modified files:
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git update-index --refresh --unmerged",
                                                      chdir=self.get_project_base_dir())
        if exitcode != 0 and exitcode != 1:
            print >>sys.stderr, "Error: git update-index failed while getting project code version (%s)." % exitcode
            return None
        #  2. List uncommitted files:
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git diff-index --name-only HEAD",
                                                      chdir=self.get_project_base_dir())
        if exitcode != 0:
            print >>sys.stderr, "Error: git diff-index failed while getting project code version (%s)." % exitcode
            return None
        files_not_committed = False
        if len(out):
            files_not_committed = True
        dirty_string = ""
        if files_not_committed:
            dirty_string = "-dirty"

        # We use the most recent short sha1 in version strings, to make it easier to line up deploys
        # with what we see in git:
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git rev-parse HEAD",
                                                      chdir=self.get_project_base_dir())
        if exitcode != 0:
            print >>sys.stderr, "Error: git rev-parse failed while getting project code version (%s)." % exitcode
            return None
        sha1 = out[0:8]

        # Check if there's an exact match for a tag -- that is, our current HEAD has been tagged
        # directly as a build (version X):
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git describe --tags --match '%s*' --exact-match" %
                                                      expected_jenkins_tag,
                                                      chdir=self.get_project_base_dir())
        if exitcode != 0 and exitcode != 128:
            print >>sys.stderr, "Error: git describe(1) failed while getting project code version (%s)." % exitcode
            return None
        if len(out):
            # Return the exact version as tagged -- this is normally "X", not "X.Y", and usually
            # set by a jenkins post-build tag equal to the build number:
            return "%s.0.%s%s" % (out.rstrip()[len(expected_jenkins_tag):], sha1, dirty_string)

        # If we get here, no exact match for a tag. Check how many commits we have since the last
        # tag, and if there are any uncommited files since the last commit, and generate a X.yy[.dirty] version.

        # Figure out how many commits we've had since the last build tag:
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git describe --match '%s*' --tags" % expected_jenkins_tag,
                                                      chdir=self.get_project_base_dir())
        if exitcode == 0:
            # Then we've had a build tag, so we're some number of commits past it:
            m = re.search('\-([0-9]+)\-([0-9]+)', out.rstrip())
            if m is None:
                print >>sys.stderr,\
                    "Error: no version info found in tag while getting project code version (%s)." % out
                return None
            build_number = m.group(1)
            commits_since_build = m.group(2)
            return "%s.%s.%s%s" % (build_number, commits_since_build, sha1, dirty_string)

        # If we get here, then this branch has no prior build tags, return "0.y" where y is the number of commits.
        out, err, exitcode = \
            devops.process_helpers.get_command_output("git log --oneline",
                                                      chdir=self.get_project_base_dir())
        if exitcode != 0:
            print >>sys.stderr, "Error: 'git log --oneline' failed while getting project code version (%s)." % exitcode
            return None
        return "0.%s.%s%s" % (len(out.split('\n')), sha1, dirty_string)



    def get_command_for_running(self, args=None, setting_overrides=None):
        """Return a string that represents a command to be run on the command line, with options and
        values set as necessary. This is intended to be used for hints/tips in error messages."""
        (args, env) = self.get_args_and_env_for_running_self(args=args, setting_overrides=setting_overrides)
        ret_str = ""
        for k in env:
            ret_str += "%s=%s " % (k, pipes.quote(env[k]))
        ret_str += pipes.quote(self.get_project_exec())
        for i in args:
            ret_str += ' ' + pipes.quote(i)
        return ret_str


    def get_args_and_env_for_running_self(self, args=None, setting_overrides=None):
        """Return an array of args that will cause this control script to run.
        The first arg is the name of the control script itself."""
        ret_env = {}
        if setting_overrides:
            for k in setting_overrides:
                ret_env['%s_SETTING_%s' % (self.get_project_name().upper(), k.upper())] = setting_overrides[k]
        ret_args = ()
        if self._angel_version_manager:
            if self.get_project_code_branch() != self._angel_version_manager.get_default_branch():
                ret_args += ("--use-branch", self.get_project_code_branch())
            if self.get_project_code_version() !=\
                    self._angel_version_manager.get_default_version(self.get_project_code_branch()):
                ret_args += ("--use-version", self.get_project_code_version())
        if args:
            ret_args += args
        return ret_args, ret_env


    def get_private_ip_addr(self):
        """Return the private IP address of this node, when the node is on ec2 and part of a cluster.
        Otherwise, returns the public IP address."""
        if 'get_private_ip_addr' in self._cached_data:
            return self._cached_data['get_private_ip_addr']
        ip = None
        # If we're on linux, try using ec2 to get the private IP address:
        if sys.platform[:5] == 'linux':
            import devops.ec2_support
            ip = devops.ec2_support.ec2_get_attribute_via_http('meta-data/local-ipv4')
        # If still no IP, fall back on public IP address:
        if ip is None:
            ip = self.get_public_ip_addr()
        self._cached_data['get_private_ip_addr'] = ip
        return self._cached_data['get_private_ip_addr']


    def get_public_ip_addr(self):
        """Return the public IP address for this node. This IP isn't necessarily externally route-able."""
        if 'get_public_ip_addr' in self._cached_data:
            return self._cached_data['get_public_ip_addr']
        ip = None
        # If we're on linux, try using ec2 to get the public IP address:
        if sys.platform[:5] == 'linux':
            import devops.ec2_support
            ip = devops.ec2_support.ec2_get_attribute_via_http('meta-data/public-ipv4')
        # If that didn't work, try looking up the IP address of our hostname:
        if ip is None:
            try:
                ip = socket.gethostbyname(socket.gethostname())
                if ip == '127.0.0.1':
                    ip = None
            except Exception as e:
                print >>sys.stderr, "Warning: socket.gethostbyname(socket.gethostname()) failed (%s)." % e
                pass
        # If that still didn't work, try connecting to a public server and checking the IP of our socket:
        if ip is None:
            try:
                sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                sock.connect(('google.com', 80))
                ip = sock.getsockname()[0]
                sock.close()
            except:
                pass  # Catch and ignore condition where there's no DNS/network
        # If still no luck, fall back on localhost:
        if ip is None:
            ip = '127.0.0.1'
        self._cached_data['get_public_ip_addr'] = ip
        return self._cached_data['get_public_ip_addr']


    def is_ip_addr_on_this_host(self, ip):
        ''' Return True if the given ip address matches an address that the local node is listening to.
        Note: this function doesn't currently exhaustively check all local IPs.
        '''
        if ip is None:
            return False
        if ip == '127.0.0.1':
            return True
        if self.get_private_ip_addr() == ip:
            return True
        if self.get_public_ip_addr() == ip:
            return True
        return False


    def get_node_hostname(self, short_name=False):
        cache_key = 'get_node_hostname-%s' % short_name
        if cache_key in self._cached_data:
            return self._cached_data[cache_key]
        hostname = socket.gethostname()  # This isn't guaranteed to return the short name; e.g. we can get "my-laptop.local".
        hostname = hostname.lower()  # Use lower, so comparisons are consistent
        try:
            socket.getaddrinfo(hostname, None)
        except:
            print >>sys.stderr, "Warning: hostname '%s' isn't reverse-resolving!" % hostname
        if short_name:
            hostname = hostname.split('.')[0]
        self._cached_data[cache_key] = hostname
        return hostname


    def get_ips_running_service(self, service_name):
        """Return a tuple of IP addresses running the given service name, or None if no setting found (which is allowed)."""
        service_host_variable_name = '%s_HOST' % service_name.replace('-', '_').upper()
        service_hosts_variable_name = '%s_HOSTS' % service_name.replace('-', '_').upper()
        if service_host_variable_name in self.get_settings():
            return (self.get_settings()[service_host_variable_name],)
        if service_hosts_variable_name in self.get_settings():
            return self.get_settings()[service_hosts_variable_name].split(",")
        return None


    def is_multinode_install(self):
        """Return true if any service HOST / HOSTS setting has a non-127.0.0.x IP address."""
        for service_name in self.get_service_names():
            service_ips = self.get_ips_running_service(service_name)
            if not service_ips:
                continue
            for ip in service_ips:
                if not ip.startswith("127.0.0"):
                    return True
        return False


    def get_node_ip_addrs(self, include_localhost=True):
        """Return a list of IP addresses associated with this node."""
        from devops.unix_helpers import get_local_ip_addrs
        local_ips = get_local_ip_addrs()
        if not include_localhost:
            try:
                local_ips.remove('127.0.0.1')
            except:
                print >>sys.stderr, "Warning: 127.0.0.1 missing from local ips?"
        this_public_ip = self.get_public_ip_addr()
        if this_public_ip not in local_ips:
            local_ips += (this_public_ip,)
        return local_ips


    def get_enabled_services(self):
        enabled_services = []

        # We have a bunch of variables like REDIS_SERVICE = 'default|on|off' and REDIS_HOSTS="127.0.0.1".
        # If the service is "on", it always runs. If it is "off", it never runs.
        # By default, when ALL of our xxx_HOSTS / xxx_HOST settings are under 127.0.0.x, we'll run everything;
        # if there is a single xxx_HOST(S) setting that points at a non-127.0.0.x IP, then we ONLY run the services
        # which IP address match this node's IP addresses.
        run_by_ip_match = self.is_multinode_install()
        non_localhost_ips = self.get_node_ip_addrs(include_localhost=False)
        for service_name in self.get_service_names():
            config_key = service_name.replace('-','_').upper() + '_SERVICE'
            if config_key not in self.get_settings():
                print >>sys.stderr, "Warning: service %s not defined in config. Make sure %s is defined and set to default, off, or on." %\
                                    (service_name, config_key)
                continue

            # Check if this service is explicitly enabled or disabled:
            if self.get_settings()[config_key].lower() == 'off':
                continue
            if self.get_settings()[config_key].lower() == 'on':
                enabled_services += [service_name]
                continue
            if self.get_settings()[config_key].lower() != 'default':
                raise angel.exceptions.AngelSettingsException('%s set to "%s"; should be on, off, or default.' %\
                            (config_key, self.get_settings()[config_key]))
            if not run_by_ip_match:
                enabled_services += [service_name]
                continue

            # If we get here, then we're a multi-node install and the service is set to run "by default", in which case
            # we enable a service when the IP address in our config matches an IP address on this machine:
            service_ips = self.get_ips_running_service(service_name)
            if service_ips is None:
                continue
            if len(list(set(service_ips) & set(non_localhost_ips))):
                # If the union of IPs for the service and IPs on this node > 0, then we match.
                enabled_services += [service_name]

        return enabled_services


    def get_hosts_running_service(self, service_name):
        # Get a list of all hosts running given service (based on the config available to *this* node):
        service_setting_name = '%s_SERVICE' % service_name.replace('-', '_').upper()
        if service_setting_name not in self.get_settings():
            print >>sys.stderr, "Warning: no %s setting for service %s?" % (service_setting_name, service_name)
            return None
        if self.get_settings()[service_setting_name].lower() == 'off':
            return ()
        if self.get_settings()[service_setting_name].lower() == 'on':
            return self.get_all_known_hosts()
        return self._get_listed_ips_for_service(service_name)


    def _get_listed_ips_for_service(self, service_name):
        # Check settings.xxx_HOST and settings.xxx_HOSTS for IP addresses:
        hosts_running_service = ()
        hosts_list_in_settings = None
        host_service_setting_name = service_name.replace('-', '_').upper() + '_HOST'
        hosts_service_setting_name = host_service_setting_name + 'S'
        if host_service_setting_name in self.get_settings() and self.get_settings()[host_service_setting_name] is not None:
            hosts_list_in_settings = (self.get_settings()[host_service_setting_name],)
        if hosts_service_setting_name in self.get_settings() and self.get_settings()[hosts_service_setting_name] is not None:
            hosts_list_in_settings = self.get_settings()[hosts_service_setting_name].split(',')
        if hosts_list_in_settings is None:
            return None
        for host in sorted(set(hosts_list_in_settings)):
            if host == '127.0.0.1':
                if self.is_multinode_install():
                    # 127.0.0.1 entries are ignored in multi-node installs when service runs
                    # in "default" mode; if it was "on", we caught that a dozen lines above.
                    continue
                else:
                    # For clarity, remap localhost to private IP (which might be 127.0.0.1 in some cases):
                    host = self.get_private_ip_addr()
            hosts_running_service += (host,)
        return sorted(set(hosts_running_service))


    def get_all_known_hosts(self):
        hosts = []
        for service_name in self.get_service_names():
            service_hosts = self._get_listed_ips_for_service(service_name)
            if service_hosts:
                hosts += service_hosts
        return sorted(set(hosts))


    def set_maintenance_mode(self, run_in_maintenance_mode):
        ''' When in maintenance mode, no public access to the site should be permitted. '''
        lockfile_path = '%s/.maintenance_mode_lock' % self.get_settings()['DATA_DIR']  # Use data dir so that restarts / resets don't lose state
        if run_in_maintenance_mode:
            if os.path.exists(lockfile_path):
                return 0
            try:
                open(lockfile_path, 'w').close()
            except:
                print >>sys.stderr, "Error: unable to create maintenance mode lockfile."
                return 1
        else:
            if not os.path.exists(lockfile_path):
                return 0
            try:
                os.remove(lockfile_path)
            except:
                print >>sys.stderr, "Error: unable to remove maintenance mode lockfile."
                return 1
        ret_val = 0
        if self.are_services_running():
            run_in_parallel = True
            if run_in_maintenance_mode:
                (ret_val, dummy) = self._run_verb_on_services(self.get_enabled_or_running_service_objects(), 'switchToMaintenanceMode', run_in_parallel)
            else:
                (ret_val, dummy) = self._run_verb_on_services(self.get_enabled_or_running_service_objects(), 'switchToRegularMode', run_in_parallel)
        else:
            print >>sys.stderr, "Warning: services not running."
        return ret_val


    def is_in_maintenance_mode(self):
        ''' Return true if site is running in maintenance mode. '''
        if os.path.exists('%s/.maintenance_mode_lock' % self.get_settings()['DATA_DIR']):
            return True
        return False


    def clear_tmp_dir(self):
        if os.path.isdir(self.get_settings()['TMP_DIR']):
            for f in os.listdir(self.get_settings()['TMP_DIR']):
                try:
                    path = os.path.join(self.get_settings()['TMP_DIR'], f)
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    elif os.path.isfile(path):
                        os.remove(path)
                    else:
                        print >>sys.stderr, 'Warning: unknown file type in tmp dir (%s); skipping it.' % path
                except Exception as e:
                    print >>sys.stderr, 'Warning: clearing tmp dir file "%s" failed (%s), possibly do to concurrent runs?' % (f, e)


    def get_service_state(self):
        state_file = self._get_state_file_path()
        if not os.path.exists(state_file):
            return None, -1
        try:
            data = open(state_file, 'r').read()
            if 0 == len(data): return None
            state, time = data.split('\n')[0:2]
            return int(state), float(time)
        except Exception as e:
            print >> sys.stderr, "Unable to read state file '%s': %s" % (state_file, e)
        return None, -2


    def set_service_state(self, state_constant):
        state_file = self._get_state_file_path()
        tmp_state_file = '%s-%s.tmp' % (state_file, int(time.time()))
        if state_constant is None:
            print >>sys.stderr, " ********** ERROR? set_service_state given a None state_constant? "
            try:
                if os.path.isfile(state_file):
                    os.remove(state_file)
            except:
                print >>sys.stderr, "Error: unable to remove state file '%s'." % state_file
                return -1
            return 0
        try:
            open(tmp_state_file, 'wt').write( "%s\n%s" % (state_constant, time.time()) )
            os.rename(tmp_state_file, state_file)
        except Exception as e:
            print >>sys.stderr, "Error writing state file '%s': %s" % (state_file, e)
            return -1
        return 0


    def _get_state_file_path(self):
        return os.path.join(os.path.expanduser(self.get_settings()['LOCK_DIR']), 'service_state.lock')


    def are_services_status_ok(self, accept_warn_as_ok=True, wait_for_ok=False, timeout=300):
        if timeout > 60*60:
            print >>sys.stderr, 'Warning: wait timeout given invalid value "%s", using 60 minutes.' % timeout
            timeout=60*60
        current_state = run_status_check(self, do_all_checks=True, format="silent")
        if current_state == angel.constants.STATE_RUNNING_OK:
            return True
        if accept_warn_as_ok and current_state == angel.constants.STATE_WARN:
            return True
        if wait_for_ok and timeout > 0:
            try:
                time.sleep(2)
                return self.are_services_status_ok(accept_warn_as_ok=accept_warn_as_ok,
                                                   wait_for_ok=wait_for_ok,
                                                   timeout=(timeout-2))
            except KeyboardInterrupt:
                print >>sys.stderr, "Returning early (services not yet ok; ctrl-c abort)"
        return False


    def get_service_names(self):
        ''' Auto-discover all services by looking for settings named SERVICE_xxx. '''
        names = []
        for key in self.get_settings():
            if key[-8:] != '_SERVICE': continue
            service_name = key[:-8].lower().replace('_', '-')
            names.append(service_name)
        return names


    def are_services_running(self):
        ''' Return True if services are supposed to be running (i.e. 'service start' has been called); False otherwise. '''
        current_state, last_change_time = self.get_service_state()
        if current_state is None or current_state == angel.constants.STATE_STOPPED:
            return False

        # If services aren't stopped, then make sure that the last change time is newer than our uptime.
        # We do this so that we start up correctly if a node gets hard-rebooted.
        uptime = None
        if sys.platform == 'darwin':
            out, err, exitcode = devops.process_helpers.get_command_output("sysctl -n kern.boottime | awk '{print $4}' | sed -e 's:\,::'")
            if exitcode == 0:
                boottime = int(out)
                uptime = time.time() - boottime
                if uptime < 0: uptime = None
                if uptime > 365*24*60*60: uptime = None
            else:
                print >>sys.stderr, "Failed to get uptime in OS X logic"
        else:
            if os.path.exists("/proc/uptime"):
                uptime, dummy = [float(field) for field in open("/proc/uptime").read().split()]
            else:
                print >>sys.stderr, "Failed to get uptime"

        if uptime is None or last_change_time < 1000000000:
            print >>sys.stderr, "Warning: can't figure out uptime or last_change_time in call to are_services_running"
            return True  # Can't get uptime, assume that we didn't crash...

        state_age = time.time() - last_change_time
        if state_age > uptime:
            state_file = self._get_state_file_path()
            if not os.path.exists(state_file):
                print >>sys.stderr, "Error: service uptime check missing state file?!"
            else:
                print >>sys.stderr, "Warning: uptime is less than service statefile's last change time; assuming machine hard crashed and rebooted; clearing old statefile."
                os.rename(state_file, "%s-stale-uptime-%s" % (state_file, int(time.time())))
            return False

        return True


    def get_supervisor_lockdir(self):
        return os.path.join(os.path.expanduser(self.get_settings()['LOCK_DIR']), 'supervisor')


    def get_supervisor_lockpath(self, service_name):
        ''' Supervisor lock files need to be named such that we can back out the class name of the service based on the filename. '''
        super_lock_dir = self.get_supervisor_lockdir()
        filename = '-'.join(re.findall('[A-Z][^A-Z]*', service_name.replace('Service',''))).lower() # RedisReadonlyService -> redis-readonly
        if not os.path.isdir(super_lock_dir):
            from devops.file_and_dir_helpers import create_dirs_if_needed
            if 0 != create_dirs_if_needed(super_lock_dir, name='service lockdir', recursive_fix_owner=True,
                                          owner_user=self.get_settings()['RUN_AS_USER'], owner_group=self.get_settings()['RUN_AS_GROUP']):
                print >>sys.stderr, "Error: can't make lock subdir for services."
        return os.path.join(super_lock_dir, '%s.lock' % filename)


    def get_running_service_names(self):
        ''' Return a list of all currently-running services. '''
        services = []
        super_lock_dir = self.get_supervisor_lockdir()
        if not os.path.isdir(super_lock_dir):
            return services
        lockfiles = os.listdir(super_lock_dir)
        for lockfile in lockfiles:
            if lockfile[-5:] == '.lock':
                services += [ lockfile[:-5], ]
        return services


    def _get_enabled_or_running_service_names(self):
        ''' Return a list of names of the services that are currently running or should be active (as defined in the conf) for this node. '''
        return list(set( self.get_running_service_names() + self.get_enabled_services()))


    def get_enabled_or_running_service_objects(self):
        ''' Return a list of classes for services that are currently running or should be active (as defined in the conf) for this node. '''
        return self._get_service_objects_by_name(self._get_service_objects(), self._get_enabled_or_running_service_names())


    def _get_service_objects(self):
        ''' Return a map of ServiceName->class objects by auto-discovering services in config (SERVICE_xxx) and loading the associated python class. '''
        service_objects = {}
        for service_name in self.get_service_names():
            service_objects[service_name] = self.get_service_object_by_name(service_name)
        return service_objects


    def get_service_object_by_name(self, service_name):
        ''' Given the name of a service, find the .py file that defines it and return an instantiated instance of the class.
            The service should be deined in a file under ./services/<name>/<name>_service.py.
            E.g.:
                redis is loaded from class RedisService in ./services/redis/redis_service.py
            We support subclassing services with an '_' in the name, like this:
                redis_readonly is loaded from class RedisReadonlyService
            Subclassed services are loaded from three possible file paths, most-specific first, like this:
                         ./services/redis_readonly/redis_readonly_service.py
                         ./services/redis/redis_readonly_service.py
                         ./services/redis/redis_service.py

            Returns None on error.
        '''

        try:
            parent_service_name = None
            service_classname = service_name.capitalize() + 'Service'

            if service_name.find('-') > 0:
                parent_service_name = service_name[0:service_name.find('-')]
                service_classname = parent_service_name.capitalize() + service_name[service_name.find('-')+1:].capitalize() + 'Service'

            service_dir = os.path.join(self.get_project_base_dir(), 'services', service_name)
            if not os.path.isdir(service_dir) and parent_service_name is not None:
                service_dir = os.path.join(self.get_project_base_dir(), 'services', parent_service_name)

            service_py_filename = '%s_service.py' % service_name.replace('-','_')
            src_path = os.path.join(service_dir, service_py_filename)
            if not os.path.isfile(src_path) and parent_service_name is not None:
                service_py_filename = service_py_filename = '%s_service.py' % parent_service_name
                src_path = os.path.join(service_dir, service_py_filename)

            if not os.path.exists(src_path):
                print >>sys.stderr, "Error: Can't find service class for service '%s'." % (service_name)
                return None

            if service_dir not in sys.path:
                # Add the service_dir to the path, so that services can do relative imports -- needed for parent imports as well as services that define any sort of common stuff:
                sys.path.insert(0, service_dir)

            service_module = None
            '''
            # Disabling following .pyc loader; seeing random occasional errors and want to see if this is related.
            # PicklingError: Can't pickle <class 'DseService.DseService'>: it's not the same object as DseService.DseService
            if os.path.exists(src_path + 'c'):
                pyc_fh = open(src_path + 'c')
                pyc_fh.seek(4)
                packed_time = pyc_fh.read(4)
                pyc_src_time = int(struct.unpack("i", packed_time)[0])
                if os.stat(src_path).st_mtime > pyc_src_time:
                    try:
                        os.remove("%sc" % src_path)
                    except:
                        print >>sys.stderr, "Stale .pyc file (%sc)" % src_path
                else:
                    try:
                        service_module = imp.load_compiled(service_classname, src_path + 'c')
                    except Exception as e:
                        print >>sys.stderr, "Error: failed to load %sc: %s" % (src_path, e)  # Hint: if the .pyc was compiled with a different version of python, you'll get a "Bad Magic Number" error.
            '''
            if service_module is None:
                service_module = imp.load_source(service_classname, src_path)
            service_class = getattr(service_module, service_classname)

        except (KeyError, ImportError, AttributeError) as e:
            print >>sys.stderr, "Error: unable to load service '%s': %s" % (service_name, e)
            return None

        try:
            return service_class(self)
        except Exception as e:
            print >>sys.stderr, "Error: unable to instantiate service '%s':\n%s" % (service_name, traceback.format_exc(e))
        return None


    def _get_service_objects_by_name(self, service_objects, service_names):
        if service_names is None:
            print >>sys.stderr, 'Warning: service_names is None!'
            return {}
        def service_name_to_service_object(service_name):
            if service_name not in service_objects:
                print >>sys.stderr, "Error: unable to find service class for service '%s'. (Is %s_SERVICE defined?)" % (service_name, service_name.replace('-','_').upper())
                return None
            return service_objects[service_name]
        return map(service_name_to_service_object, service_names)


    def _run_verb_on_services(self, services, verb, run_in_parallel, args=None, kwargs=None, timeout=None):
        ''' Given a list of service objects and the name of a function, run service_object.function()
            Function should return either an int or a dictionary with a key 'state'.
            Returns two values: first value is an int, 0 if all calls succeeded, 1 if any call returned non-zero;
                                second value is an array of all the returns from each service's verb call
        '''

        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}

        if len(services) == 0:
            print >>sys.stderr, 'Warning: no services supplied, %s command will have no affect.' % str(verb)
            return 0, None

        if verb is not 'trigger_status':
            log_to_syslog('calling services.%s on following services: %s' % (verb, ', '.join(map(lambda a: a.__class__.__name__.replace("Service",""),services))))

        if len(services) == 1:
            run_in_parallel = False
        else:
            services = sorted(services)  # Make sure runs are consistent

        return_values = None
        return_dict = {}

        if run_in_parallel:
            '''
            # Using threads won't work -- no sane way to kill a thread, so timeouts fail? Maybe sigalarm in the main thread?
            threads = {}
            results_queue = Queue.Queue()
            def _run_verb_on_service(service):
                the_method = getattr(service, verb)
                ret_val = the_method(*args, **kwargs)
                results_queue.put((service, ret_val))

            for service in services:
                print >>sys.stderr, service
                t = multiprocessing.Process(target=_run_verb_on_service, args=(service,))
                t.daemon = True
                if service in threads:
                    print >>sys.stderr, "Error: multiple instances of %s in services?" % service
                else:
                    threads[service] = t
                t.start()

            while len(threads):
                (service, ret_val) = results_queue.get()
                print >>sys.stderr, ret_val
                return_dict[ service.__class__.__name__ ] = ret_val
                if service in threads:
                    del threads[service]
                else:
                    print >>sys.stderr, "How did %s go missing?" % service
                print >>sys.stderr, threads
            '''

            def _keyboard_interrupt_handler(signum, frame):
                print >>sys.stderr, "Warning: ctrl-c ignored during %s" % verb
                return
            old_sigint_handler = signal.signal(signal.SIGINT, _keyboard_interrupt_handler)

            try:
                pool = None
                try:
                    pool = multiprocessing.Pool(len(services))
                except OSError: # Trap this: [Errno 12] Cannot allocate memory
                    print >>sys.stderr, "Can't create service pool; out of memory?"
                    return 1, None

                try:
                    return_values = pool.map(angel._HelperRunsVerbOnAService(verb, args, kwargs, timeout), services)
                except Exception as e:
                    print >>sys.stderr, "Error: %s command got exception %s: %s" % (verb, type(e), str(e))
                    # print >>sys.stderr, traceback.format_exc(e) Don't bother doing this, it's a strack trace from the wrong pool process...

                if return_values is None:
                    return 1, None

            finally:
                signal.signal(signal.SIGINT, old_sigint_handler)

        else:
            return_values = map(angel._HelperRunsVerbOnAService(verb, args, kwargs, timeout), services)

        # The order of values in return_values matches the order of values in services array.
        # For convenience, create a dict that's key -> value based and return that instead.
        if return_values:
            for i in range(len(services)):
                return_dict[ services[i].__class__.__name__ ] = return_values[i]

        # Do a big "OR" on the return-values to see if any service returned non-zero:
        services_with_errors = ()
        for name in return_dict:
            val = return_dict[name]
            if isinstance(val, int):
                if val != 0:
                    services_with_errors += (name,)
            elif isinstance(val, dict):
                if val.has_key('state'):
                    if val['state'] != angel.constants.STATE_RUNNING_OK:
                        services_with_errors += (name,)
                else:
                   print >>sys.stderr, "Warning: service %s returned a dict without a 'state' key while running %s()" % (name, verb)
                   services_with_errors += (name,)

            else:  # if it's not a dict or int, something failed.
                print >>sys.stderr, "Warning: service %s returned nothing while running %s()" % (name, verb)
                services_with_errors += (name,)

        if 0 == len(services_with_errors):
            return 0, return_dict

        return 1, return_dict




    def service_start(self, timeout=None):
        ''' Start services.
            Return 0 on success, -1 if any service failes to start; -2 if services fail to show OK status within timeout seconds.
        '''

        # If services are already started, we'll check conf for any additional services that might be new and need starting;
        # so okay to continue.
        if not self.are_services_running():
            self.set_service_state(angel.constants.STATE_STARTING)

        # It's possible to start a service manually, using the "tool" command; so ignore ones that are already running:
        already_running_services = self.get_running_service_names()
        enabled_services = self.get_enabled_services()
        services_to_start = [n for n in enabled_services if n not in already_running_services]

        if len(already_running_services):
            print >>sys.stderr, "Services already running: %s" % ' '.join(already_running_services)
            print >>sys.stderr, "Services starting up now: %s" % ' '.join(services_to_start)

        if 0 == len(self.get_enabled_services()):
            print >>sys.stderr, 'Warning: no services are enabled.'

        if len(services_to_start) == 0:
            return 0

        ret_val = self._run_verb_on_services(self._get_service_objects_by_name(self._get_service_objects(), services_to_start), 'trigger_start', True)[0]

        self.set_service_state(angel.constants.STATE_RUNNING_OK)

        if ret_val != 0:
            return -1
        if timeout is not None:
            if not self.are_services_status_ok(wait_for_ok=True, timeout=timeout):
                print >>sys.stderr, "Error: not all services started within %s seconds" % (timeout)
                return -2
        return 0


    def service_stop(self, hard_stop=False):
        ''' Stop services (if running); return 0 on success, non-zero otherwise. '''

        if hard_stop:
            # hard_kill_all will terminate all processes listed in lockfiles, plus any descendant processes.
            # We'll call the traditional stop() logic after this, which will do any finalization that's necessary.
            hard_kill_all(self.get_supervisor_lockdir(), yes_i_understand_this_is_potentially_dangerous=True)

        running_service_names = self.get_running_service_names()
        if not self.are_services_running():
            if 0 == len(running_service_names):
                print >>sys.stderr, "Ignoring stop request; services aren't running."
                return 0
            print >>sys.stderr, "Warning: services are stopped, but some services (%s) are running, stopping them now." % ', '.join(running_service_names)
        else:
            self.set_service_state(angel.constants.STATE_STOPPING)
        if not len(running_service_names):
            print >>sys.stderr, "Warning: stopping services, but no services were running."
            ret_val = 0
        else:
            ret_val = self._run_verb_on_services(self._get_service_objects_by_name(self._get_service_objects(), running_service_names), 'trigger_stop', True)[0] # Note: 'True' means stop in parallel
        self.set_service_state(angel.constants.STATE_STOPPED)
        # List any processes that are still running, as a visibility safety-check -- should be empty:
        if 'RUN_AS_USER' in self.get_settings() and self.get_settings()['RUN_AS_USER']:
            try:
                if os.getuid() != pwd.getpwnam(self.get_settings()['RUN_AS_USER']).pw_uid:
                    os.system('ps -o pid,args -U "%s" | tail -n +2 | grep -v collectd' % (self.get_settings()['RUN_AS_USER']))
            except KeyError:
                raise angel.exceptions.AngelUnexpectedException("Can't get uid for RUN_AS_USER %s; does user exist?" % self.get_settings()['RUN_AS_USER'])
        self.clear_tmp_dir()
        return ret_val


    def service_restart(self, timeout=None):
        ''' Restart services; return 0 on success; non-zero otherwise. '''
        ret_val = self.service_stop()
        if ret_val != 0:
            print >>sys.stderr, "Warning: at least one service reported an error during stop."
        if self.are_services_running():
            self.service_stop()                   # Will stop services listed in running_services
        ret_val = self.service_start()            # Will start services listed in our config
        if ret_val != 0:
            return ret_val
        if timeout is not None:
            if not self.are_services_status_ok(wait_for_ok=True, timeout=timeout):
                print >>sys.stderr, "Error: not all services started within %s seconds" % (timeout)
                return 1
        return 0


    def service_repair(self, stop_unexpected_services=True, start_missing_services=True, repair_running_services=True):
        ''' Calls repair() on all valid running services, which will in turn call service_repair() on services with status other than ok/warn.
            We also call start on services that should be running and stop on services that shouldn't be running.
            Does nothing when services are stopped.
            Returns 0 on success, non-zero on error. '''
        if not self.are_services_running():
            return 0
        # Check what services might be added (start them), deleted (stop them), or already running (reload/repair them).
        conf_services = self.get_enabled_services()
        running_services = self.get_running_service_names()
        running_and_in_conf_services = list(set(running_services).intersection(set(conf_services)))
        running_but_not_in_conf_services = list(set(running_services).difference(set(conf_services)))
        in_conf_but_not_running_services = list(set(conf_services).difference(set(running_services)))
        if 'devops' in in_conf_but_not_running_services: in_conf_but_not_running_services.remove('devops') # devops doesn't actually run, inelegant solution for now.
        errors_seen = 0
        run_in_parallel = True
        service_classes = self._get_service_objects()
        if len(running_but_not_in_conf_services) and stop_unexpected_services:
            print >>sys.stderr, "Repair: stopping services: %s" % ', '.join(running_but_not_in_conf_services)
            if 0 != self._run_verb_on_services(self._get_service_objects_by_name(service_classes, running_but_not_in_conf_services), 'trigger_stop', run_in_parallel)[0]:
                errors_seen += 1
        if len(in_conf_but_not_running_services) and start_missing_services:
            print >>sys.stderr, "Repair: starting services: %s" % ', '.join(in_conf_but_not_running_services)
            if 0 != self._run_verb_on_services(self._get_service_objects_by_name(service_classes, in_conf_but_not_running_services), 'trigger_start', run_in_parallel)[0]:
                errors_seen += 1
        if len(running_and_in_conf_services) and repair_running_services:
            if 0 != self._run_verb_on_services(self._get_service_objects_by_name(service_classes, running_and_in_conf_services), 'trigger_repair', run_in_parallel)[0]:
                errors_seen += 1
        return errors_seen


    def service_status(self, services_to_check=None, format=None, timeout=13, run_in_parallel=True):
        ''' Check running or enabled services; or, if services_to_check is not None, the listed services. '''
        if services_to_check is None:
            # When checking all services, include devops service so we include system-level status and warnings.
            # This should get migrated out into some place cleaner.
            services_to_check = ['devops',]
            if self.are_services_running():
                services_to_check += self._get_enabled_or_running_service_names()
            else:
                # We can be "stopped" but have individual services manually started on us:
                services_to_check += self.get_running_service_names()
        services_objs_to_check = self._get_service_objects_by_name(self._get_service_objects(), services_to_check)
        return self._run_verb_on_services(services_objs_to_check, 'trigger_status', run_in_parallel, timeout=timeout)


    def service_reload(self, reload_code=True, reload_conf=True, flush_caches_requested=False):
        ''' Trigger service_reload() on all running services.
            reload_code: true if the application code has been changed
            reload_conf: true if the conf for the app has been changed
            flush_caches_requested: true if data for the system has been changed, i.e. DB reset, such that services that cache data might want to reset their caches
        '''
        service_classes = self._get_service_objects()
        running_services = self.get_running_service_names()
        self.service_repair(repair_running_services=False)  # This will start missing services and stop now-unwanted ones
        if len(running_services) == 0:
            if self.are_services_running():
                print >>sys.stderr, "Warning: no services to reload."
            else:
                if not reload_code:
                    # (Skip services stopped warning when doing a code reload -- this happens during upgrades...)
                    print >>sys.stderr, "Warning: services are stopped; nothing to reload."
            return 0
        run_in_parallel = False  # So, on Ubuntu 14, multiprocess seems to fail when there are args passed into the function
        return self._run_verb_on_services(self._get_service_objects_by_name(service_classes, running_services),
                                   'trigger_reload',
                                   run_in_parallel,
                                   args=(reload_code, reload_conf, flush_caches_requested))[0]


    def service_rotate_logs(self):
        return self._service_run_verb_on_all_services('rotateLogs')


    def service_decommission_precheck(self):
        return self._service_run_verb_on_all_services('decommission_precheck')


    def service_decommission(self):
        return self._service_run_verb_on_all_services('decommission')


    def _service_run_verb_on_all_services(self, verb, run_in_parallel=True):
        all_services = self.get_service_names()
        service_classes = self._get_service_objects()
        return self._run_verb_on_services(self._get_service_objects_by_name(service_classes, all_services), verb, run_in_parallel)[0]


    def decommission(self, force=False):
        ''' Tell all services to stop accepting new requests, drain any data on this node as required,
            and halt all services in preparation for node termination. '''

        if self.is_decommissioned():
            print >>sys.stderr, "Error: node has already been decommissioned."
            return 1

        if not self.are_services_running():
            print >>sys.stderr, "Error: can't decommission a stopped node."
            return 1

        if not force:
            if not self.are_services_status_ok():
                print >>sys.stderr, "Error: refusing to decommission a node with non-okay status."
                return 1

        if self.service_decommission_precheck() != 0:  # 0 isn't the same thing as None; check for 0!
            print >>sys.stderr, "Error: one or more services failed the decommission pre-check. Either there are services that don't support decommissioning, or there is an active issue preventing decommissioning this node."
            return 1

        self._mark_decommissioned('Decommission started')

        if self.service_decommission() != 0:
            print >>sys.stderr, "Error: one or more services failed to decommission. Node services are now in undefined state."
            self._mark_decommissioned('Decommission failed')
            return 1

        # Check the number of files under DATA_DIR; if non-zero, then we have data that's not been cleaned up for decommissioning.
        data_files = os.listdir(self.get_settings()['DATA_DIR'])
        if 'decommissioned' in data_files: data_files.remove('decommissioned')
        # Bug: this doesn't skip decommissioned dir. But data_files count doesn't handle empty dir condition. To come back to later...
        file_count = sum(len(list(fs)) for _, _, fs in os.walk(self.get_settings()['DATA_DIR']))
        if 0 != file_count:
            print >>sys.stderr, "Error: one or more services still has data under DATA_DIR. Services should either delete the data or move it under <DATA_DIR>/decommissioned."
            return 1

        self._mark_decommissioned('Decommission finished')


    def is_decommissioned(self):
        ''' Check if the system has been decommissioned. In a pinch, one can reset the decommissioned state by removing the /.devops-decommissioned file.'''
        if os.path.exists('/.angel-decommissioned'):
            return True
        return False


    def _mark_decommissioned(self, message):
        try:
            line = "%s: %s" % (time.time(), message)
            open('/.angel-decommissioned', 'wa').write(line)
            log_to_syslog(line)
        except:
            return 1
        return 0


    def activate_version_and_reload_code(self, branch, version,
                                         force=False,
                                         jitter=0,
                                         skip_reload=False,
                                         downgrade_allowed=False,
                                         download_only=False,
                                         wait_for_ok=False,
                                         wait_for_ok_timeout=600):
        """Install, activate, and reload (when services running) to the given version and branch.
         If version is None, then we'll attempt to switch to and activate the newest version."""

        if branch == self.get_project_code_branch() and version == self.get_project_code_version():
            # Always no-op when trying to activate the current version.
            return

        if not self._angel_version_manager:
            raise angel.exceptions.AngelVersionException("Not a versioned install")

        if not force and self.are_services_running() and self.get_project_code_branch() != branch:
            raise angel.exceptions.AngelVersionException("Refusing to switch branches while code running; use --force")

        if not self._angel_version_manager.is_version_installed(branch, version):
            # If we get here, we need to somehow install branch/version, where version might be None,
            # and then learn what version was installed and what path it's at.
            installed_version, path_to_code = ("0.00", "/tmp/unknown")  # jitter! force!
            self._angel_version_manager.add_version(branch, installed_version, path_to_code)

        if download_only:
            # Purge old versions here when doing download-only, so we don't stack up lots of versions.
            # We normally wait to do this, so that download to activation time is shorter.
            self._angel_version_manager.delete_stale_versions(branch, self._settings['SYSTEM_INSTALLED_VERSIONS_TO_KEEP'])
            return 0

        version_to_activate = version
        if version_to_activate is None:
            version_to_activate = self._angel_version_manager.get_highest_installed_version_number(branch)
        self._angel_version_manager.activate_version(branch, version_to_activate,
                                                     downgrade_allowed=downgrade_allowed,
                                                     jitter=jitter)

        try:
            if not skip_reload:
                pid = os.fork()
                if pid:
                    (wait_pid, wait_exitcode) = os.waitpid(pid, 0)
                    if wait_exitcode != 0 or wait_pid != pid:
                        raise angel.exceptions.AngelVersionException("Failed to reload services correctly " +
                                                                     "(exit %s for pid %s)" % (wait_exitcode, wait_pid))
                else:
                    self._angel_version_manager.exec_command_with_version(branch, version_to_activate,
                                                                          self._project_entry_script,
                                                                          ("service", "reload", "--code-only"))
        finally:
            self._angel_version_manager.delete_stale_versions(branch, self._settings['SYSTEM_INSTALLED_VERSIONS_TO_KEEP'])

        if wait_for_ok:
            if self.are_services_running():
                # We exec using the new version, so that status info is determined using the new version.
                pid = os.fork()
                if pid:
                    (wait_pid, wait_exitcode) = os.waitpid(pid, 0)
                    if wait_exitcode != 0 or wait_pid != pid:
                        raise angel.exceptions.AngelVersionException("Unable to get ok status " +
                                                                     "(exit %s for pid %s)" % (wait_exitcode, wait_pid))
                else:
                    self._angel_version_manager.exec_command_with_version(branch, version_to_activate,
                                                                          self._project_entry_script,
                                                                          ("status",
                                                                           "--wait=%s" % wait_for_ok_timeout))


class AngelArgParser():
    '''Parses command-line options. We need support for tree-based arg parsing; so python's argparse won't work.'''

    _args = None
    _project_name = None  # Name of the project, used in X_<setting> overrides, /usr/lib/X/, /etc/X/conf.d paths
    _project_base_dir = None  # Path to top level directory for the project's codebase
    _project_entry_script = None  # Path to the script that started us, relative to base_dir

    def __init__(self, args, project_name, project_base_dir, project_entry_script):
        if len(sys.argv) < 1:
            raise angel.exceptions.AngelArgException("No arguments given (sys.argv).")
        self._args = args[:]  # [:] to make a copy of args, so we can safely pop/edit it
        self._project_name = project_name
        self._project_base_dir = project_base_dir
        self._project_entry_script = project_entry_script


    def need_to_run_make(self):
        """Check if we should run Make.
        If args include --skip-make, or if we have an ENV var named <project>_SKIP_MAKE set, we'll return false.
        Calling this function will set the ENV var, so it'll only return true exactly once, and only when Make
        can and should be run."""
        if '%s_SKIP_MAKE' % self._project_name.upper() in os.environ:
            return False
        os.environ['%s_SKIP_MAKE' % self._project_name.upper()] = '%s' % os.getpid()

        # If we don't have a Makefile, are run with --skip-make, or are calling "make" or "git" commands, don't run:
        if not os.path.isfile(os.path.join(self._project_base_dir, "Makefile")):
            return False
        for arg in self._args:
            if arg.startswith('--'):
                if arg == '--skip-make':
                    return False
            else:
                # First non "--" arg -- if it's "make" then we also don't want to run make.
                if arg == "make" or arg == "git":
                    return False

        # If we get here, then we have a Makefile and it should be triggered:
        return True


    def run_make(self, settings, target="build"):
        """Run the given target in make as a subprocess. Where possible, we'll give status info as make runs."""

        # Import lock functions:
        from devops.file_and_dir_helpers import get_lock, release_lock, is_lock_available, who_has_lock

        pid = os.fork()
        if 0 == pid:
            if 0 != get_lock(settings, 'check-env', lock_timeout=0.5, print_errors=False):
                print >>sys.stderr, "Error: unable to verify dependencies (can't get lock)."
                print >>sys.stderr, "   * Process %s appears to be running a build in another terminal." % \
                                    who_has_lock(settings, 'check-env')
                print >>sys.stderr, "   * Use --skip-make to skip dependency checks."
                sys.exit(2)
            try:
                # Use '1>&2' to redirect STDOUT to STDERR, in case a Makefile prints anything to STDOUT.
                # Need to do this in an os.system call, not os.exec*, so that our finally call can release the lock.
                j_flag = ''  # "-j 2" -- but this is failing on Yosemite
                build_val = os.system("cd '%s' && make %s build 1>&2" % (self._project_base_dir, j_flag))
                (build_exit, build_signal) = (build_val >> 8, build_val % 256)
                if build_exit or build_signal:
                    print >>sys.stderr, "\nError building dependencies (make failed: %s/%s)." % \
                                        (build_exit, build_signal)
                    sys.exit(1)
                sys.exit(0)
            finally:
                release_lock(settings, 'check-env')

        # Parent process waits for make to exit:
        # (We wait for a few seconds, and if still running, show a status message.)
        try:
            old_sigalrm = signal.signal(signal.SIGALRM, lambda: None)
            signal.alarm(8)
            still_running = False
            build_exit = build_signal = None  # If make has an error, one of these will get set to non-zero value
            try:
                (pid, build_val) = os.waitpid(pid,0)
                (build_exit, build_signal) = (build_val >> 8, build_val % 256)
            except:
                still_running = True
            signal.alarm(0)
            old_sigalrm = signal.signal(signal.SIGALRM, old_sigalrm)

            # If we're still running, give developer some clues about progress:
            if still_running:
                if os.path.isfile('/usr/bin/osascript') and os.path.exists('/usr/local/bin/watch'):
                    # Run osa script to fire off an Applescript event that pops open a new Terminal window,
                    # where the terminal window runs the watch command that's trimmed down to just the pid of the
                    # child process we just forked above. There's some additional cut/tail/sed stuff going on here
                    # to remove out extraneous stuff.
                    script = "osascript >/dev/null 2>/dev/null " + \
                             "-e 'tell application \"Terminal\" to do script " + \
                             "\"watch --no-title -n 0.25 " + \
                                 "\\\"pstree -p %s | tail -n+10 | cut -b 18- | sed -e \\\\\\\"s: `whoami` :   :\\\\\\\" && kill -0 %s || kill $PPID\\\"" % (pid, pid) + \
                             "; exit\"'"
                    os.system(script)
                (pid, build_val) = os.waitpid(pid,0)
                (build_exit, build_signal) = (build_val >> 8, build_val % 256)
            if build_exit or build_signal:
                sys.exit(1)
        except KeyboardInterrupt:
            os.kill(pid, signal.SIGTERM)
            sys.exit(1)


    def create_angel(self):
        # To-do: add initial --foo parsing here, along with handling delayed starts, version switches, etc
        #        add --conf-dir option, adding that to conf_paths (and setting ENV so any subcalls see it?)
        #        add --get-autocomplete option that knows to skip over delayed start

        ro_conf_paths = ("%s/services/*/*.conf" % self._project_base_dir,
                         "%s/code/conf/*.conf" % self._project_base_dir)

        rw_conf_path = None
        # To-do: add --conf-dir load here

        conf_dir_override_env_name = '%s_CONF_DIR' % self._project_name.upper()
        if conf_dir_override_env_name in os.environ:
            if rw_conf_path is None:
                rw_conf_path = os.environ[conf_dir_override_env_name]
                if not os.path.isdir(rw_conf_path):
                    raise angel.exceptions.AngelSettingsException("Conf dir set in ENV (%s: %s) doesn't exist" % \
                                                                  (conf_dir_override_env_name, rw_conf_path))
            else:
                print >>sys.stderr, "Warning: --conf-dir option masking ENV conf dir %s" % \
                                    (os.environ[conf_dir_override_env_name])

        dot_path = os.path.expanduser("~/.%s/conf" % self._project_name)
        etc_path = "/etc/%s/conf.d" % self._project_name

        if rw_conf_path is None:
            if os.path.isdir(dot_path):
                rw_conf_path = dot_path + "/*.conf"
                if os.path.isdir(etc_path):
                    print >>sys.stderr, "Warning: %s exists, ignoring settings unders %s" % (dot_path, etc_path)

        if rw_conf_path is None:
            if os.path.isdir(etc_path):
                rw_conf_path = etc_path + "/*.conf"

        if rw_conf_path is None:
            # Then no conf dir exists -- for root, use /etc; for non-root, use ~/.tappy
            # This will generally trigger the creation of the dir right away.
            if os.getuid() == 0:
                rw_conf_path = etc_path + "/*.conf"
            else:
                rw_conf_path = dot_path + "/*.conf"

        # conf_env_prefix lets users set env vars of the form <PROJECT>_SETTING_<NAME>=foo to set the setting SOME_VAR to foo:
        settings = angel.AngelSettings(env_conf_override_prefix="%s_SETTING_" % self._project_name.upper(),
                                       ro_conf_paths=ro_conf_paths,
                                       rw_conf_path=rw_conf_path)

        # Set up environment:
        if settings.is_set('BIN_PATHS'):
            bin_paths = ""
            for p in settings['BIN_PATHS'].split(':'):
                bin_paths += os.path.join(self._project_base_dir, p) + ":"
            if not os.environ['PATH'].startswith(bin_paths):
                os.environ['PATH'] = bin_paths + os.environ['PATH']

        # Increase open file resource limits:
        # (We do this before anything else so that all processes inherit the increased limit;
        # necessary because sudo -s on Ubuntu isn't't configured to apply /etc/security/limits.)
        try:
            import resource
            (soft_num_file_limit, hard_num_file_limit) = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft_num_file_limit < 50000 or hard_num_file_limit < 50000:  # If above 50k, don't bother
                try:
                    # Try to increase the number of open files allowed:
                    if os.getuid() == 0:
                        # If running as root, double-up until we fail to get highest-possible value:
                        limit = 1024
                        while True:
                            resource.setrlimit(resource.RLIMIT_NOFILE, (limit,limit))
                            limit *= 2
                    else:
                        # If running as non-root, can't double-up; once a limit is *explicitly* set, it can't be increased.
                        try:
                            # This should work on Linux with our /etc/security/limits.d/hsn-limits.conf:
                            resource.setrlimit(resource.RLIMIT_NOFILE, (100000,100000))
                        except:
                            # On OS X, we'll fail the linux setrlimit; re-try with the 10240 limit:
                            resource.setrlimit(resource.RLIMIT_NOFILE, (10240,-1))
                except Exception as e:
                    pass
            # Check that the max file limit was increased; if not, warn:
            (soft_num_file_limit, hard_num_file_limit) = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft_num_file_limit < 10000 or hard_num_file_limit < 10000:
                print >>sys.stderr, "Warning: open file limit is low (%s/%s)." % (soft_num_file_limit, hard_num_file_limit)
            del resource

        except Exception as e:
            print >>sys.stderr, "Warning: error setting file limits (%s)." % e

        # Create angel object:
        new_angel = angel.Angel(self._project_name, self._project_base_dir, self._project_entry_script, settings)

        # When a Makefile is present, we call make to verify that any required project dependencies are ready to go.
        # "make" is expected to run quickly and silently when there is nothing that needs updating.
        if self.need_to_run_make():
            self.run_make(settings)

        return new_angel




class AngelSettings():

    _setting_values = {}
    _setting_src = {}

    _ro_conf_paths = None
    _rw_conf_path = None
    _env_conf_override_prefix = None

    def __init__(self, env_conf_override_prefix=None, ro_conf_paths=(), rw_conf_path=None):
        '''Initialize the settings for an angel project.
         env_conf_override_prefix: should be an alpha-numeric string, usually the name of the control script, that is used
         for env variable overrides and template strings.
         conf_paths: optional list of additional directories to read conf settings from
         (later items will take precedence).
        '''
        self._ro_conf_paths = ro_conf_paths
        self._rw_conf_path = rw_conf_path
        self._env_conf_override_prefix = env_conf_override_prefix
        self._load_settings()


    def _load_settings(self):
        """Load settings from the Angel default settings, conf dirs, and ENV variables.

    Settings follow an order-of-definition precedence:
    @param _conf_paths: list of wildcard paths for reading in config settings; later paths override earlier paths; globs are sorted alphabetically
    @param _env_conf_override_prefix: string, typically "<PROJECT>_SETTING_", for matching environment variable overrides
    """
        for i in dir(angel.settings.defaults):
            if i.startswith('_'):
                continue  # Skip python internal objects
            self.set(i, angel.settings.defaults.__dict__[i], '(Angel: %s)' % angel.settings.defaults.__file__)
        for i in (self._ro_conf_paths + (self._rw_conf_path,)):
            # Make sure rw path comes after ro paths!
            if 0 == len(i):
                continue
            i_wildcard_expansion = sorted(glob.glob(i))
            for j in i_wildcard_expansion:
                self._import_settings_from_conf_file(j)
        if self._env_conf_override_prefix and len(self._env_conf_override_prefix):
            for i in os.environ:
                if i.startswith(self._env_conf_override_prefix):
                    key = i[len(self._env_conf_override_prefix):]
                    value = os.environ[i]
                    if not self.is_set(key):
                        if not key.startswith('APP_'):
                            # Don't print override-unknown warnings for APP_* settings; angel won't ever know about them
                            print >>sys.stderr, "Warning: ENV override %s doesn't match any known setting." % key
                    else:
                        try:
                            # If the non-ENV override value is None, and the ENV value is the string "None",
                            # we're going to assume that the value shoule be None, as opposed to an actual string.
                            # This does mean one can't override a None value with a string that's "None".
                            if self._setting_values[key] is None and value == 'None':
                                value = None
                            else:
                                prior_type = type(self._setting_values[key])
                                if prior_type == type(True):
                                    # Then it's a boolean, and bool("False") is True, which is stupid.
                                    if value.lower() == 'false':
                                        value = False
                                    elif value.lower() == 'true':
                                        value = True
                                    else:
                                        print >>sys.stderr, "Warning: unable to cast ENV override %s to true or false." % key
                                else:
                                    value = prior_type(value)
                        except:
                            print >>sys.stderr, "Warning: unable to cast ENV override %s to correct type." % key
                    self.set(key, value, '(ENV: %s)' % key)


    def set(self, key, value, src):
        self._setting_values[key] = value
        self._setting_src[key] = src


    def get(self, key):
        if key in self._setting_values:
            return self._setting_values[key]
        raise angel.exceptions.AngelSettingsException("No setting '%s'" % key)


    def get_src(self, key):
        if key in self._setting_src:
            return self._setting_src[key]
        raise angel.exceptions.AngelSettingsException("No src for setting '%s'" % key)


    def is_set(self, key):
        if key in self._setting_values:
            return True
        return False


    def _import_settings_from_conf_file(self, path):
        self._import_settings_from_string(open(path, 'rt').read(), data_src=path)


    def _import_settings_from_string(self, data_string, data_src='(unknown)'):
        """ Given a string like:
            key=value\nkey2=value2
        Return a dict with data[key] = values.
        - Ignores all lines that start with # or are empty.
        - Throws AngelSettingsException on any parse errors.
        - Strips leading and trailing white-space on a line
        """
        key_value_separator = '='
        line_counter = 0
        if not isinstance(data_string, str):
            raise angel.exceptions.AngelSettingsException("Can't parse a non-string value")
        try:
            for line in data_string.split("\n"):
                line_counter += 1
                line = line.lstrip().rstrip()
                if line.startswith('#'): continue
                if 0 == len(line): continue
                (key, value) = line.split('=', 1)
                key = key.rstrip()
                value = value.lstrip().rstrip()
                # Cast strings to the basic python types that we support:
                if value.startswith('\'') or value.startswith('"'):
                    # Need to cleave off any trailing comments
                    split_value = shlex.split(value)
                    if len(split_value) > 1:
                        if '#' != split_value[1]:
                            raise angel.exceptions.AngelSettingsException("Invalid comment format on line %s of %s" % (line_counter, data_src))
                    value = split_value[0]
                else:
                    # Then it's not a string -- easy case, cleave off any potential comment and parse the value:
                    if value.find('#') >= 0:
                        value = value.split('#', 1)[0].rstrip()
                    if value == 'True':
                        value = True
                    elif value == 'False':
                        value = False
                    elif value == 'None':
                        value = None
                    elif value.find(".") >= 0:
                        try:
                            value = float(value)
                        except:
                            raise angel.exceptions.AngelSettingsException("Invalid float on line %s of %s" % (line_counter, data_src))
                    else:
                        try:
                            value = int(value)
                        except:
                            raise angel.exceptions.AngelSettingsException("Invalid entry on line %s of %s" % (line_counter, data_src))

                self.set(key, value, data_src)
        except Exception as e:
            raise angel.exceptions.AngelSettingsException("Parse error on line %s, %s (%s)" % (line_counter, data_src, e))


    def __getitem__(self, name):
        if name in self._setting_values:
            if isinstance(self._setting_values[name], basestring) and self._setting_values[name].startswith('~'):
                return os.path.expanduser(self._setting_values[name])
            return self._setting_values[name]
        if name == '.NODE_ENABLED_SERVICES':
            print >>sys.stderr, "Error: NODE_ENABLED_SERVICES called; returning empty string."
            return ""
        raise AttributeError


    def __setitem__(self, name, value):
        raise angel.exceptions.AngelSettingsException("Settings should be immutable; use set() if you really need to.")


    def __iter__(self):
        return iter(self._setting_values)


    def export_settings_to_string(self, comment='', override_dict=None):
        """ Export settings to a string.
            @param comment: optional comment to add into exported file; handy for including timestamps, etc
            @param override_dict: keys defined here will override values defined in the settings object
        """

        data = ''
        if len(comment):
            data += '# %s\n' % comment

        if override_dict is None:
            override_dict = {}

        for key in override_dict:
            if key not in self._setting_values:
                print >>sys.stderr, "Warning: export_settings given an override for %s but no such setting exists." % key

        for key in sorted(self._setting_values.keys()):
            if key[0] == '.': continue # Don't export .Variables
            value = repr(self._setting_values[key])
            if key in override_dict:
                value = repr(override_dict[key])
            data += '%s=%s\n' % (key, value)

        return data


    def export_settings_to_file(self, filename, comment='', override_dict=None):
        """ Export settings to the given filename, returning 0 on success or non-zero if problems.
        @param filename: path to file to write
        @param comment: optional comment to add into exported file; handy for including timestamps, etc
        @param override_dict: keys defined here will override values defined in settings dict
        """
        filename = os.path.expanduser(filename)
        data = self.export_settings_to_string(comment=comment, override_dict=override_dict)
        if os.path.isfile(filename):
            # Read the file to see if there are any changes.
            # We check in case there are no changes and we don't have write permission -- this way we can continue anyway.
            current_data = open(filename, 'r').read()
            if current_data == data:
                return
        try:
            tmp_filename = "%s-%s-%s" % (filename, int(time.time()), os.getpid())
            open(tmp_filename, 'wt').write(data)
            os.rename(tmp_filename, filename)  # Atomic operation, in case we fail mid-way though writing data
        except IOError as e:
            raise angel.exceptions.AngelSettingsException("Unable to export settings to file %s (%s)." % (filename, e))


    def export_settings_to_tmpfile(self, comment='', override_dict=None):
        settings_as_string = self.export_settings_to_string(comment=comment, override_dict=override_dict)
        settings_checksum = angel.util.checksum.get_checksum(settings_as_string)[0:8]
        settings_filepath = os.path.join(os.path.expanduser(self.get('TMP_DIR')), 'angel-settings-%s.conf' % settings_checksum)
        if not os.path.isfile(settings_filepath):
            self.export_settings_to_file(settings_filepath)
        return settings_filepath


    def set_override_and_save(self, key, value):
        '''Update the conf files to set key to given value.'''
        self._update_conf_files(key, value)


    def unset_override_and_save(self, key):
        '''Delete any conf setting for given key that is defined under (non-code) config dirs.'''
        self._update_conf_files(key, None, delete=True)


    def _update_conf_files(self, key, value, delete=False):
        ''' Helper function for setting/unsetting conf overrides.'''
        key_src_location = None
        if key in self._setting_src:
            key_src_location = self._setting_src[key]

        if delete:
            if not key_src_location:
                raise angel.exceptions.AngelSettingsException("Can't delete non-existent setting '%s'" % key)
            if not os.path.isfile(key_src_location):
                raise angel.exceptions.AngelSettingsException("Can't delete %s from %s: setting file missing" % (key, key_src_location))
            if key_src_location not in glob.glob(self._rw_conf_path):
                raise angel.exceptions.AngelSettingsException("Can't delete %s from %s (not under %s); try setting a new value as an override?" % (key, key_src_location, self._rw_conf_path))
            raise angel.exceptions.AngelSettingsException("Not implemented: need to delete %s from %s" % (key, key_src_location))

        if key_src_location is None:
            # Allow unknown settings to be set in case we're pre-defining a key before an upgrade or using a programatically-referenced setting.
            print >>sys.stderr, "Warning: no default for setting %s; setting it anyway." % key

        # Easy case: we're setting a new override
        if key_src_location not in glob.glob(self._rw_conf_path):
            # Then we're setting a new override -- easy case, append it to a .conf file:
            raise angel.exceptions.AngelSettingsException("Not implemented: need to create %s=%s" % (key, value))

        # Harder case: we're editing an existing override
        raise angel.exceptions.AngelSettingsException("Not implemented: need to update %s=%s in file %s" %
                                                      (key, value, key_src_location))

        # If key is in a ro file, then need to add it to a rw file, otherwise need to find it in rw path and edit it
        """
        All of this is ported over from old code:
        def _get_new_conf_line():
            with_quotes = True
            if key in config and not isinstance(config[key], basestring):
                with_quotes = False
                if config[key] is None:
                    # Clunky, but necessary: if the default is None, then treat this as a string.
                    with_quotes = True
            if with_quotes:
                return '%s="%s"\n' % (key, value.replace('\\', '\\\\').replace('"', '\\"'))
            else:
                return '%s=%s\n' % (key, value)


        has_key_been_seen = False
        for filename in glob.glob('%s/*.conf' % config['CONF_DIR']):
            new_data = ''
            file_needs_updating = False
            lines = open(filename).readlines()
            for line in lines:
                # Check against a version that removes leading '# ', so that
                # when we "set, unset, set", we reuse the line that was previously commented out:
                comment_cleared_line = line
                if comment_cleared_line[0:2] == '# ':
                    comment_cleared_line = comment_cleared_line[2:]
                if comment_cleared_line[0] == '#':
                    comment_cleared_line = comment_cleared_line[1:]
                if comment_cleared_line.startswith(key) and (comment_cleared_line[len(key)] == '=' or comment_cleared_line[len(key)] == ' '):
                    file_needs_updating = True
                    if value is None or has_key_been_seen:
                        new_data += '# %s' % comment_cleared_line
                    else:
                        new_data += _get_new_conf_line()
                    has_key_been_seen = True
                else:
                    new_data += line

            if not file_needs_updating:
                continue  # continue, not break, so that we process all files to delete out any duplicate keys that a user may have accidentally set manually.
            try:
                open('%s.tmp' % filename, 'w').write(new_data)
                os.rename('%s.tmp' % filename, filename)
            except Exception as e:
                print >>sys.stderr, "Error: unable to update setting %s in %s (%s)." % (filename, key, e)
                return -2

        # If we didn't find the key and the new value is None, there's nothing to do:
        if not has_key_been_seen and value is None:
            print >>sys.stderr, "Warning: setting %s isn't defined in conf dir." % key
            return 0

        # Append the new key=value line to a settings file:
        if not has_key_been_seen:
            try:
                settings_file = os.path.join(config['CONF_DIR'], '%s_settings.conf' % key.split('_')[0].lower())
                current_file_needs_newline_before_value = False
                if os.path.exists(settings_file):
                    data = open(settings_file).read()
                    if len(data) and data[-1] != '\n':
                        current_file_needs_newline_before_value = True
                new_data = ''
                if current_file_needs_newline_before_value:
                    new_data = "\n"
                new_data += _get_new_conf_line()
                open(settings_file, 'a').write(new_data)

            except Exception as e:
                print >>sys.stderr, "Error: unable to add setting %s (%s)." % (key, e)
                return -4

        # Log to syslog when the setting was updated:
        # (Don't show value when key name has 'key' or 'secret' in it to avoid security issues with central logging; not fool-proof but a "better than nothing" approach.)
        if 'key' in key.lower() or 'secret' in key.lower():
            log_to_syslog("Settings: set config key %s" % (key))
        else:
            log_to_syslog("Settings: set config key %s to '%s'" % (key, value))

        # End of old code that needs reworking -- last bit of code that updates settings in memory below should be good to go.
        """

        # There's no sane way to re-parse everything after a delete other than to
        # create a new setting object and back-copy the found value. We do that for set calls as well,
        # so we get the correct file path for src value.
        reloaded_settings = AngelSettings(ro_conf_paths=self._ro_conf_paths,
                                          rw_conf_path=self._rw_conf_path,
                                          env_conf_override_prefix=self._env_conf_override_prefix)
        new_value = reloaded_settings.get(key)
        new_src = "(unknown)"
        try:
            new_src = reloaded_settings.get_src(key)
        except:
            pass
        self.set(key, new_src, new_value)





class _HelperRunsVerbOnAService:

    _verb = None
    _timeout = None
    _args = ()
    _kwargs = {}

    def __init__(self, verb, args, kwargs, timeout):
        self._verb = verb
        if args is not None:
            self._args = args
        if kwargs is not None:
            self._kwargs = kwargs
        if isinstance(timeout, int) and timeout > 0:
            self._timeout = timeout

    def __call__(self, service):
        if service is None:
            print >>sys.stderr, 'Error: null service object'
            return -1

        class TimeoutAlarm(Exception):
            pass

        name = str(service.getServiceName())

        def timeout_alarm_handler(signum, frame):
            #print >>sys.stderr, "TIMEOUT in %s" % service
            raise TimeoutAlarm("%s.%s" % (name, self._verb))

        ret_val = None
        old_sigalarm = None
        if self._timeout:
            old_sigalarm = signal.signal(signal.SIGALRM, timeout_alarm_handler)
            signal.alarm(self._timeout)

        try:
            set_proc_title('service %s: %s' % (name, self._verb))
        except:
            pass

        try:
            the_method = getattr(service, self._verb)
            ret_val = the_method(*self._args, **self._kwargs)
            result = ''
        except TimeoutAlarm:
            print >>sys.stderr, "Error: %s.%s() failed to return within %s seconds" % (service.__class__.__name__, self._verb, self._timeout)
            result = ': timeout'
        except SystemExit as e:
            print >>sys.stderr, "System exit from %s.%s()" % (service.__class__, service.__class__.__name__) # Some processes call sys.exit() -- things like redis-primer fork, run, then exit, which causes an exception here
            sys.exit(0)  # Exit here as well -- we're inside a pool, so the return above us will just generate a confusing stack trace and then exit anyway...
        except Exception as e:
            print >>sys.stderr, 'Error: unexpected exception during call in %s:\n%s' % (service.__class__.__name__, traceback.format_exc(e))
            result = ': exception'
        except:
            print >>sys.stderr, 'Error: unexpected %s error during call in %s:\n%s' % (sys.exc_info()[0], service.__class__.__name__, traceback.format_exc(sys.exc_info()[2]))
            result = ': error'

        try:
            set_proc_title('service %s: %s [Z%s]' % (self._verb, name, result))  # Z is for zombie... because with process pooling, this process is just waiting to be reaped... most likely.
        except:
            pass

        if self._timeout:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_sigalarm)

        return ret_val









def kill_after_x_seconds(duration):
    # This is a special utility function for command-line --duration option only.
    if duration < 1:
        print >>sys.stderr, 'Error: duration too short.'
        sys.exit(1)
    if duration > 60*60*24*7:
        print >>sys.stderr, 'Error: duration too long.'
        sys.exit(1)

    # Fork, sleep in parent / return in child, then kill child after duration seconds.
    pid = os.fork()
    if pid == 0:
        return

    def _child_killer_default(signum, frame):
        print >>sys.stderr, "kill -%s %s" % (signum, pid)
        os.kill(pid, signum)
    signal.signal(signal.SIGHUP, _child_killer_default)  # Pass along HUP/TERM to child as courtesy
    signal.signal(signal.SIGTERM, _child_killer_default)
    signal.signal(signal.SIGQUIT, _child_killer_default)
    signal.signal(signal.SIGUSR1, _child_killer_default)
    signal.signal(signal.SIGUSR2, _child_killer_default)

    # Use signals to find out when our child process exits, if it finishes before duration is expired:
    class ChildExitedException(Exception):
        pass
    def child_exit_handler(signum, frame):
        raise ChildExitedException()
    signal.signal(signal.SIGCHLD, child_exit_handler)

    # Sleep for requested duration; if we get interrupted before then, then exit as well.
    # The only time we exit with a zero exit code is if the job finishes before duration seconds and exits with 0 exit code.
    try:
        time.sleep(duration)
        log_to_syslog("Duration exceeded for pid %s; killing it." % pid)
        print >>sys.stderr, "Stopping process %s (%s seconds elapsed in --duration flag)." % (pid, duration)
    except ChildExitedException:
        (pid, exitcode) = os.wait()
        real_exitcode = exitcode / 256
        print >>sys.stderr, "Process %s exited before duration elapsed (exit code %s)." % (pid, real_exitcode)
        sys.exit(real_exitcode)
    except (KeyboardInterrupt):
        print >>sys.stderr, "Stopping process %s (user aborted)." % (pid)
    except Exception as e:
        print >>sys.stderr, "Stopping process %s (encountered exception: %s)." % (pid, e)

    # We can't just kill the process; we have to track all the descendant processes too.
    # This came up initially because bash's 'huponexit' doesn't seem to pass HUPs to grandchildren.
    # To fix this, we build a tree of all child processes, then do the proper kill,
    # then examine the tree for any stray child processes.

    processes_to_kill = get_all_children_of_process(pid)

    # First, the proper way: send sigterm / sigkill to our main process:
    timeout = 10
    if 0 != kill_and_wait(pid, 'angel', signal.SIGTERM, timeout):
        if 0 != kill_and_wait(pid, 'angel', signal.SIGKILL, timeout):
            log_to_syslog("Duration exceeded for pid %s; FAILED TO KILL IT. This should never happen." % pid)
            print >>sys.stderr, "Fatal error: pid %s not responding to SIGKILL?" % pid

    # Next, the brute-force way: track down all subprocesses and kill them:
    def _send_signal_to_processes(pid_list, signum):
        for pid in pid_list:
            try:
                os.kill(pid, signum)
                time.sleep(0.1)
            except:
                pass

    processes_to_kill = get_only_running_pids(processes_to_kill)
    if 0 == len(processes_to_kill):
        sys.exit(0)
    print >>sys.stderr, "Killing dangling processes: kill -TERM %s" % ' '.join(map(str,processes_to_kill))
    _send_signal_to_processes(processes_to_kill, signal.SIGTERM)

    time_to_wait = 10
    while time_to_wait > 0:
        try:
            time.sleep(0.5)
            time_to_wait -= 0.5
            if not is_any_pid_running(processes_to_kill):
                sys.exit(0)
        except KeyboardInterrupt:
            sys.exit(130)
        except Exception as e:
            print >>sys.stderr, "Error during wait: %s" % e
            pass

    processes_to_kill = get_only_running_pids(processes_to_kill)
    if 0 == len(processes_to_kill):
        sys.exit(0)
    print >>sys.stderr, "Killing dangling processes: kill -KILL %s" % ' '.join(map(str,processes_to_kill))
    _send_signal_to_processes(processes_to_kill, signal.SIGKILL)

    processes_to_kill = get_only_running_pids(processes_to_kill)
    if 0 == len(processes_to_kill):
        sys.exit(0)
    print >>sys.stderr, "Warning: zombie processes: %s" % ' '.join(map(str,processes_to_kill))
    sys.exit(1)

