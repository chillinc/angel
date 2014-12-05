import glob
import os
import sys

from devops.generic_service import GenericService

class FakesmtpService(GenericService):
    '''
    Accepts SMTP messages and writes them to local archive directory without real delivery.
    '''

    def service_start(self):                                         
        args=('--host', self._config['FAKESMTP_HOST'],
              '--port', self._config['FAKESMTP_PORT'],
              '--max-messages', self._config['FAKESMTP_MAX_QUEUE_SIZE'],
              '--archive-dir', self._get_archive_dir())
        if self._config['FAKESMTP_AUTO_OPEN_ON_OS_X']:
            args += ('--auto-open',)
        return self.start_supervisor_with_binary(self.which('fakesmtpserver'), args)


    def service_status(self, ip=None):
        status_struct = self.checkStatusViaNagiosPlugin('check_smtp', ['-H', self._config['FAKESMTP_HOST'], '-t', '2', '-p', self._config['FAKESMTP_PORT']])
        if ip is not None:
            return status_struct
        mcount = self._get_message_count()
        if 0 == mcount:
            return status_struct
        replace_status=False
        if self.isStatStructStateOk(status_struct):
            replace_status=True
        self.updateStatStruct(status_struct, message='%s messages (See: tool fakesmtp list-messages)' % mcount,
                              replace_instead_of_append=replace_status)
        return status_struct



    def shell_tool_list_messages(self):
        ''' List archived messages. '''
        if 0 == self._get_message_count():
            print >>sys.stderr, "No messages."
            return 0
        return os.system("cd '%s' && grep -r -H Subject * | cut -b 9- | sed -e 's:.eml\:Subject\: :    :'" % self._get_archive_dir())


    def _get_message_count(self):
        if not os.path.isdir(self._get_archive_dir()):
            return 0
        return len(os.listdir(self._get_archive_dir()))


    def shell_tool_open(self, message_id=None):
        '''
        Open the mail spool directory in the Finder (OS X only).
           * message_id: Open given message instead
        '''
        open_path = self._get_archive_dir()
        if message_id is not None:
            open_path = os.path.join(self._get_archive_dir(), 'message-%s.eml' % message_id)
        if os.path.exists('/usr/bin/open'):
            return os.system('/usr/bin/open "%s"' % open_path)  # Unescaped wild-card, fyi.
        else:
            print "See: %s" % open_path
        return 0


    def shell_tool_show_message(self, message_id):
        '''
        Show a given message in the fakesmtp queue.
           * message_id: Open given message instead
        '''
        file = self._get_filepath_for_message_id(message_id)
        if file is None:
            print >>sys.stderr, "Error: no message with id '%s'." % message_id
            return 1
        print open(file).read()
        return 0


    def shell_tool_delete_message(self, message_id):
        '''
        Delete a message from the fakesmtp queue.
           * message_id: Open given message instead
        '''
        file = self._get_filepath_for_message_id(message_id)
        if file is None:
            print >>sys.stderr, "Error: no message with id '%s'." % message_id
            return 1
        try:
            os.remove(file)
        except:
            print >>sys.stderr, "Error: unable to remove message '%s' (path: %s); try sudo?" % (message_id, file)
            return 1
        return 0


    def _get_filepath_for_message_id(self, message_id):
        ''' Given a message id, return a path to the file for that id, or None if no exact match is found. '''
        file_path = glob.glob(os.path.join(self._get_archive_dir(), 'message-*%s*.eml' % message_id))
        if 0 == len(file_path):
            return None
        if 1 != len(file_path):
            print >>sys.stderr, "Error: multiple matches for message id '%s' (%s)." % (message_id, ', '.join(file_path))
            return None
        return file_path[0]


    def _get_archive_dir(self):
        ''' Return the directory path for the fakesmtp message queue. '''
        return os.path.join(self._config['CACHE_DIR'], 'fake-smtp-messages')


    def shell_tool_send_message(self, message_id):
        ''' Attempt to deliver a message for real (requires localhost:25 to be running).
           * message_id: Open given message instead
        '''
        file = self._get_filepath_for_message_id(message_id)
        if file is None:
            return 1

        # We rename the file to both prevent concurrent sends, and to check that we can unlink it after we're done:
        sending_file_path = os.path.join(self._get_archive_dir(), 'sending-%s.eml' % message_id)
        try:
            os.rename(file, sending_file_path)
        except:
            print >>sys.stderr, "Error: can't rename message; try sudo?"
            return 1

        ret_val = self.runCommand('sendmail', args=('-t', '-i'), stdin_string=open(sending_file_path).read())
        if 0 != ret_val:
            print >>sys.stderr, "Error: failed to send message."
            os.rename(sending_file_path, file)
            return 1

        try:
            os.remove(sending_file_path)
        except Exception as e:
            print >>sys.stderr, "Error: message sent, but unable to remove file %s (%s)." % (sending_file_path, e)
            return 1
        return 0

