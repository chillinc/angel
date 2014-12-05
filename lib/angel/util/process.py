
def get_process_title():
    global _set_process_title_value
    return _set_process_title_value
    # Don't use setproctitle.getproctitle(); it has additional formatting on it


_set_process_title_value = None
_set_process_title_version_info = None
def set_process_title(title, version_info=None, project_name=None):
    # We need the unformated title for use with our get_process_title wrapper, so quick solution is to cache it globally:
    global _set_process_title_value
    _set_process_title_value = title

    # We don't always have a way to get the version info (the config dict may not be available in a sub-function),
    # but as a hack, we'll shove it in a global and re-use it on future calls when missing. Ugly, but fast and works ok.
    global _set_process_title_version_info
    if version_info is not None:
        _set_process_title_version_info = version_info
    if version_info is None and _set_process_title_version_info is not None:
        version_info = _set_process_title_version_info

    # Generate a formatted process name, along with whatever title / status we're being given:
    slug = ''
    if project_name:
        slug = '[%s] ' % project_name
        if version_info is not None:
            slug = "[%s.%s] " % (project_name, version_info)
    if title is None or len(title) == 0:
        title = 'untitled process'
    slug += title
    slug = slug.replace('(','[').replace(')',']') # Do not allow ()'s in proc title, will screw up parsing in get_pid_relationships

    try:
        import setproctitle
        setproctitle.setproctitle(slug)
    except:
        pass
