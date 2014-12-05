import urllib
import urllib2
import base64
import re

def call_and_return_with_timing(f, *args, **kwargs):
    """Helper for calling a function that returns (result, duration) where result
       is the returned value, and duration is the datetime.timedelta of the call.
       Exceptions are not caught.
    """
    from datetime import datetime
    before = datetime.now()
    result = f(*args, **kwargs)
    after = datetime.now()
    return (result, after-before)

def get_total_seconds(td):
    """Returns total seconds represented by a datetime.timedelta object.
    """
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 1e6) / 1e6


# TODO(Jeff): Replace user/pass defaults with HEALTHCHECK_LOGIN_USERNAME and
#   HEALTHCHECK_LOGIN_PASSWORD, once they're defined.
def check_url(domain, rel_url, domain_port=80, threshold_seconds=10, login_user='dan', login_password='thinmint'):
    """Checks health of url, does login first. The rel_url param should start with a backslash.
    """
    try:
        # Setup
        cookie_handler= urllib2.HTTPCookieProcessor()
        opener = urllib2.build_opener(cookie_handler)
        urllib2.install_opener(opener)
        # POST user/pass
        login_url = 'http://%s:%s%s' % (domain, domain_port, '/account/login/')
        login_data = urllib.urlencode({'username': login_user, 'password':login_password, 'next':'/'})
        response = opener.open(login_url, data=login_data)
        if not response.code == 200:
            raise AssertionError('Bad login response code: %s' % response.code)
        # GET reuqested URL, capture duration.
        requested_url = 'http://%s:%s%s' % (domain, domain_port, rel_url)
        (response, time_delta) = call_and_return_with_timing ( lambda : opener.open(requested_url))
        duration = get_total_seconds(time_delta)
        if not response.code==200:
            raise AssertionError('Bad main response code: %s' % response.code)
        # Make sure userId contains our user
        returned_user = re.search('userId="([^"]*)"', response.read()).group(1)
        if not returned_user == login_user:
            raise AssertionError('Expected userId to be "%s", was "%s"' % (login_user, returned_user))
        # Formulate return values
        if duration <= threshold_seconds:
            state = 'OK'
            message = 'Check_url() succeeded, see data.'
            data = {'duration': '%.6f' % duration}
        else:
            state = 'WARN'
            data = {}
            message = 'Timeout exceeded.'
    except Exception, ex:
        state = 'ERR'
        message = 'Exception: %s' % repr(ex)
        data = {}

    # TODO(Jeff) - change to use the new stats/structs builder functions.
    return {'state':state, 'message': message, 'data':data}

# UNCOMMENT TO TEST:
# print check_url('preview.namesaketools.com', '/conversations/private')
