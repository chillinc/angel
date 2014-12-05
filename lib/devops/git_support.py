import os
import sys
from devops.process_helpers import get_command_output


def git_is_git_useable(path_under_git_checkout=None):
    ''' Returns true if we're able to find a .git directory that has a "master" branch in it. '''
    if git_does_branch_exist('master', path_under_git_checkout=path_under_git_checkout) is not True:
        return False
    return True


def git_get_current_branch_name(path_under_git_checkout=None):
    ''' Return the name of the current branch, or None if not discoverable. '''
    dirpath = None
    if path_under_git_checkout is None:
        path_under_git_checkout = __file__  # This'll work as a fallback if *this* file is under the git checkout
    dirpath_candidate = os.path.realpath(__file__)
    while dirpath is None:
        if os.path.isdir(os.path.join(dirpath_candidate, ".git")):
            dirpath = dirpath_candidate
        else:
            dirpath_candidate = os.path.dirname(dirpath_candidate)
        if '/' == dirpath_candidate:
            return None

    # We don't use "git symbolic-ref HEAD" because that involves creating a subprocess,
    # which involves resetting SIGCHLD to avoid dpkg weirdness, but we can't do that inside other places.
    branch_name = None
    try:
        branch_name = open(os.path.join(dirpath, '.git', 'HEAD')).readline().rstrip().split('/')[2]
    except Exception as e:
        print >> sys.stderr, "Error: can't find .git/HEAD file (%s)." % e
        return None
    if len(branch_name) < 2: return None
    return branch_name


def git_does_branch_exist(branch, path_under_git_checkout=None):
    ''' Return true if the given branch name exists; false if not; and None if we can't tell. '''
    dirpath = os.path.dirname(os.path.realpath(__file__)) # In case we're outside the git checkout
    out, err, exitcode = get_command_output('git show-ref --verify --quiet "refs/remotes/origin/%s"' % branch,
                                            chdir=dirpath)
    if 0 == exitcode: return True
    if 1 == exitcode: return False
    return None  # If we're not inside a git checkout, git will exit 128
